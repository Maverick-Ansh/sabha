"""Plain chat completions against the box's vLLM servers, addressed by role.

Roles (docs/RESEARCH_PLAN.md section 1): s1 = Qwen3.5-0.8B triage, reader = Qwen3.5-4B
section mapper, deep = Qwen3.5-9B-AWQ reducer / planner / writer. A role that is down
falls back to the next healthy one. Every call: thinking off, optional guided JSON,
hard max_tokens, a loop guard, retries with backoff. The model never picks the next
step; code does.

Pool (multiKaggle): a role is served by every READY hub model with that served_name, this
box's servers and other boxes' (through their /api/relay link, with the hub token). The pool
is read from the hub (SABHA_HUB_URL) every 30 s, so a box can join or leave mid-run. A call
goes to the endpoint with a free slot, biggest first (its `concurrency`, else the role's
default), then the least loaded. An endpoint that fails is benched for 60 s and the call moves
on at once. Remote calls stream so a long answer never meets the tunnel's 100 s idle cut.
Without a hub the pool is this box's fixed ports, as before.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import trace

# role: (served name, port, max concurrent, context length)
ROLES = {"s1": ("s1", 8003, 16, 4096), "deep": ("deep", 8001, 4, 16384), "reader": ("reader", 8002, 6, 20480)}
for _r, (_n, _p, _c, _ctx) in list(ROLES.items()):  # a model switched in the app may have a smaller window (SABHA_CTX_DEEP=...)
    try:
        _srv = int(__import__("os").environ.get(f"SABHA_CTX_{_r.upper()}") or 0)
    except ValueError:
        _srv = 0
    if _srv:
        ROLES[_r] = (_n, _p, _c, min(_ctx, max(_srv - 1024, 4096)))
FALLBACK = {"s1": ["reader", "deep"], "reader": ["deep"], "deep": ["reader"]}
_health: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()
_free = threading.Condition(_lock)  # notified whenever an endpoint slot frees up
STATS = {r: {"calls": 0, "fail": 0, "tok": 0, "sec": 0.0, "fallback": 0} for r in ROLES}
OVERRIDE: dict[str, str] = {}  # e.g. {"s1": "reader"} after a failed triage calibration
WAIT_DOWN_S = float(os.environ.get("SABHA_LLM_WAIT_S") or 1500)  # how long a call waits for a restarting server
HUB = (os.environ.get("SABHA_HUB_URL") or "").rstrip("/")
POOL_EVERY_S = 30.0
BENCH_S = 60.0


class LLMError(Exception):
    pass


class Endpoint:
    """One server for one role: this box's port or another box's relay link."""

    def __init__(self, role: str, url: str, name: str, conc: int, ctx: int, key: str = "", label: str = ""):
        self.role, self.url, self.name, self.key = role, url.rstrip("/"), name, key
        self.conc, self.ctx, self.label = max(int(conc), 1), int(ctx), label or url
        self.remote = not self.url.startswith(("http://127.0.0.1", "http://localhost"))
        self.inflight, self.bad_until = 0, 0.0
        self.calls = self.fail = self.tok = 0
        self.sec = 0.0

    def ok(self) -> bool:
        return time.time() >= self.bad_until


_pool: dict[str, list[Endpoint]] = {}
_pool_at = 0.0
_by_url: dict[str, Endpoint] = {}  # kept across refreshes: a running call keeps its slot count


def _local_pool() -> dict[str, list[Endpoint]]:
    out = {}
    for r, (name, port, conc, ctx) in ROLES.items():
        ep = _by_url.get(f"http://127.0.0.1:{port}/v1") or Endpoint(r, f"http://127.0.0.1:{port}/v1", name, conc, ctx, label=f"local {r}")
        _by_url[ep.url] = ep
        out[r] = [ep] if up(r) else []
    return out


def _hub_pool() -> dict[str, list[Endpoint]] | None:
    """The READY models of the hub, grouped by role. None when the hub does not answer (then: local ports)."""
    try:
        req = urllib.request.Request(HUB + "/api/state", headers={"Authorization": "Bearer " + os.environ.get("SABHA_TOKEN", "")})
        models = json.loads(urllib.request.urlopen(req, timeout=6).read()).get("models") or []
    except Exception:
        return None
    out: dict[str, list[Endpoint]] = {r: [] for r in ROLES}
    for m in models:
        r, url = (m.get("served_name") or "").lower(), (m.get("base_url") or "").rstrip("/")
        if r not in ROLES or not url or m.get("status") != "ready" or m.get("kind") == "mock":
            continue
        _, _, conc0, ctx0 = ROLES[r]
        cw = int(m.get("context_window") or 0)
        ctx = min(ctx0, max(cw - 1024, 4096)) if cw else ctx0
        conc = int(m.get("concurrency") or 0) or conc0
        key = os.environ.get(m.get("api_key_env") or "", "") if m.get("api_key_env") else ""
        if not key and "/api/relay/" in url:  # a box linked after this run started: its key variable is not here; the fleet token is
            key = os.environ.get("SABHA_TOKEN", "")
        ep = _by_url.get(url)
        if ep is None:
            ep = _by_url[url] = Endpoint(r, url, m.get("served_name") or r, conc, ctx, key, m.get("name") or url)
        else:
            ep.conc, ep.ctx, ep.key, ep.label = max(conc, 1), ctx, key, m.get("name") or url
        out[r].append(ep)
    return out


def pool(force: bool = False) -> dict[str, list[Endpoint]]:
    global _pool, _pool_at
    if force or time.time() - _pool_at > POOL_EVERY_S or not _pool:
        p = _hub_pool() if HUB else None
        if p is None or not any(p.values()):  # no hub, or the hub lists nothing ready yet: this box's own ports
            p = _local_pool()
        with _lock:
            _pool, _pool_at = p, time.time()
    return _pool


def up(role: str) -> bool:
    t, ok = _health.get(role, (0.0, False))
    if time.time() - t < 20:
        return ok
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{ROLES[role][1]}/v1/models", timeout=4).read()
        ok = True
    except Exception:
        ok = False
    _health[role] = (time.time(), ok)
    return ok


def _roles_for(role: str) -> list[str]:
    role = OVERRIDE.get(role, role)
    return [role] + FALLBACK[role]


def pick(role: str) -> str:
    """The role that will answer (kept for callers that log it)."""
    p = pool()
    for r in _roles_for(role):
        if any(ep.ok() for ep in p.get(r) or []):
            return r
    return OVERRIDE.get(role, role)


def acquire(role: str, avoid: set | None = None, wait_s: float = 600) -> Endpoint | None:
    """A free slot on the best endpoint for this role (or its fallbacks). Blocks while all are busy.
    None when no endpoint is healthy at all."""
    t0 = time.time()
    while True:
        p = pool()
        target = None  # the first role (wanted, then fallbacks) that has a healthy endpoint; busy is not down
        for i, r in enumerate(_roles_for(role)):
            eps = [ep for ep in p.get(r) or [] if ep.ok() and ep.url not in (avoid or ())]
            if eps:
                target = (i, r, eps)
                break
        if target is None:
            return None
        i, r, eps = target
        with _lock:
            free = [ep for ep in eps if ep.inflight < ep.conc]
            if free:
                ep = min(free, key=lambda e: (-e.conc, e.inflight / e.conc))  # biggest server first, then least loaded
                ep.inflight += 1
                if i:
                    STATS[role]["fallback"] += 1
                return ep
            _free.wait(1.0)  # all busy: wait for a slot, then look again (the pool may have changed)
        if time.time() - t0 > wait_s:
            return None


def release(ep: Endpoint) -> None:
    with _lock:
        ep.inflight = max(ep.inflight - 1, 0)
        _free.notify_all()


def bench(ep: Endpoint, why: str) -> None:
    ep.bad_until = time.time() + BENCH_S
    ep.fail += 1
    trace.emit("error", f"{ep.label}: benched {int(BENCH_S)} s ({why[:120]})")


def pool_status() -> list[dict]:
    """Every endpoint with its load and totals (STATUS, the app)."""
    with _lock:
        return [{"role": ep.role, "label": ep.label, "remote": ep.remote, "conc": ep.conc, "inflight": ep.inflight,
                 "ok": ep.ok(), "calls": ep.calls, "fail": ep.fail, "tok": ep.tok,
                 "tps": round(ep.tok / ep.sec, 1) if ep.sec else 0.0} for ep in _by_url.values()]


def _post(ep: Endpoint, body: dict, timeout: float) -> dict:
    """One completion. Remote: streamed (SSE) and put back together into the usual response shape."""
    hdr = {"Content-Type": "application/json"}
    if ep.key:
        hdr["Authorization"] = "Bearer " + ep.key
    if not ep.remote:
        req = urllib.request.Request(ep.url + "/chat/completions", data=json.dumps(body).encode(), headers=hdr)
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(ep.url + "/chat/completions", data=json.dumps(body).encode(), headers=hdr)
    parts, usage, finish = [], {}, None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if ev.get("usage"):
                usage = ev["usage"]
            for c in ev.get("choices") or []:
                parts.append((c.get("delta") or {}).get("content") or "")
                finish = c.get("finish_reason") or finish
    return {"choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish}], "usage": usage}


def looping(text: str) -> bool:
    """True when an 80-character stretch repeats 3+ times near the end: the model is stuck."""
    tail = text[-5000:]
    for i in range(0, max(len(tail) - 80, 0), 60):
        if tail.count(tail[i:i + 80]) >= 3:
            return True
    return False


def _fit(system: str, user: str, ctx: int, max_tokens: int) -> str:
    budget_chars = int((ctx - max_tokens - 300) * 3.1) - len(system)
    if len(user) <= budget_chars:
        return user
    return user[:max(budget_chars, 2000)] + "\n[...cut to fit the context window...]"


def repair_json(s: str):
    """Parse JSON; if the model was cut off, close the open strings and brackets and try again."""
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    i = s.find("{")
    if i < 0:
        raise ValueError("no JSON object")
    s = s[i:]
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    tail = ('"' if in_str else "") + "".join(reversed(stack))
    for cut in (s + tail, re.sub(r",\s*\"[^\"]*\"?\s*:?\s*\"?[^\"]*$", "", s) + "".join(reversed(stack))):
        try:
            return json.loads(cut)
        except ValueError:
            continue
    raise ValueError("unrepairable JSON")


def chat(role: str, system: str, user: str, max_tokens: int = 800, schema: dict | None = None,
         temperature: float = 0.2, timeout: float = 240, tries: int = 6) -> str:
    last = ""
    t_first = time.time()
    attempt = -1
    while attempt + 1 < tries:
        attempt += 1
        ep = acquire(role)
        if ep is None:  # no server for this role or its fallbacks (a model switch, a restart): wait for one
            last = last or f"{role}: no server is up"
            if time.time() - t_first < WAIT_DOWN_S:
                attempt -= 1
                _health.clear()
                time.sleep(30)
                pool(force=True)
                continue
            break
        r = ep.role
        body = {"model": ep.name, "max_tokens": max_tokens, "temperature": temperature,
                "repetition_penalty": 1.05, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": _fit(system, user, ep.ctx, max_tokens)}]}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
        t0 = time.time()
        try:
            d = _post(ep, body, timeout)
        except urllib.error.HTTPError as e:
            msg = e.read()[:400].decode("utf-8", "replace")
            last = f"{ep.label} HTTP {e.code}: {msg}"
            _record(role, ep, attempt, system, body, "", t0, error=last)
            with _lock:
                STATS[r]["fail"] += 1
            if e.code == 400 and schema and "context" not in msg.lower() and "token" not in msg.lower():
                schema = None  # the server rejected the schema: fall back to free text + repair
                continue
            if e.code == 400:
                user = user[: int(len(user) * 0.7)]  # still too long for the window
                continue
            if e.code in (401, 403, 404, 502, 503, 504, 520, 521, 522, 523, 524, 530):
                bench(ep, last)  # a tunnel or relay problem on that box: the next try goes elsewhere at once
                attempt -= 1 if time.time() - t_first < WAIT_DOWN_S else 0
                continue
            time.sleep(min(20 * (attempt + 1), 90))
            continue
        except Exception as e:
            last = f"{ep.label} {type(e).__name__}: {str(e)[:120]}"
            _record(role, ep, attempt, system, body, "", t0, error=last)
            with _lock:
                STATS[r]["fail"] += 1
            _health.pop(r, None)
            if isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError)) and time.time() - t_first < WAIT_DOWN_S:
                attempt -= 1  # the server went away (a model switch, a restart, a tunnel): bench it, the tries are for real errors
                bench(ep, last)
                pool(force=True)
                continue
            time.sleep(min(20 * (attempt + 1), 90))
            continue
        finally:
            release(ep)
        ch = d["choices"][0]
        text = ch["message"].get("content") or ""
        ntok = (d.get("usage") or {}).get("completion_tokens", 0)
        with _lock:
            s = STATS[r]
            s["calls"] += 1
            s["tok"] += ntok
            s["sec"] += time.time() - t0
            ep.calls += 1
            ep.tok += ntok
            ep.sec += time.time() - t0
        loop_ = looping(text)
        _record(role, ep, attempt, system, body, text, t0, usage=d.get("usage") or {}, looped=loop_)
        if loop_:
            last = f"{r} output looped"
            temperature = min(temperature + 0.2, 0.9)
            continue
        trace.emit("think", text, role=r, secs=round(time.time() - t0, 1),
                   tok=d.get("usage", {}).get("completion_tokens", 0))
        return text
    trace.emit("error", f"{role} model gave no answer: {last[:300]}")
    raise LLMError(last or "no answer")


def _record(role: str, ep: Endpoint, attempt: int, system: str, body: dict, text: str, t0: float, **kw) -> None:
    """The full exchange into state/calls.jsonl (trace.exchange): the coordination history the app shows."""
    usage = kw.pop("usage", {}) or {}
    trace.exchange(asked=role, role=ep.role, endpoint=ep.label, remote=ep.remote, model=ep.name, attempt=attempt,
                   json=bool(body.get("response_format")), max_tokens=body.get("max_tokens"), system=system,
                   input=body["messages"][-1]["content"], output=text, tok=usage.get("completion_tokens", 0),
                   prompt_tok=usage.get("prompt_tokens", 0), secs=round(time.time() - t0, 2), **kw)


def chat_json(role: str, system: str, user: str, schema: dict | None, max_tokens: int = 800, **kw) -> dict:
    last = ""
    for _ in range(2):
        text = chat(role, system, user, max_tokens=max_tokens, schema=schema, **kw)
        try:
            out = repair_json(text)
            if isinstance(out, dict):
                return out
            last = "not an object"
        except ValueError as e:
            last = str(e)
        max_tokens = int(max_tokens * 1.4)
    raise LLMError(f"bad JSON from {role}: {last}")


def speed() -> dict:
    """Tokens per second per role, for STATUS and DIGEST."""
    with _lock:
        return {r: (round(s["tok"] / s["sec"], 1) if s["sec"] else 0.0) for r, s in STATS.items()}
