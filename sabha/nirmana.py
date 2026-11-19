"""Nirmana: the workshop. Load and remove models from the app, tune each one's context window, try them, and
chain them into flows whose every step is shown while it runs (PROTOCOL.md section 15).

Models. Nirmana edits the same <data>/run/layout.json the Models panel writes; the box supervisor
(sabha/box.py) sees the change within seconds and starts, restarts or stops vLLM servers to match. A server
beyond the two default roles (deep, reader) gets its own role m3, m4, ... and port 8003, 8004, ...; the
supervisor registers it in the hub as "<label> (m3)", so flows, Talk and the crew can use it like any model.
Live numbers come from each server's Prometheus /metrics (KV cache in use, requests running and waiting,
tokens per second), sampled when the app asks (every few seconds while Nirmana is on screen).

Flows. A flow is a list of stages; a run passes text from one stage to the next:
    decide  a model reads the text and picks one option; each option says where to go (next stage, a named
            stage, or the end). The model's reason is shown next to its choice.
    think   a model does the work its instructions describe ({input} = the text so far, {start} = the run's input)
    agent   the text becomes a Work mission for a crew agent (dsh, real tools); the stage waits for it to end
            and passes on the agent's result. The mission is on the Watch and Work views while it runs.
    search  a key-free web search for the text (sabha/webtools.py); passes on the results
    read    reads the first link in the text (a page or a PDF); passes on its text
Every model stage streams: ephemeral nirmana.delta events carry the pieces, persisted nirmana.run events the
run record (each stage's status, output, choice, reason, timings and tokens). Runs are kept under
<data>/nirmana/runs, flows under <data>/nirmana/flows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path

from aiohttp import web

from . import catalog, webtools
from .research_run import read_boot
from .talk import ChatError, stream_chat
from .util import ApiError, mock_speed, new_id, now, short

log = logging.getLogger("sabha.nirmana")

KINDS = ("decide", "think", "agent", "search", "read")
MAX_STAGES = 12
MAX_RUNS_KEPT = 200
FINISHED = ("done", "failed", "stopped")
CTX_STEPS = [2048, 4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072]
METRIC = re.compile(r"^(vllm:[a-z_]+)(\{[^}]*\})?\s+([0-9.eE+-]+|NaN)\s*$")

DECIDE_SYSTEM = (
    "You are a gate in a pipeline. You read the text you are given and pick exactly one of the options. "
    "Answer with one JSON object and nothing else: {\"choice\": \"<one option, copied exactly>\", \"why\": \"<one short sentence>\"}"
)
THINK_SYSTEM = "You are one step in a pipeline. Do exactly the work described. Write plain markdown. Do not add a preamble."

TEMPLATES = [
    {"id": "gatekeeper", "name": "Gatekeeper", "blurb": "A small model decides if a request is worth the big model's time.",
     "stages": [
         {"kind": "decide", "title": "Is it worth it?", "model": "reader",
          "prompt": "Does this request need careful, expert reasoning, or is it a quick question?\n\n{input}",
          "options": [{"label": "needs care", "goto": "next"}, {"label": "quick", "goto": "Quick answer"}]},
         {"kind": "think", "title": "Deep answer", "model": "deep",
          "prompt": "Answer this carefully, step by step, then give the answer in one line at the end.\n\n{start}", "options": []},
         {"kind": "think", "title": "Quick answer", "model": "reader", "prompt": "Answer in two sentences.\n\n{start}", "options": []}]},
    {"id": "scout", "name": "Scout", "blurb": "Search the web, then a model writes a short brief from what came back.",
     "stages": [
         {"kind": "search", "title": "Search", "model": "", "prompt": "{input}", "options": []},
         {"kind": "think", "title": "Brief", "model": "deep",
          "prompt": "From these search results, write a 5 bullet brief on: {start}\nCite the links you use.\n\n{input}", "options": []}]},
    {"id": "triage", "name": "Triage, then an agent", "blurb": "A gate sends real tasks to a crew agent with tools, and chat to a quick reply.",
     "stages": [
         {"kind": "decide", "title": "Task or chat?", "model": "reader",
          "prompt": "Is this a task that needs tools (code, files, the web), or just conversation?\n\n{input}",
          "options": [{"label": "task", "goto": "next"}, {"label": "chat", "goto": "Reply"}]},
         {"kind": "agent", "title": "Agent does it", "model": "", "prompt": "{start}", "options": [], "agent": ""},
         {"kind": "think", "title": "Reply", "model": "reader", "prompt": "Reply warmly and briefly.\n\n{start}", "options": []}]},
]


def _ctx_ceiling(m: dict, gpus: list[dict], share: float) -> int:
    """The longest context (from CTX_STEPS) whose KV cache fits next to the weights on these GPUs."""
    if not gpus:
        return 0
    tp = len(gpus)
    mem = min(g["mem_mb"] for g in gpus) / 1024
    util = (0.90 if mem < 30 else 0.92) * share
    w = float(m.get("weights_gib") or 0) / tp
    free = mem * util - w - (1.5 + 0.015 * w + (0.4 if tp > 1 else 0))
    kv_tok = float(m.get("kv_kib") or 64) / tp / (1024 * 1024)
    best = 0
    for c in CTX_STEPS:
        if c <= int(m.get("max_ctx") or 32768) and free >= kv_tok * c * 1.05:
            best = c
    return best


class Nirmana:
    def __init__(self, hub):
        self.hub = hub
        self.home = Path(hub.cfg.data_dir) / "nirmana"
        (self.home / "flows").mkdir(parents=True, exist_ok=True)
        (self.home / "runs").mkdir(parents=True, exist_ok=True)
        self.tasks: dict[str, asyncio.Task] = {}
        self.runs: dict[str, dict] = {}
        self._last: dict[str, tuple[float, dict]] = {}  # role -> (ts, counters) for tokens per second
        self._spark: dict[str, list[float]] = {}
        self._load_runs()

    # ------------------------------------------------------------ files

    def _read(self, p: Path, default=None):
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, p: Path, obj) -> None:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def _load_runs(self) -> None:
        for p in sorted((self.home / "runs").glob("*.json")):
            r = self._read(p)
            if isinstance(r, dict) and r.get("id"):
                if r.get("status") == "running":  # the hub restarted mid-run
                    r["status"] = "stopped"
                    r["error"] = "the hub restarted during this run"
                self.runs[r["id"]] = r

    async def close(self) -> None:
        for t in list(self.tasks.values()):
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    # ------------------------------------------------------------ the box: GPUs, servers, live numbers

    def _layout_path(self) -> Path:
        return Path(self.hub.cfg.data_dir) / "run" / "layout.json"

    def _layout(self) -> dict:
        d = self._read(self._layout_path(), {}) or {}
        return d if isinstance(d, dict) else {}

    # ------------------------------------------------------------ the default setup for new boxes

    def _default_path(self) -> Path:
        return Path(self.hub.cfg.data_dir) / "default_layout.json"

    def default(self) -> dict:
        """The setup a fresh box starts with: yours (saved here or in GitHub), else Sabha's preset (9B deep + 4B reader)."""
        mine = self._read(self._default_path(), None)
        src = "this box" if mine else ""
        lib = getattr(self.hub, "library", None)
        if not mine and lib is not None and lib._gh()[0]:
            try:
                mine, src = json.loads(lib._call("GET", "config/default_layout.json", raw=True, timeout=15) or b"null"), "GitHub"
            except Exception:  # noqa: BLE001 (404 or offline: Sabha's preset)
                mine = None
        servers = (mine or {}).get("servers") if isinstance(mine, dict) else None
        if servers:
            return {"mine": True, "source": src, "servers": [{k: s.get(k) for k in ("role", "hf", "label", "gpus", "ctx")} for s in servers],
                    "saved": (mine or {}).get("ts")}
        try:
            from . import catalog
            pre = catalog.presets(self._gpus() or [{"index": 0, "mem_mb": 15360, "name": "T4"}, {"index": 1, "mem_mb": 15360, "name": "T4"}])
            servers = pre[0]["servers"] if pre else []
        except Exception:  # noqa: BLE001
            servers = []
        return {"mine": False, "source": "Sabha", "servers": [{k: s.get(k) for k in ("role", "hf", "label", "gpus", "ctx")} for s in servers]}

    def set_default(self) -> dict:
        """Make the setup on the box now the default for every new box: saved here and, with GitHub connected, in
        config/default_layout.json of the repository (the box reads it at start, before it picks models)."""
        servers = [s for s in self._layout().get("servers") or [] if isinstance(s, dict) and s.get("hf") and s.get("role")]
        if not servers:
            raise ApiError(409, "no models are set up on this box yet")
        doc = {"servers": servers, "ts": time.time(), "by": "Nirmana: made the default"}
        p = self._default_path()
        p.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        in_github, err = False, ""
        lib = getattr(self.hub, "library", None)
        if lib is not None and lib._gh()[0]:
            try:
                lib._put("config/default_layout.json", json.dumps(doc, indent=1), "default models: " + ", ".join(f"{s['role']}={s['hf']}" for s in servers))
                in_github = True
            except Exception as e:  # noqa: BLE001
                err = f"not saved to GitHub: {str(e)[:120]}"
        return {**self.default(), "in_github": in_github, "error": err}

    def _gpus(self) -> list[dict]:
        b = read_boot(self.hub.cfg.data_dir)
        g = [x for x in b.get("gpus") or [] if isinstance(x, dict) and "mem_mb" in x]
        if not g and self.hub.cfg.demo:
            g = [{"index": 0, "name": "Tesla T4 (demo)", "mem_mb": 15360}, {"index": 1, "name": "Tesla T4 (demo)", "mem_mb": 15360}]
        return g

    def _hub_model(self, role: str) -> dict | None:
        for m in self.hub.store.models.values():
            if (m.get("name") or "").endswith(f"({role})") and "box launcher" in (m.get("notes") or ""):
                return m
        return None

    async def _metrics(self, port: int) -> dict:
        if self.hub.http is None:
            return {}
        try:
            async with self.hub.http.get(f"http://127.0.0.1:{port}/metrics", timeout=2.5) as r:
                if r.status != 200:
                    return {}
                text = await r.text()
        except Exception:
            return {}
        out: dict[str, float] = {}
        for line in text.splitlines():
            mm = METRIC.match(line)
            if mm and mm.group(3) != "NaN":
                out[mm.group(1)] = out.get(mm.group(1), 0.0) + float(mm.group(3))
        return out

    def _rates(self, role: str, c: dict) -> tuple[float, float]:
        t = time.time()
        gen = c.get("vllm:generation_tokens_total", 0.0)
        pr = c.get("vllm:prompt_tokens_total", 0.0)
        prev = self._last.get(role)
        self._last[role] = (t, {"g": gen, "p": pr})
        if not prev or t - prev[0] < 0.5:
            return 0.0, 0.0
        dt = t - prev[0]
        return max(gen - prev[1]["g"], 0) / dt, max(pr - prev[1]["p"], 0) / dt

    async def forge(self) -> dict:
        """Everything the Models half shows: the GPUs with memory in use, and each server with live numbers."""
        gpus = self._gpus()
        tele = ((getattr(self.hub.telemetry, "last_event", None) or {}).get("data") or {}).get("gpus") or []
        used = {int(x.get("index", i)): x for i, x in enumerate(tele)}
        lay = self._layout()
        steps = {s.get("id"): s for s in read_boot(self.hub.cfg.data_dir).get("steps") or []}
        servers = [dict(s) for s in lay.get("servers") or [] if isinstance(s, dict)]
        by_index = {g["index"]: g for g in gpus}
        rng = random.Random(int(time.time() / 3))
        out = []
        for s in servers:
            role = str(s.get("role"))
            st = steps.get("model:" + role) or {}
            status = st.get("status") or ("ready" if self.hub.cfg.demo else "pending")
            m = catalog.entry(s.get("hf") or "") or {"hf": s.get("hf"), "label": s.get("label"), "weights_gib": s.get("weights_gib") or 8,
                                                     "kv_kib": s.get("kv_kib") or 64, "max_ctx": s.get("max_ctx") or 32768}
            use = [by_index[i] for i in s.get("gpus") or [] if i in by_index]
            share = min(float(s.get("util") or 0.9) / 0.9, 1.0)
            hm = self._hub_model(role)
            live = {}
            if self.hub.cfg.demo:
                busy = any(t for t in self.tasks.values())
                live = {"kv_pct": round(rng.uniform(18, 64) if busy else rng.uniform(0, 3), 1), "running": (rng.randint(1, 3) if busy else 0),
                        "waiting": 0, "gen_tps": round(rng.uniform(30, 70), 1) if busy else 0.0, "prompt_tps": 0.0}
            else:  # a closed port answers at once, so asking a server that is still loading costs nothing
                c = await self._metrics(int(s.get("port") or 0))
                if c:
                    g, p = self._rates(role, c)
                    kv = c.get("vllm:kv_cache_usage_perc", c.get("vllm:gpu_cache_usage_perc", 0.0))
                    live = {"kv_pct": round(kv * 100 if kv <= 1.0 else kv, 1), "running": int(c.get("vllm:num_requests_running", 0)),
                            "waiting": int(c.get("vllm:num_requests_waiting", 0)), "gen_tps": round(g, 1), "prompt_tps": round(p, 1)}
            if live:
                sp = self._spark.setdefault(role, [])
                sp.append(live["gen_tps"])
                del sp[:-40]
            out.append({"role": role, "hf": s.get("hf"), "label": s.get("label") or s.get("hf"), "family": s.get("family"),
                        "gpus": s.get("gpus") or [], "tp": s.get("tp", 1), "port": s.get("port"), "util": s.get("util"),
                        "ctx": int(s.get("ctx") or 0), "max_ctx": int(m.get("max_ctx") or 32768),
                        "ctx_fit": _ctx_ceiling(m, use, share) if use else 0, "weights_gib": float(m.get("weights_gib") or 0),
                        "status": status, "detail": st.get("detail") or "", "secs": st.get("secs"), "expect_s": st.get("expect_s") or 600,
                        "model_id": hm["id"] if hm else None,
                        "model_status": hm.get("status") if hm else None, "live": live, "spark": self._spark.get(role, [])[-40:],
                        "core": role in ("deep", "reader")})
        gout = []
        for g in gpus:
            t = used.get(g["index"]) or {}
            share = sum(float(s.get("util") or 0) for s in servers if g["index"] in (s.get("gpus") or []))
            gout.append({**g, "sm": catalog.gpu_sm(g["name"]), "mem_used_mb": t.get("mem_used_mb"), "util_pct": t.get("util"),
                         "temp_c": t.get("temp_c") or t.get("temperature"), "share": round(share, 3),
                         "free_share": round(max(0.92 - share, 0.0), 3)})
        return {"gpus": gout, "servers": out, "demo": self.hub.cfg.demo, "by": lay.get("by", ""),
                "hf_token": bool(os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))}

    def _save_layout(self, servers: list[dict], why: str) -> None:
        for s in servers:
            s.pop("status", None)
            s.pop("detail", None)
        p = self._layout_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self._write(p, {"servers": servers, "by": why, "ts": time.time()})

    def plan(self, body: dict) -> dict:
        """How a model would sit on the chosen GPUs: fit verdict, the longest context that fits, the share it gets."""
        m = catalog.lookup(_hf_id(body.get("hf")))
        gpus = self._gpus()
        servers = self._layout().get("servers") or []
        want = [int(x) for x in body.get("gpus") or []] or [gpus[0]["index"]] if gpus else []
        by_index = {g["index"]: g for g in gpus}
        use = [by_index[i] for i in want if i in by_index]
        if not use:
            raise ApiError(409, "no GPU on this box to load a model on")
        replace = str(body.get("replace") or "")
        taken = max((sum(float(s.get("util") or 0) for s in servers if i in (s.get("gpus") or []) and s.get("role") != replace) for i in want),
                    default=0.0)
        share = round(max(0.92 - taken, 0.0) / 0.9, 3)
        if share < 0.2:
            return {"model": m, "fits": "no", "share": share, "ctx_fit": 0,
                    "why": f"GPU {','.join(map(str, want))} is full: remove a model there first, or pick another GPU"}
        plan = catalog.plan_for(m, use, min(share, 1.0), int(body.get("ctx") or 32768))
        return {"model": m, "fits": plan["fits"], "why": plan["why"], "share": min(share, 1.0), "gpus": want,
                "ctx_fit": _ctx_ceiling(m, use, min(share, 1.0)), "ctx": plan.get("ctx", 0), "util": plan.get("util")}

    def load(self, body: dict) -> dict:
        """{hf, gpus, ctx, replace?}: add a server for this model (or put it in the seat of `replace`)."""
        p = self.plan(body)
        if p["fits"] == "no":
            raise ApiError(409, p["why"])
        m, gpus = p["model"], self._gpus()
        servers = [dict(s) for s in self._layout().get("servers") or [] if isinstance(s, dict)]
        replace = str(body.get("replace") or "")
        ctx = int(body.get("ctx") or p["ctx_fit"] or 16384)
        ctx = max(2048, min(ctx, p["ctx_fit"] or ctx))
        if replace:
            if not any(s.get("role") == replace for s in servers):
                raise ApiError(404, f"no {replace} server to replace")
            role = replace
        elif not any(s.get("role") == "deep" for s in servers):
            role = "deep"
        elif not any(s.get("role") == "reader" for s in servers):
            role = "reader"
        else:
            n = 3
            while any(s.get("role") == f"m{n}" for s in servers):
                n += 1
            role = f"m{n}"
        used_ports = {int(s.get("port") or 0) for s in servers if s.get("role") != role}
        port = catalog.PORTS.get(role) or 8003
        while port in used_ports:
            port += 1
        plan = {"gpus": p["gpus"], "tp": len(p["gpus"]), "util": p["util"], "ctx": ctx, "fits": p["fits"], "why": p["why"]}
        srv = catalog.server(role, m, plan, [g for g in gpus if g["index"] in p["gpus"]])
        srv.update({"port": port, "kv_kib": m.get("kv_kib"), "max_ctx": m.get("max_ctx")})
        servers = [s for s in servers if s.get("role") != role] + [srv]
        self._save_layout(servers, f"Nirmana: {m['label']} as {role}")
        return {"role": role, "server": srv, "note": self._note(m)}

    def _note(self, m: dict) -> str:
        if self.hub.cfg.demo:
            return "Demo: the layout is saved, nothing is served."
        return (f"Loading {m['label']}: about {float(m.get('weights_gib') or 0):.0f} GB to download, then 5 to 10 minutes to warm up on a T4 (a model new to this box compiles first). "
                "Its card shows each step.")

    def set_ctx(self, role: str, ctx: int) -> dict:
        servers = [dict(s) for s in self._layout().get("servers") or [] if isinstance(s, dict)]
        s = next((x for x in servers if x.get("role") == role), None)
        if s is None:
            raise ApiError(404, "no such model on the box")
        m = catalog.entry(s.get("hf") or "") or {"weights_gib": s.get("weights_gib") or 8, "kv_kib": s.get("kv_kib") or 64,
                                                 "max_ctx": s.get("max_ctx") or 32768}
        by_index = {g["index"]: g for g in self._gpus()}
        use = [by_index[i] for i in s.get("gpus") or [] if i in by_index]
        cap = _ctx_ceiling(m, use, min(float(s.get("util") or 0.9) / 0.9, 1.0)) if use else 0
        ctx = int(ctx)
        if ctx < 2048:
            raise ApiError(400, "the context must be at least 2048 tokens")
        if cap and ctx > cap:
            raise ApiError(409, f"{ctx:,} tokens do not fit next to the weights on GPU {','.join(map(str, s.get('gpus') or []))}: "
                                f"at most {cap:,} here")
        s["ctx"] = ctx
        s["seqs"] = 8 if ctx >= 16384 else 6
        self._save_layout(servers, f"Nirmana: {s.get('label')} context {ctx}")
        return {"role": role, "ctx": ctx, "note": "Demo: saved." if self.hub.cfg.demo else
                "Restarting it with the new context window (about 5 minutes on a T4). Its card shows each step."}

    async def remove(self, role: str) -> dict:
        servers = [dict(s) for s in self._layout().get("servers") or [] if isinstance(s, dict)]
        gone = next((x for x in servers if x.get("role") == role), None)
        if gone is None:
            raise ApiError(404, "no such model on the box")
        rest = [s for s in servers if s.get("role") != role]
        self._save_layout(rest, f"Nirmana: removed {gone.get('label')}")
        moved = []
        hm = self._hub_model(role)
        if hm and role not in ("deep", "reader"):  # deep/reader entries stay: the launcher points them at another server
            keep = self._hub_model("deep") or self._hub_model("reader")
            for a in self.hub.store.agents.values():
                if a.get("model_id") == hm["id"] and keep:
                    a["model_id"] = keep["id"]
                    self.hub.bus.emit("agent.updated", {"agent": a}, agent_id=a["id"])
                    moved.append(a["name"])
            await self.hub.models.delete(hm["id"])
        self._spark.pop(role, None)
        self._last.pop(role, None)
        note = f"{gone.get('label')} is leaving the box; its GPU memory comes back in about a minute."
        if moved:
            note += f" {', '.join(moved)} now sit on {keep['name']}."
        if not rest:
            note += " No model is left: agents and flows wait until you load one."
        return {"removed": role, "note": note}

    # ------------------------------------------------------------ try: one streamed prompt straight at a model

    def try_model(self, body: dict) -> dict:
        model = self._model(body.get("model_id"))
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise ApiError(400, "prompt must be a non-empty string")
        tid = new_id("nt")
        msgs = ([{"role": "system", "content": str(body["system"])[:4000]}] if body.get("system") else []) + \
               [{"role": "user", "content": prompt[:20000]}]
        max_tokens = max(16, min(int(body.get("max_tokens") or 600), 4096))
        self.tasks[tid] = asyncio.create_task(self._try(tid, model, msgs, max_tokens))
        self.tasks[tid].add_done_callback(lambda _t: self.tasks.pop(tid, None))
        return {"try_id": tid}

    async def _try(self, tid: str, model: dict, msgs: list[dict], max_tokens: int) -> None:
        t0, first, n = time.time(), None, 0

        def piece(kind: str, s: str) -> None:
            nonlocal first, n
            first = first or time.time()
            n += 1
            self.hub.bus.emit("nirmana.delta", {"try_id": tid, kind: s}, ephemeral=True)
        err, usage = "", None
        try:
            if model.get("kind") == "mock":
                await self._mock_stream(piece, "A quick answer from the demo model: " + short(msgs[-1]["content"], 80) +
                                        "\n\nOn a real box this streams from vLLM, with the time to first token and the speed below.")
            else:
                _, _, usage = await stream_chat(self.hub.http, model, msgs, piece, max_tokens=max_tokens, timeout_s=600)
        except ChatError as e:
            err = str(e)
        except asyncio.CancelledError:
            err = "stopped"
        dt = time.time() - t0
        out = (usage or {}).get("completion_tokens") or n
        self.hub.bus.emit("nirmana.delta", {"try_id": tid, "done": True, "error": err, "ttft_ms": int(((first or time.time()) - t0) * 1000),
                                            "ms": int(dt * 1000), "tokens": out, "tps": round(out / max(dt - ((first or t0) - t0), 0.05), 1),
                                            "prompt_tokens": (usage or {}).get("prompt_tokens")}, ephemeral=True)

    async def _mock_stream(self, piece, text: str) -> None:
        for w in re.findall(r"\S+\s*", text):
            piece("text", w)
            await asyncio.sleep(0.035 / mock_speed())

    def _model(self, model_id) -> dict:
        m = self.hub.store.models.get(str(model_id or ""))
        if m is None:
            raise ApiError(400, "pick a model that is on the box")
        return m

    def _model_for(self, ref: str) -> dict | None:
        """A stage's model: a hub model id, or a role name (deep, reader, m3) that follows whatever serves it."""
        ref = str(ref or "")
        if ref in self.hub.store.models:
            return self.hub.store.models[ref]
        hm = self._hub_model(ref)
        if hm:
            return hm
        if ref in ("deep", "reader"):
            for m in self.hub.store.models.values():
                if m.get("served_name") == ref or m.get("kind") == "mock":
                    return m
        ms = list(self.hub.store.models.values())
        return ms[0] if ms and not ref else None

    # ------------------------------------------------------------ flows

    def _clean_flow(self, body: dict, fid: str | None = None) -> dict:
        name = short(str(body.get("name") or "Untitled flow"), 60)
        stages_in = body.get("stages")
        if not isinstance(stages_in, list) or not stages_in:
            raise ApiError(400, "a flow needs at least one stage")
        if len(stages_in) > MAX_STAGES:
            raise ApiError(400, f"at most {MAX_STAGES} stages")
        stages = []
        for i, s in enumerate(stages_in):
            if not isinstance(s, dict):
                raise ApiError(400, "each stage must be an object")
            kind = s.get("kind")
            if kind not in KINDS:
                raise ApiError(400, f"stage kind must be one of {', '.join(KINDS)}")
            opts = []
            for o in s.get("options") or []:
                if isinstance(o, dict) and str(o.get("label") or "").strip():
                    opts.append({"label": short(str(o["label"]), 40), "goto": short(str(o.get("goto") or "next"), 60)})
            if kind == "decide" and len(opts) < 2:
                raise ApiError(400, f"stage {i + 1} decides between options: give it at least two")
            stages.append({"id": str(s.get("id") or new_id("st")), "kind": kind, "title": short(str(s.get("title") or kind.title()), 50),
                           "model": str(s.get("model") or ""), "agent": str(s.get("agent") or ""),
                           "prompt": str(s.get("prompt") or "{input}")[:8000], "options": opts,
                           "max_tokens": max(16, min(int(s.get("max_tokens") or (200 if kind == "decide" else 900)), 4096))})
        old = self._read(self.home / "flows" / f"{fid}.json") if fid else None
        return {"id": fid or new_id("fl"), "name": name, "blurb": short(str(body.get("blurb") or ""), 160), "stages": stages,
                "created_ts": (old or {}).get("created_ts") or now(), "updated_ts": now()}

    def flows(self) -> dict:
        out = []
        for p in (self.home / "flows").glob("*.json"):
            f = self._read(p)
            if isinstance(f, dict) and f.get("id"):
                out.append(f)
        out.sort(key=lambda f: -(f.get("updated_ts") or 0))
        runs = sorted(self.runs.values(), key=lambda r: -(r.get("created_ts") or 0))[:40]
        return {"flows": out, "templates": TEMPLATES, "runs": [self._run_brief(r) for r in runs], "kinds": list(KINDS)}

    def save_flow(self, body: dict, fid: str | None = None) -> dict:
        if fid and not (self.home / "flows" / f"{fid}.json").exists():
            raise ApiError(404, "flow not found")
        f = self._clean_flow(body, fid)
        self._write(self.home / "flows" / f"{f['id']}.json", f)
        return f

    def delete_flow(self, fid: str) -> None:
        p = self.home / "flows" / f"{fid}.json"
        if not p.exists():
            raise ApiError(404, "flow not found")
        p.unlink()

    def _run_brief(self, r: dict) -> dict:
        return {k: r.get(k) for k in ("id", "flow_id", "flow_name", "status", "created_ts", "ended_ts", "ms", "error")} | \
               {"input": short(r.get("input") or "", 120), "path": [s["title"] for s in r.get("stages") or [] if s.get("status") == "done"]}

    def get_run(self, rid: str) -> dict:
        r = self.runs.get(rid)
        if r is None:
            raise ApiError(404, "run not found")
        return r

    def start_run(self, body: dict) -> dict:
        text = str(body.get("input") or "").strip()
        if not text:
            raise ApiError(400, "give the flow something to work on")
        if body.get("flow_id"):
            f = self._read(self.home / "flows" / f"{body['flow_id']}.json")
            if not f:
                raise ApiError(404, "flow not found")
        else:  # an unsaved flow straight from the editor
            f = self._clean_flow(body.get("flow") or {})
            f["id"] = ""
        if sum(1 for r in self.runs.values() if r.get("status") == "running") >= 4:
            raise ApiError(429, "four flows are already running: wait for one to end")
        rid = new_id("nr")
        r = {"id": rid, "flow_id": f.get("id") or "", "flow_name": f["name"], "flow": f, "input": text[:20000], "status": "running",
             "created_ts": now(), "ended_ts": None, "ms": 0, "error": "", "output": "", "current": None,
             "stages": [{"id": s["id"], "title": s["title"], "kind": s["kind"], "status": "waiting", "model": "", "output": "",
                         "choice": "", "why": "", "goto": "", "started_ts": None, "ms": 0, "ttft_ms": 0, "tokens_in": 0,
                         "tokens_out": 0, "mission_id": "", "error": "", "prompt": ""} for s in f["stages"]]}
        self.runs[rid] = r
        self._emit(r)
        self.tasks[rid] = asyncio.create_task(self._run(r))
        self.tasks[rid].add_done_callback(lambda _t: self.tasks.pop(rid, None))
        self._trim()
        return r

    def stop_run(self, rid: str) -> dict:
        r = self.get_run(rid)
        t = self.tasks.get(rid)
        if t and not t.done():
            t.cancel()
        return r

    def _trim(self) -> None:
        old = sorted(self.runs.values(), key=lambda r: r.get("created_ts") or 0)[:-MAX_RUNS_KEPT]
        for r in old:
            if r.get("status") != "running":
                self.runs.pop(r["id"], None)
                (self.home / "runs" / f"{r['id']}.json").unlink(missing_ok=True)

    def _emit(self, r: dict, save: bool = True) -> None:
        self.hub.bus.emit("nirmana.run", {"run": r})
        if save:
            self._write(self.home / "runs" / f"{r['id']}.json", r)

    async def _run(self, r: dict) -> None:
        f = r["flow"]
        titles = {s["title"].lower(): i for i, s in enumerate(f["stages"])}
        i, text, hops, t0 = 0, r["input"], 0, time.time()
        try:
            while 0 <= i < len(f["stages"]) and hops < 3 * MAX_STAGES:
                hops += 1
                st, rec = f["stages"][i], r["stages"][i]
                if rec["status"] == "done":  # a loop back: run it again, fresh
                    rec.update(output="", choice="", why="", error="", tokens_in=0, tokens_out=0)
                rec.update(status="running", started_ts=now())
                r["current"] = st["id"]
                self._emit(r, save=False)
                ts = time.time()
                out = await self._stage(r, st, rec, text)
                rec["ms"] = int((time.time() - ts) * 1000)
                rec["status"] = "done"
                nxt = i + 1
                if st["kind"] == "decide":
                    goto = rec["goto"] or "next"
                    if goto == "end":
                        nxt = len(f["stages"])
                    elif goto != "next":
                        nxt = titles.get(goto.lower(), i + 1)
                    # the text passes through a gate unchanged; the gate's output is its choice
                else:
                    text = out or text
                    r["output"] = text
                for j in range(i + 1, nxt):  # stages jumped over by a gate
                    if r["stages"][j]["status"] == "waiting":
                        r["stages"][j]["status"] = "skipped"
                if nxt <= i:  # a gate sent the run back: let the later stages wait again
                    for j in range(nxt, len(f["stages"])):
                        if j != i and r["stages"][j]["status"] == "skipped":
                            r["stages"][j]["status"] = "waiting"
                # a stage a gate can jump to runs only when that gate picked it; the run merges after the branches
                while st["kind"] != "decide" and nxt < len(f["stages"]) and self._is_branch_target(f, nxt) and not self._reached_by(f, r, nxt):
                    if r["stages"][nxt]["status"] == "waiting":
                        r["stages"][nxt]["status"] = "skipped"
                    nxt += 1
                self._emit(r, save=False)
                i = nxt
            for rec in r["stages"]:
                if rec["status"] == "waiting":
                    rec["status"] = "skipped"
            r["status"] = "done"
        except asyncio.CancelledError:
            r["status"], r["error"] = "stopped", "stopped by you"
            for rec in r["stages"]:
                if rec["status"] == "running":
                    rec["status"] = "stopped"
        except StageError as e:
            r["status"], r["error"] = "failed", str(e)
        except Exception as e:  # never leave a run "running"
            log.exception("nirmana run failed")
            r["status"], r["error"] = "failed", f"{type(e).__name__}: {e}"
        r["current"] = None
        r["ended_ts"] = now()
        r["ms"] = int((time.time() - t0) * 1000)
        self._emit(r)

    @staticmethod
    def _is_branch_target(f: dict, i: int) -> bool:
        t = f["stages"][i]["title"].lower()
        return any(o["goto"].lower() == t for s in f["stages"] if s["kind"] == "decide" for o in s["options"])

    @staticmethod
    def _reached_by(f: dict, r: dict, i: int) -> bool:
        """True when the gate that last ran chose this stage (so it is the branch the run is on)."""
        t = f["stages"][i]["title"].lower()
        last = [rec for rec, s in zip(r["stages"], f["stages"]) if s["kind"] == "decide" and rec["status"] == "done"]
        return bool(last) and last[-1]["goto"].lower() == t

    async def _stage(self, r: dict, st: dict, rec: dict, text: str) -> str:
        prompt = st["prompt"].replace("{input}", text).replace("{start}", r["input"])
        rec["prompt"] = short(prompt, 600)
        kind = st["kind"]
        if kind == "search":
            q = short(prompt, 200)
            rec["model"] = "web search"
            hits, engine = await asyncio.to_thread(webtools.search, q, 6)
            lines = [f"{k + 1}. **{h.get('title') or h.get('url')}** {h.get('url', '')}"
                     + (f" (published {h['date']})" if h.get("date") else "") + f"\n   {short(h.get('snippet') or '', 300)}"
                     for k, h in enumerate(hits)]
            out = (f"Search ({engine or 'none'}) for: {q}\n\n" + "\n".join(lines)) if lines else f"No results for: {q}"
            rec["output"] = out
            self._delta(r, st, "text", out)
            return out
        if kind == "read":
            mm = re.search(r"https?://\S+", prompt)
            if not mm:
                raise StageError(f"{st['title']}: there is no link in the text to read")
            url = mm.group(0).rstrip(").,;]'\"")
            rec["model"] = "page reader"
            title, body = await asyncio.to_thread(webtools.page_text, url)
            out = f"# {title or url}\n{url}\n\n{body[:6000]}"
            rec["output"] = out
            self._delta(r, st, "text", short(body, 1200))
            return out
        if kind == "agent":
            return await self._agent(r, st, rec, prompt)
        model = self._model_for(st["model"])
        if model is None:
            raise StageError(f"{st['title']}: its model ({st['model'] or 'none'}) is not on the box. Pick another in the flow editor.")
        rec["model"] = model.get("name") or ""
        if kind == "decide":
            labels = [o["label"] for o in st["options"]]
            ask = prompt + "\n\nOptions: " + " | ".join(labels) + "\nAnswer with the JSON object only."
            msgs = [{"role": "system", "content": DECIDE_SYSTEM}, {"role": "user", "content": ask}]
            out = await self._chat(r, st, rec, model, msgs, labels)
            choice, why = parse_choice(out, labels)
            if not choice:
                choice, why = labels[0], f"(the model's answer named no option, so the first one was taken) {short(out, 120)}"
            rec["choice"], rec["why"] = choice, why
            rec["goto"] = next(o["goto"] for o in st["options"] if o["label"] == choice)
            rec["output"] = out
            return out
        msgs = [{"role": "system", "content": THINK_SYSTEM}, {"role": "user", "content": prompt}]
        out = await self._chat(r, st, rec, model, msgs, None)
        rec["output"] = out
        return out

    def _delta(self, r: dict, st: dict, kind: str, s: str) -> None:
        self.hub.bus.emit("nirmana.delta", {"run_id": r["id"], "stage_id": st["id"], kind: s}, ephemeral=True)

    async def _chat(self, r, st, rec, model, msgs, labels) -> str:
        t0, first = time.time(), None
        parts: list[str] = []

        def piece(kind: str, s: str) -> None:
            nonlocal first
            if kind == "text":
                first = first or time.time()
                parts.append(s)
            self._delta(r, st, kind, s)
        usage = None
        if model.get("kind") == "mock":
            await asyncio.sleep(0.4 / mock_speed())
            first = time.time()
            if labels:
                pick = labels[sum(map(ord, r["input"])) % len(labels)]
                await self._mock_stream(piece, json.dumps({"choice": pick, "why": f"in the demo the gate picks '{pick}' for this input"}))
            else:
                await self._mock_stream(piece, f"**{st['title']}** (demo model). This stage would do: {short(msgs[-1]['content'], 160)}\n\n"
                                               "- one point it found\n- another point\n- a conclusion in one line")
        else:
            try:
                _, _, usage = await stream_chat(self.hub.http, model, msgs, piece, max_tokens=st["max_tokens"], timeout_s=900)
            except ChatError as e:
                raise StageError(f"{st['title']}: {e}") from None
        rec["ttft_ms"] = int(((first or time.time()) - t0) * 1000)
        rec["tokens_in"] = (usage or {}).get("prompt_tokens") or sum(len(m["content"]) for m in msgs) // 4
        rec["tokens_out"] = (usage or {}).get("completion_tokens") or len("".join(parts)) // 4
        out = "".join(parts).strip()
        if not out:
            raise StageError(f"{st['title']}: the model said nothing (it may have used its whole budget thinking)")
        return out

    async def _agent(self, r, st, rec, prompt) -> str:
        agents = self.hub.store.agents
        aid = st.get("agent") or ""
        if aid not in agents:
            aid = next(iter(agents), "")
        if not aid:
            raise StageError(f"{st['title']}: there is no crew agent on this box")
        rec["model"] = agents[aid]["name"]
        m = await self.hub.missions.create({"prompt": prompt, "agent_id": aid, "title": f"{r['flow_name']}: {st['title']}"})
        rec["mission_id"] = m["id"]
        self._emit(r, save=False)
        self._delta(r, st, "text", f"Handed to {agents[aid]['name']} as a Work mission. Watch it live on the Watch view.\n\n")
        t_end = time.time() + 45 * 60
        try:
            while time.time() < t_end:
                cur = self.hub.store.missions.get(m["id"]) or {}
                if cur.get("status") in FINISHED:
                    break
                if cur.get("status") == "awaiting_approval":
                    self._delta(r, st, "text", "(the agent waits for your approval on the Work view)\n")
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            try:
                await self.hub.missions.stop(m["id"])
            except Exception:
                pass
            raise
        cur = self.hub.store.missions.get(m["id"]) or {}
        stats = cur.get("stats") or {}
        rec["tokens_in"], rec["tokens_out"] = stats.get("prompt_tokens", 0), stats.get("completion_tokens", 0)
        if cur.get("status") != "done":
            raise StageError(f"{st['title']}: the agent's mission ended {cur.get('status') or 'without a result'}"
                             + (f": {short(cur.get('error') or '', 200)}" if cur.get("error") else ""))
        out = (cur.get("result_text") or "").strip() or "(the agent finished without a written result)"
        rec["output"] = out
        self._delta(r, st, "text", out)
        return out


