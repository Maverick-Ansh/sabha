"""Plain chat completions against the box's vLLM servers, addressed by role.

Roles (docs/RESEARCH_PLAN.md section 1): s1 = Qwen3.5-0.8B triage, reader = Qwen3.5-4B
section mapper, deep = Qwen3.5-9B-AWQ reducer / planner / writer. A role that is down
falls back to the next healthy one. Every call: thinking off, optional guided JSON,
hard max_tokens, a loop guard, retries with backoff. The model never picks the next
step; code does.
"""
from __future__ import annotations

import json
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
_sem = {r: threading.BoundedSemaphore(v[2]) for r, v in ROLES.items()}
_health: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()
STATS = {r: {"calls": 0, "fail": 0, "tok": 0, "sec": 0.0, "fallback": 0} for r in ROLES}
OVERRIDE: dict[str, str] = {}  # e.g. {"s1": "reader"} after a failed triage calibration
WAIT_DOWN_S = float(__import__("os").environ.get("SABHA_LLM_WAIT_S") or 1500)  # how long a call waits for a restarting server


class LLMError(Exception):
    pass


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


def pick(role: str) -> str:
    role = OVERRIDE.get(role, role)
    if up(role):
        return role
    for r in FALLBACK[role]:
        if up(r):
            with _lock:
                STATS[role]["fallback"] += 1
            return r
    return role


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
        r = pick(role)
        name, port, _, ctx = ROLES[r]
        body = {"model": name, "max_tokens": max_tokens, "temperature": temperature,
                "repetition_penalty": 1.05, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": _fit(system, user, ctx, max_tokens)}]}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}
        t0 = time.time()
        try:
            with _sem[r]:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                             data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            msg = e.read()[:400].decode("utf-8", "replace")
            last = f"{r} HTTP {e.code}: {msg}"
            with _lock:
                STATS[r]["fail"] += 1
            if e.code == 400 and schema and "context" not in msg.lower() and "token" not in msg.lower():
                schema = None  # the server rejected the schema: fall back to free text + repair
                continue
            if e.code == 400:
                user = user[: int(len(user) * 0.7)]  # still too long for the window
                continue
            time.sleep(min(20 * (attempt + 1), 90))
            continue
        except Exception as e:
            last = f"{r} {type(e).__name__}: {str(e)[:120]}"
            with _lock:
                STATS[r]["fail"] += 1
            _health.pop(r, None)
            if isinstance(e, urllib.error.URLError) and time.time() - t_first < WAIT_DOWN_S:
                attempt -= 1  # no server on the port (a model switch, a restart): wait for it, the tries are for real errors
                time.sleep(30)
                continue
            time.sleep(min(20 * (attempt + 1), 90))
            continue
        ch = d["choices"][0]
        text = ch["message"].get("content") or ""
        with _lock:
            s = STATS[r]
            s["calls"] += 1
            s["tok"] += d.get("usage", {}).get("completion_tokens", 0)
            s["sec"] += time.time() - t0
        if looping(text):
            last = f"{r} output looped"
            temperature = min(temperature + 0.2, 0.9)
            continue
        trace.emit("think", text, role=r, secs=round(time.time() - t0, 1),
                   tok=d.get("usage", {}).get("completion_tokens", 0))
        return text
    trace.emit("error", f"{role} model gave no answer: {last[:300]}")
    raise LLMError(last or "no answer")


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