class StageError(Exception):
    pass


def _hf_id(v) -> str:
    """owner/name from an id, a huggingface.co link, or 'hf.co/owner/name'."""
    s = str(v or "").strip()
    mm = re.search(r"(?:huggingface\.co|hf\.co)/([\w.\-]+/[\w.\-]+)", s)
    if mm:
        return mm.group(1)
    return s.strip("/")


def parse_choice(text: str, labels: list[str]) -> tuple[str, str]:
    """(choice, why) from a gate's answer: the JSON object if there is one, else the first option it names."""
    low = {lab.lower(): lab for lab in labels}
    for mm in re.finditer(r"\{[^{}]*\}", text, re.S):
        try:
            d = json.loads(mm.group(0))
        except ValueError:
            continue
        c = str(d.get("choice") or "").strip().strip("\"'").lower()
        if c in low:
            return low[c], short(str(d.get("why") or ""), 240)
        for k, lab in low.items():
            if k in c or c in k and c:
                return lab, short(str(d.get("why") or ""), 240)
    hits = sorted(((text.lower().find(k), lab) for k, lab in low.items() if k in text.lower()), key=lambda x: x[0])
    return (hits[0][1], short(text, 200)) if hits else ("", "")


# ---------------------------------------------------------------- routes


def add_routes(router, hub_of, body_of) -> None:
    def nm(request) -> Nirmana:
        return hub_of(request).nirmana

    async def forge(request):
        return web.json_response(await nm(request).forge())

    async def plan(request):
        b = await body_of(request)
        return web.json_response(await asyncio.to_thread(nm(request).plan, b))

    async def load(request):
        b = await body_of(request)
        return web.json_response(await asyncio.to_thread(nm(request).load, b))

    async def ctx(request):
        b = await body_of(request)
        return web.json_response(nm(request).set_ctx(request.match_info["role"], int(b.get("ctx") or 0)))

    async def remove(request):
        return web.json_response(await nm(request).remove(request.match_info["role"]))

    async def try_(request):
        return web.json_response(nm(request).try_model(await body_of(request)))

    async def flows(request):
        return web.json_response(nm(request).flows())

    async def new_flow(request):
        return web.json_response(nm(request).save_flow(await body_of(request)))

    async def put_flow(request):
        return web.json_response(nm(request).save_flow(await body_of(request), request.match_info["id"]))

    async def del_flow(request):
        nm(request).delete_flow(request.match_info["id"])
        return web.json_response({"ok": True})

    async def run(request):
        return web.json_response(nm(request).start_run(await body_of(request)))

    async def get_run(request):
        return web.json_response(nm(request).get_run(request.match_info["id"]))

    async def stop_run(request):
        return web.json_response(nm(request).stop_run(request.match_info["id"]))

    router.add_get("/api/nirmana/forge", forge)
    router.add_post("/api/nirmana/plan", plan)
    router.add_post("/api/nirmana/load", load)

    async def get_default(request):
        return web.json_response(await asyncio.to_thread(nm(request).default))

    async def post_default(request):
        return web.json_response(await asyncio.to_thread(nm(request).set_default))
    router.add_get("/api/nirmana/default", get_default)
    router.add_post("/api/nirmana/default", post_default)
    router.add_post("/api/nirmana/models/{role}/ctx", ctx)
    router.add_delete("/api/nirmana/models/{role}", remove)
    router.add_post("/api/nirmana/try", try_)
    router.add_get("/api/nirmana/flows", flows)
    router.add_post("/api/nirmana/flows", new_flow)
    router.add_put("/api/nirmana/flows/{id}", put_flow)
    router.add_delete("/api/nirmana/flows/{id}", del_flow)
    router.add_post("/api/nirmana/runs", run)
    router.add_get("/api/nirmana/runs/{id}", get_run)
    router.add_post("/api/nirmana/runs/{id}/stop", stop_run)
