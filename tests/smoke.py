"""Smoke test for the Sabha hub. Plain script, no pytest:

    python tests/smoke.py          exit code 0 = everything passed

It starts the hub in-process on a free port with --demo semantics and
SABHA_MOCK_SPEED=25, plus a tiny fake OpenAI-compatible server and a fake
`dsh` on PATH, then exercises the REST API and the WebSocket stream.
"""

from __future__ import annotations

import asyncio
import json
import zipfile
import io
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["SABHA_MOCK_SPEED"] = "25"

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from sabha.server import Config, Hub, create_app  # noqa: E402
from sabha.store import DataDirBusy  # noqa: E402

TOKEN = "smoke-token-123"
FAILS: list[str] = []
IS_WINDOWS = os.name == "nt"


def check(name: str, cond, detail="") -> bool:
    ok = bool(cond)
    line = f"{'PASS' if ok else 'FAIL'}  {name}"
    if not ok and detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    if not ok:
        FAILS.append(name)
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- fakes

FAKE_DSH = r'''
import json, os, sys, time
args = sys.argv[1:]
task = sys.stdin.read()

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

problems = []
if args[:2] != ["--profile", "headless"]:
    problems.append("profile")
if "--json" not in args or args[-1] != "-":
    problems.append("json-or-dash")
patch = args[args.index("--patch") + 1] if "--patch" in args else ""
text = open(patch, encoding="utf-8").read() if patch and os.path.exists(patch) else ""
if "baseURL:" not in text or "agent-default-model" not in text or "apiKeyEnv: SABHA_LLM_KEY" not in text:
    problems.append("patch")
for key in ("DSH_HOME", "DSH_PERMISSION_MODE", "SABHA_LLM_KEY"):
    if not os.environ.get(key):
        problems.append(key)
resumed = "--session-id" in args
sid = args[args.index("--session-id") + 1] if resumed else "sess-fake-1"
sys.stderr.write("dsh: fake harness starting\n")
sys.stderr.flush()
out({"type": "session", "sessionId": sid, "cwd": os.getcwd()})
out({"type": "status", "phase": "turn_start", "turn": 1})
if "SLOW" in task:
    for _ in range(600):
        time.sleep(0.1)
out({"type": "status", "phase": "step_start", "turn": 1, "step": 1})
out({"type": "thinking", "text": "fake thinking", "truncated": False})
out({"type": "text", "text": "TASK<<" + task + ">> ARGS<<" + " ".join(args) + ">> PROBLEMS<<" + ",".join(problems) + ">>"})
out({"type": "tool_call", "callId": "c1", "tool": "todo_write", "input": {"todos": [
    {"content": "first", "status": "completed"},
    {"content": "second", "status": "in_progress"},
    {"content": "third", "status": "pending"}]}})
out({"type": "tool_result", "callId": "c1", "status": "completed", "result": "ok"})
out({"type": "tool_call", "callId": "c2", "tool": "bash", "input": {"command": "false"}})
out({"type": "tool_result", "callId": "c2", "status": "error", "result": "", "error": "exit 1"})
with open("from_fake_dsh.txt", "w") as f:
    f.write("hello from fake dsh")
out({"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": {"input": 1000, "output": 50}})
out({"type": "mystery", "x": 1})
out({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}})
out({"type": "final", "sessionId": sid, "reason": {"kind": "completed"},
     "text": ("RESUMED " if resumed else "") + "FINAL " + "X" * 150000})
sys.exit(0)
'''


def install_fake_dsh(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "fake_dsh.py"
    script.write_text(FAKE_DSH, encoding="utf-8")
    if IS_WINDOWS:
        (bin_dir / "dsh.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %ERRORLEVEL%\r\n', encoding="utf-8")
    else:
        exe = bin_dir / "dsh"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)


FAKE_SEEN: list[dict] = []


async def fake_models(request):
    return web.json_response({"object": "list", "data": [{"id": "fake-llm"}]})


async def fake_chat(request):
    body = await request.json()
    FAKE_SEEN.append(body)
    last = body["messages"][-1]["content"] or ""
    if body["messages"][-1]["role"] == "tool":  # Talk fed a tool result back: answer from it
        pieces = [("content", "Found it: " + last[:40])]
    elif body.get("tools") and "MEMORYTEST" in last:  # Talk with tools: ask to search memory first
        pieces = [("tool_calls", [{"index": 0, "id": "call_1", "type": "function",
                                   "function": {"name": "search_memory", "arguments": ""}}]),
                  ("tool_calls", [{"index": 0, "function": {"arguments": json.dumps({"query": "joint embedding predictive"})}}])]
    elif "JSON array" in last:
        pieces = [("content", '["Read the brief", "Write the code",'), ("content", ' "Test it"]')]
    else:
        pieces = [("reasoning_content", "Thinking about it. "), ("content", "<thi"),
                  ("content", "nk>inline thought</think>Hello from "), ("content", "the fake model.")]
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    for key, text in pieces:
        chunk = {"choices": [{"index": 0, "delta": {key: text}}]}
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await asyncio.sleep(0.02)
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


# ---------------------------------------------------------------- client helpers


class Api:
    def __init__(self, http: aiohttp.ClientSession, base: str):
        self.http = http
        self.base = base

    async def call(self, method: str, path: str, body=None, token: str | None = TOKEN, raw: str | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        kwargs = {}
        if raw is not None:
            kwargs["data"] = raw
            headers["Content-Type"] = "application/json"
        elif body is not None:
            kwargs["json"] = body
        async with self.http.request(method, self.base + path, headers=headers, **kwargs) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except ValueError:
                data = text
            return r.status, data


class Watch:
    """Collects every WebSocket event and lets the test wait for one."""

    def __init__(self):
        self.events: list[dict] = []
        self._new = asyncio.Event()

    async def pump(self, ws) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.events.append(json.loads(msg.data))
                self._new.set()

    async def wait_for(self, pred, timeout: float = 30.0, start: int = 0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        i = start
        while True:
            while i < len(self.events):
                if pred(self.events[i]):
                    return self.events[i]
                i += 1
            self._new.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._new.wait(), remaining)
            except asyncio.TimeoutError:
                pass

    def of(self, type_: str, **match) -> list[dict]:
        out = []
        for e in self.events:
            if e.get("type") != type_:
                continue
            if all(e.get(k) == v for k, v in match.items()):
                out.append(e)
        return out


def mission_is(mid: str, *statuses: str, runs: int | None = None):
    def pred(e):
        if e.get("type") != "mission.updated":
            return False
        m = e["data"]["mission"]
        return m["id"] == mid and m["status"] in statuses and (runs is None or m["runs"] >= runs)
    return pred


async def start_hub(data_dir: Path, port: int) -> web.AppRunner:
    hub = Hub(Config(host="127.0.0.1", port=port, data_dir=data_dir, token=TOKEN, demo=True))
    runner = web.AppRunner(create_app(hub), access_log=None, handle_signals=False)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


# ---------------------------------------------------------------- the test


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sabha_smoke_"))
    data_dir = tmp / "data"
    bin_dir = tmp / "bin"
    install_fake_dsh(bin_dir)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path

    fake_port = free_port()
    fake_app = web.Application()
    fake_app.router.add_get("/v1/models", fake_models)
    fake_app.router.add_post("/v1/chat/completions", fake_chat)
    fake_runner = web.AppRunner(fake_app, access_log=None)
    await fake_runner.setup()
    await web.TCPSite(fake_runner, "127.0.0.1", fake_port).start()
    fake_url = f"http://127.0.0.1:{fake_port}/v1"

    port = free_port()
    runner = await start_hub(data_dir, port)
    base = f"http://127.0.0.1:{port}"
    http = aiohttp.ClientSession()
    api = Api(http, base)
    watch = Watch()
    ws = None
    pump = None
    t0 = time.monotonic()
    try:
        # ---------------- one hub per data dir
        try:
            Hub(Config(data_dir=data_dir, token=TOKEN, demo=True))
            check("second hub on the same data dir is refused", False, "it started")
        except DataDirBusy:
            check("second hub on the same data dir is refused", True)

        # ---------------- auth and static
        st, body = await api.call("GET", "/api/state", token=None)
        check("401 without token", st == 401 and body == {"error": "unauthorized"}, f"{st} {body}")
        st, _ = await api.call("GET", "/api/state", token="wrong")
        check("401 with a wrong token", st == 401)
        try:
            bad = await http.ws_connect(base + "/ws")
            await bad.close()
            check("ws 401 without token", False, "connected")
        except aiohttp.WSServerHandshakeError as e:
            check("ws 401 without token", e.status == 401, str(e.status))
        async with http.get(base + "/") as r:
            check("static / open without token, no-cache", r.status == 200
                  and r.headers.get("Cache-Control") == "no-cache", f"{r.status} {r.headers.get('Cache-Control')}")

        # ---------------- state shape
        st, state = await api.call("GET", "/api/state")
        keys = {"app", "system", "models", "agents", "missions", "threads", "seq_head"}
        check("/api/state shape", st == 200 and keys <= set(state), f"{st} {sorted(state) if isinstance(state, dict) else state}")
        check("app block", state["app"].get("name") == "Sabha" and state["app"].get("demo") is True
              and isinstance(state["app"].get("version"), str))
        sysb = state["system"]
        check("system block", sysb.get("platform") in ("colab", "kaggle", "local")
              and sysb.get("harnesses") == {"dsh": True, "mock": True, "chat": True}
              and sysb.get("session_limit_s") == 12 * 3600 and isinstance(sysb.get("data_dir"), str), str(sysb))
        check("demo seed: 2 mock models, 4 agents, 1 thread, 2 missions",
              len(state["models"]) == 2 and all(m["kind"] == "mock" and m["status"] == "ready" for m in state["models"])
              and [a["name"] for a in state["agents"]] == ["Lead", "Scout", "Builder", "Critic"]
              and len(state["threads"]) == 1 and len(state["missions"]) == 2)
        check("threads in snapshot carry no messages", all("messages" not in t for t in state["threads"]))
        agents = {a["name"]: a for a in state["agents"]}
        lead_thread = state["threads"][0]["id"]
        demo_missions = [m["id"] for m in state["missions"]]

        # ---------------- websocket: hello, replay, live, ping
        ws = await http.ws_connect(f"{base}/ws?token={TOKEN}&since=0")
        first = await ws.receive_json(timeout=10)
        check("ws hello first", first.get("type") == "hello" and isinstance(first.get("seq_head"), int)
              and keys <= set(first.get("state", {})), str(first)[:200])
        head = first["seq_head"]
        pump = asyncio.create_task(watch.pump(ws))
        replayed = await watch.wait_for(lambda e: e.get("seq", 0) == head, 10)
        check("ws replays persisted events since=0 up to seq_head", replayed is not None
              and [e["seq"] for e in watch.events if 0 < e.get("seq", 0) <= head] == list(range(1, head + 1)))
        live = await watch.wait_for(lambda e: e.get("seq", 0) > head, 15)
        check("ws live events after replay", live is not None)
        await ws.send_json({"type": "ping"})
        check("ws ping -> pong", await watch.wait_for(lambda e: e.get("type") == "pong", 5) is not None)
        tele = await watch.wait_for(lambda e: e.get("type") == "sys.telemetry", 8)
        check("sys.telemetry ephemeral event", tele is not None and tele["seq"] == 0
              and {"gpus", "cpu_pct", "ram_used_mb", "disk_total_gb", "uptime_s", "session_limit_s"} <= set(tele["data"]))

        # ---------------- demo missions finish
        for mid in demo_missions:
            ev = await watch.wait_for(mission_is(mid, "done", "failed"), 60)
            check(f"demo mission {mid} done", ev is not None and ev["data"]["mission"]["status"] == "done",
                  ev and ev["data"]["mission"].get("error"))

        # ---------------- council: mock members read a pasted paper, discuss, give a verdict
        from sabha import webtools
        from sabha.council import assign
        check("council assign() balances consecutive sections", assign([10, 10, 10, 10], 2) == [[0, 1], [2, 3]]
              and sorted(sum(assign([5], 3), [])) == [0] and assign([30, 1, 1], 2) == [[0], [1, 2]])
        title, text = webtools.html_to_text(
            "<html><head><title>T</title></head><body><nav>menu</nav><h2>1 Intro</h2><p>Hello <math alttext='x^2'>"
            "<mi>x</mi></math> there.</p><section class='ltx_bibliography'><h2>References</h2><p>[1] X</p></section></body></html>")
        check("webtools html_to_text: headings, math as LaTeX, nav and references dropped",
              title == "T" and "## 1 Intro" in text and "$x^2$" in text and "menu" not in text and "References" not in text, text)
        paper_text = "A Tiny Paper\n\nAbstract\nWe test a thing and it works well in 3 settings.\n\n" + "\n\n".join(
            f"{i} {name}\n" + " ".join(f"In step {k} of {name.lower()} the method reached {60 + k} percent accuracy on the held out split."
                                       for k in range(1, 9))
            for i, name in enumerate(["Introduction", "Method", "Experiments", "Conclusion"], 1))
        two = [agents["Lead"]["id"], agents["Critic"]["id"]]
        st, body = await api.call("POST", "/api/councils", {"text": paper_text, "agent_ids": two[:1]})
        check("POST /api/councils needs 2 agents -> 400", st == 400, f"{st} {body}")
        mark = len(watch.events)
        st, c = await api.call("POST", "/api/councils", {"text": paper_text, "agent_ids": two, "rounds": 1,
                                                         "lookups": False, "question": "Does it work?"})
        check("POST /api/councils", st == 200 and c["id"].startswith("c_") and c["status"] == "fetching", str(c)[:200])
        cid = c["id"]

        def council_is(*statuses):
            return lambda e: (e.get("type") == "council.updated" and e["data"]["council"]["id"] == cid
                              and e["data"]["council"]["status"] in statuses)

        ev = await watch.wait_for(council_is("done", "failed"), 60, start=mark)
        check("council runs to done", ev is not None and ev["data"]["council"]["status"] == "done",
              ev and ev["data"]["council"].get("error"))
        check("council.updated carries no messages", ev is not None and "messages" not in ev["data"]["council"])
        st, c = await api.call("GET", f"/api/councils/{cid}")
        kinds = [m["kind"] for m in c["messages"]]
        check("council messages: 2 briefs, 2 turns, 1 verdict", st == 200 and kinds.count("brief") == 2
              and kinds.count("say") == 2 and kinds[-1] == "verdict" and c["verdict"], str(kinds))
        check("council paper cut into sections, every seat has a part", len(c["paper"]["sections"]) == 4
              and all(seat["section_ids"] for seat in c["seats"]) and c["paper"]["abstract"].startswith("We test"),
              str(c["paper"])[:300])
        deltas = [e for e in watch.events[mark:] if e["type"] == "council.delta" and e["data"]["council_id"] == cid]
        check("council.delta stream is ephemeral and names the agent", len(deltas) > 10
              and all(e["seq"] == 0 and e["agent_id"] in two for e in deltas))
        st, sec = await api.call("GET", f"/api/councils/{cid}/section?id=s2")
        check("GET council section", st == 200 and sec["title"] == "2 Method" and "percent accuracy" in sec["text"], str(sec)[:200])
        mark = len(watch.events)
        st, _ = await api.call("POST", f"/api/councils/{cid}/say", {"text": "What about seeds?"})
        await watch.wait_for(council_is("discussing"), 10, start=mark)
        ev = await watch.wait_for(council_is("done", "failed"), 60, start=mark)
        st, c = await api.call("GET", f"/api/councils/{cid}")
        kinds = [m["kind"] for m in c["messages"]]
        check("human message after the verdict starts one more round", ev is not None and c["round"] == 2
              and kinds.count("verdict") == 2 and "human" in kinds, str(kinds))
        st, _ = await api.call("DELETE", f"/api/councils/{cid}")
        st2, _ = await api.call("GET", f"/api/councils/{cid}")
        check("DELETE council", st == 200 and st2 == 404)

        # ---------------- create a mock mission and watch it to done
        st, m = await api.call("POST", "/api/missions", {
            "prompt": "Build a tiny tokenizer from scratch and benchmark it", "agent_id": agents["Builder"]["id"]})
        check("POST /api/missions", st == 200 and m["status"] == "running" and m["id"].startswith("ms_")
              and m["title"].startswith("Build a tiny tokenizer"), str(m)[:200])
        mid = m["id"]
        ev = await watch.wait_for(mission_is(mid, "done", "failed"), 60)
        done = ev and ev["data"]["mission"]
        check("mock mission reaches done", done and done["status"] == "done", done and done.get("error"))
        check("result_text non-empty", done and len(done["result_text"]) > 100)
        check("stats counted", done and done["stats"]["steps"] >= 12 and done["stats"]["tool_calls"] >= 10
              and done["stats"]["prompt_tokens"] > 0 and done["stats"]["turns"] == 1, done and str(done["stats"]))
        check("todo plan mapped and finished", done and len(done["plan"]) >= 4
              and all(s["status"] == "done" for s in done["plan"]), done and str(done["plan"]))
        types = {e["type"] for e in watch.events if e.get("mission_id") == mid}
        need = {"run.started", "run.status", "run.thinking", "run.text", "run.tool_call", "run.tool_result",
                "run.final", "run.ended", "mission.updated"}
        check("full run event vocabulary", need <= types, str(need - types))
        ended = [e for e in watch.events if e.get("mission_id") == mid and e["type"] == "run.ended"]
        check("run.ended ok", ended and ended[-1]["data"]["ok"] is True and ended[-1]["data"]["exit_code"] == 0)
        st, files = await api.call("GET", f"/api/missions/{mid}/files")
        paths = [f["path"] for f in files.get("files", [])] if st == 200 else []
        check("workspace has files", len(paths) >= 2 and "plan.md" in paths, str(paths))
        st, f = await api.call("GET", f"/api/missions/{mid}/file?path=plan.md")
        check("file endpoint returns text", st == 200 and f.get("text", "").startswith("# Plan") and f["truncated"] is False)
        st, f = await api.call("GET", f"/api/missions/{mid}/file?path=tokenizer.py")
        check("generated code is readable", st == 200 and "def train" in f.get("text", ""))

        # ---------------- file endpoint rejects traversal
        for bad in ("../state.json", "..%2F..%2Fstate.json", "/etc/passwd", "..\\..\\state.json",
                    "C:\\Windows\\win.ini", "sub/../../x"):
            st, body = await api.call("GET", f"/api/missions/{mid}/file?path={bad}")
            check(f"file rejects {bad!r}", st == 400 and "error" in body, f"{st} {body}")
        st, body = await api.call("GET", f"/api/missions/{mid}/file?path=nope.txt")
        check("missing file -> 404", st == 404 and "error" in body)

        # ---------------- events endpoints
        st, body = await api.call("GET", f"/api/missions/{mid}/events?since=0&limit=5")
        check("mission events endpoint paginates", st == 200 and len(body["events"]) == 5 and body["more"] is True
              and all(e["mission_id"] == mid for e in body["events"]))
        st, body = await api.call("GET", "/api/events?since=0&limit=10")
        check("all events endpoint", st == 200 and [e["seq"] for e in body["events"]] == list(range(1, 11)))

        # ---------------- note on a finished mission resumes it
        mark = len(watch.events)
        st, m = await api.call("POST", f"/api/missions/{mid}/note", {"text": "use a bigger sample"})
        check("note on finished mission -> running", st == 200 and m["status"] == "running", str(m)[:200])
        ev = await watch.wait_for(mission_is(mid, "done", "failed", runs=2), 60, start=mark)
        m2 = ev and ev["data"]["mission"]
        check("note delivered by a resumed run", m2 and m2["status"] == "done" and m2["runs"] == 2
              and m2["notes"][0]["delivered"] is True, m2 and str(m2["notes"]))
        started = [e for e in watch.events[mark:] if e["type"] == "run.started" and e["mission_id"] == mid]
        check("resumed run reuses the session", started and started[0]["data"]["resumed"] is True
              and started[0]["data"]["session_id"] == done["session_id"])
        check("human.note event", any(e["type"] == "human.note" and e["mission_id"] == mid for e in watch.events))
        check("follow-up result quotes the note", m2 and "use a bigger sample" in m2["result_text"])
        mark = len(watch.events)
        st, m = await api.call("POST", f"/api/missions/{mid}/resume", {"text": "add a memory number"})
        check("resume -> running", st == 200 and m["status"] == "running")
        st, body = await api.call("POST", f"/api/missions/{mid}/resume", {})
        check("resume while running -> 409", st == 409 and "error" in body)
        ev = await watch.wait_for(mission_is(mid, "done", "failed", runs=3), 60, start=mark)
        check("explicit resume done", ev and ev["data"]["mission"]["status"] == "done"
              and "add a memory number" in ev["data"]["mission"]["result_text"])

        # ---------------- interrupt note on a running mission
        st, m = await api.call("POST", "/api/missions", {"prompt": "Survey tokenizer designs", "agent_id": agents["Scout"]["id"]})
        mid2 = m["id"]
        await watch.wait_for(lambda e: e.get("mission_id") == mid2 and e["type"] == "run.tool_call", 30)
        mark = len(watch.events)
        st, m = await api.call("POST", f"/api/missions/{mid2}/note", {"text": "focus on byte-level BPE", "interrupt": True})
        check("interrupt note accepted", st == 200)
        ev = await watch.wait_for(mission_is(mid2, "done", "failed", "stopped", runs=2), 60, start=mark)
        mm = ev and ev["data"]["mission"]
        ends = [e for e in watch.events if e.get("mission_id") == mid2 and e["type"] == "run.ended"]
        check("interrupt: run 1 stopped, run 2 resumed with the note", mm and mm["status"] == "done"
              and mm["runs"] == 2 and len(ends) == 2 and ends[0]["data"]["ok"] is False
              and "byte-level BPE" in mm["result_text"], mm and f"{mm['status']} runs={mm['runs']} ends={len(ends)}")

        # ---------------- stop
        st, m = await api.call("POST", "/api/missions", {"prompt": "Review the tokenizer", "agent_id": agents["Critic"]["id"]})
        mid3 = m["id"]
        await watch.wait_for(lambda e: e.get("mission_id") == mid3 and e["type"] == "run.status", 30)
        st, m = await api.call("POST", f"/api/missions/{mid3}/stop")
        check("stop -> stopped", st == 200 and m["status"] == "stopped", str(m)[:200])
        check("human.stopped event", any(e["type"] == "human.stopped" and e["mission_id"] == mid3 for e in watch.events))

        # ---------------- plan approval (mock model)
        st, m = await api.call("POST", "/api/missions", {
            "prompt": "Build a CSV deduplicator", "agent_id": agents["Builder"]["id"], "require_plan_approval": True})
        mid4 = m["id"]
        check("plan approval starts in planning", st == 200 and m["status"] == "planning")
        ev = await watch.wait_for(lambda e: e["type"] == "plan.proposed" and e["mission_id"] == mid4, 20)
        check("plan.proposed with 4-6 steps", ev and 4 <= len(ev["data"]["steps"]) <= 6, ev and str(ev["data"]))
        st, m = await api.call("GET", f"/api/missions/{mid4}")
        check("mission awaiting_approval", m["status"] == "awaiting_approval" and len(m["plan"]) >= 4)
        edited = ["Edited step one", "Edited step two", "Edited step three"]
        st, m = await api.call("POST", f"/api/missions/{mid4}/approve", {"plan": edited})
        check("approve -> running", st == 200 and m["status"] == "running" and [s["text"] for s in m["plan"]] == edited)
        st, body = await api.call("POST", f"/api/missions/{mid4}/approve", {})
        check("approve twice -> 409", st == 409)
        ev = await watch.wait_for(mission_is(mid4, "done", "failed"), 60)
        mm = ev and ev["data"]["mission"]
        check("approved plan drives the run", mm and mm["status"] == "done"
              and [s["text"] for s in mm["plan"]] == edited and all(s["status"] == "done" for s in mm["plan"]),
              mm and str(mm["plan"]))
        check("human.approved event", any(e["type"] == "human.approved" and e["mission_id"] == mid4 for e in watch.events))

        # ---------------- Talk with a mock model
        st, t = await api.call("GET", f"/api/threads/{lead_thread}")
        check("GET thread has messages", st == 200 and isinstance(t.get("messages"), list) and len(t["messages"]) == 1)
        mark = len(watch.events)
        os.environ["SABHA_MOCK_SPEED"] = "4"  # slow enough that the reply is still streaming
        st, r = await api.call("POST", f"/api/threads/{lead_thread}/messages", {"content": "hello Lead, what should we build first?"})
        check("post message", st == 200 and r["user"]["role"] == "user" and r["assistant_id"].startswith("msg_"))
        aid = r["assistant_id"]
        await watch.wait_for(lambda e: e["type"] == "chat.delta" and e["data"].get("message_id") == aid, 10, start=mark)
        os.environ["SABHA_MOCK_SPEED"] = "25"
        st, body = await api.call("POST", f"/api/threads/{lead_thread}/messages", {"content": "again"})
        check("second message while busy -> 409", st == 409, f"{st} {body}")
        msg = await watch.wait_for(lambda e: e["type"] == "thread.message" and e["data"]["message"]["id"] == aid, 30, start=mark)
        deltas = [e for e in watch.events[mark:] if e["type"] == "chat.delta" and e["data"]["message_id"] == aid]
        check("chat.delta stream (ephemeral, with thread_id)", len(deltas) > 10 and all(e["seq"] == 0 for e in deltas)
              and all(e["thread_id"] == lead_thread for e in deltas)
              and any("thinking" in e["data"] for e in deltas) and any("text" in e["data"] for e in deltas))
        check("chat.done before thread.message", any(e["type"] == "chat.done" and e["data"]["message_id"] == aid
                                                     for e in watch.events[mark:]))
        content = msg and msg["data"]["message"]["content"]
        check("mock reply is context-aware", content and "Lead" in content and "demo model" in content
              and msg["data"]["message"]["thinking"], content)
        # stop mid-reply keeps the partial text
        mark = len(watch.events)
        os.environ["SABHA_MOCK_SPEED"] = "1"
        st, r = await api.call("POST", f"/api/threads/{lead_thread}/messages", {"content": "tell me a long story please"})
        await watch.wait_for(lambda e: e["type"] == "chat.delta" and "text" in e["data"]
                             and e["data"].get("message_id") == r["assistant_id"], 15, start=mark)
        os.environ["SABHA_MOCK_SPEED"] = "25"
        st, body = await api.call("POST", f"/api/threads/{lead_thread}/stop")
        msg = await watch.wait_for(lambda e: e["type"] == "thread.message" and e["data"]["message"]["id"] == r["assistant_id"], 10, start=mark)
        partial = msg and msg["data"]["message"]["content"]
        check("thread stop keeps partial reply", st == 200 and partial and "no GPU behind it" not in partial, partial)
        st, t = await api.call("GET", f"/api/threads/{lead_thread}")
        check("thread not busy after stop", t["busy"] is False)

        # ---------------- mission from Talk hits back when done
        mark = len(watch.events)
        st, m = await api.call("POST", "/api/missions", {"prompt": "Plan a tokenizer project", "agent_id": agents["Lead"]["id"],
                                                         "thread_id": lead_thread})
        mid5 = m["id"]
        card = await watch.wait_for(lambda e: e["type"] == "thread.message" and e["data"]["message"].get("mission_id") == mid5, 10, start=mark)
        check("dispatch card in thread", card and card["data"]["message"]["role"] == "system"
              and card["data"]["message"]["content"].startswith("Mission dispatched:"))
        card2 = await watch.wait_for(lambda e: e["type"] == "thread.message" and e["data"]["message"].get("mission_id") == mid5
                                     and e["data"]["message"]["content"].startswith("Mission done:"), 60, start=mark)
        check("final card in thread when the mission ends", card2 is not None)

        # ---------------- real OpenAI-compatible model (fake server)
        st, fm = await api.call("POST", "/api/models", {"name": "Fake-LLM", "kind": "chat", "base_url": fake_url + "/",
                                                        "served_name": "fake-llm", "context_window": 8192})
        check("POST /api/models", st == 200 and fm["status"] == "unknown" and fm["base_url"] == fake_url)
        st, fm = await api.call("POST", f"/api/models/{fm['id']}/probe")
        check("probe -> ready", st == 200 and fm["status"] == "ready", str(fm))
        st, echo = await api.call("POST", "/api/agents", {"name": "Echo", "role": "answers", "persona": "You are Echo.",
                                                          "model_id": fm["id"], "harness": "chat", "hue": "rose"})
        check("POST /api/agents", st == 200 and echo["harness"] == "chat")
        st, t2 = await api.call("POST", "/api/threads", {"agent_id": echo["id"]})
        check("POST /api/threads", st == 200 and t2["messages"] == [] and t2["busy"] is False)
        mark = len(watch.events)
        st, r = await api.call("POST", f"/api/threads/{t2['id']}/messages", {"content": "hi"})
        msg = await watch.wait_for(lambda e: e["type"] == "thread.message" and e["data"]["message"]["id"] == r["assistant_id"], 15, start=mark)
        mm = msg and msg["data"]["message"]
        check("SSE stream: content and thinking split (incl. inline <think>)", mm
              and mm["content"] == "Hello from the fake model."
              and "Thinking about it." in mm["thinking"] and "inline thought" in mm["thinking"], mm and str(mm))
        sent = FAKE_SEEN[-1] if FAKE_SEEN else {}
        check("chat request: stream + Talk system prompt (persona, memory) + tools + history", sent.get("stream") is True
              and sent["messages"][0]["role"] == "system" and "You are Echo" in sent["messages"][0]["content"]
              and "Your own angle: You are Echo." in sent["messages"][0]["content"]
              and "WHAT THE BOX REMEMBERS" in sent["messages"][0]["content"]
              and [t["function"]["name"] for t in sent.get("tools") or []] == ["search_memory", "read_memory", "web_search", "read_page"]
              and sent["messages"][-1] == {"role": "user", "content": "hi"} and sent.get("model") == "fake-llm", str(sent)[:300])
        st, m = await api.call("POST", "/api/missions", {"prompt": "Say hello", "agent_id": echo["id"]})
        ev = await watch.wait_for(mission_is(m["id"], "done", "failed"), 20)
        check("chat harness mission done", ev and ev["data"]["mission"]["status"] == "done"
              and ev["data"]["mission"]["result_text"] == "Hello from the fake model.", ev and str(ev["data"]["mission"])[:300])
        st, m = await api.call("POST", "/api/missions", {"prompt": "Build X", "agent_id": echo["id"], "require_plan_approval": True})
        ev = await watch.wait_for(lambda e: e["type"] == "plan.proposed" and e["mission_id"] == m["id"], 20)
        check("plan from a real model (JSON array parsed)", ev and [s["text"] for s in ev["data"]["steps"]]
              == ["Read the brief", "Write the code", "Test it"], ev and str(ev["data"]))
        await api.call("POST", f"/api/missions/{m['id']}/stop")
        st, dead = await api.call("POST", "/api/models", {"name": "Dead", "kind": "chat", "base_url": f"http://127.0.0.1:{free_port()}/v1"})
        st, ghost = await api.call("POST", "/api/agents", {"name": "Ghost", "model_id": dead["id"], "harness": "chat"})
        st, t3 = await api.call("POST", "/api/threads", {"agent_id": ghost["id"]})
        mark = len(watch.events)
        st, r = await api.call("POST", f"/api/threads/{t3['id']}/messages", {"content": "anyone there?"})
        msg = await watch.wait_for(lambda e: e["type"] == "thread.message" and e["data"]["message"]["id"] == r["assistant_id"], 15, start=mark)
        check("unreachable model -> plain error message", msg and msg["data"]["message"]["content"].startswith("Could not get a reply"),
              msg and msg["data"]["message"]["content"])

        # ---------------- model launcher
        st, lm = await api.call("POST", "/api/models", {"name": "Launched", "kind": "chat", "base_url": fake_url})
        cmd = f'"{sys.executable}" -u -c "import time; print(\'server booting\'); print(\'line two\'); time.sleep(60)"'
        mark = len(watch.events)
        st, lm2 = await api.call("POST", f"/api/models/{lm['id']}/launch", {"cmd": cmd, "env": {"FOO": "1"}})
        check("launch -> starting with pid", st == 200 and lm2["status"] == "starting" and lm2["launch"]["pid"], str(lm2))
        logline = await watch.wait_for(lambda e: e["type"] == "model.log" and e["data"]["line"] == "server booting", 10, start=mark)
        check("model.log streams launch output", logline is not None and logline["seq"] == 0)
        starting = await watch.wait_for(lambda e: e["type"] == "model.updated" and e["data"]["model"]["id"] == lm["id"]
                                        and e["data"]["model"]["status"] == "starting", 5, start=mark)
        after = watch.events.index(starting) + 1 if starting else len(watch.events)
        ready = await watch.wait_for(lambda e: e["type"] == "model.updated" and e["data"]["model"]["id"] == lm["id"]
                                     and e["data"]["model"]["status"] == "ready", 15, start=after)
        check("launched model probes ready", ready is not None)
        st, lg = await api.call("GET", f"/api/models/{lm['id']}/log")
        check("GET model log", st == 200 and "server booting" in lg["lines"] and "line two" in lg["lines"], str(lg)[:200])
        pid = lm2["launch"]["pid"]
        st, lm3 = await api.call("POST", f"/api/models/{lm['id']}/stop")
        check("stop launched model -> down", st == 200 and lm3["status"] == "down")
        check("launched process is gone", not pid_alive(pid), f"pid {pid}")

        # ---------------- fake dsh adapter
        st, dm = await api.call("POST", "/api/models", {"name": "Dsh-LLM", "kind": "chat", "base_url": fake_url, "served_name": "fake-llm"})
        st, da = await api.call("POST", "/api/agents", {"name": "Diver", "role": "codes", "persona": "PERSONA-MARK",
                                                        "model_id": dm["id"], "harness": "dsh"})
        st, m = await api.call("POST", "/api/missions", {"prompt": "do the dsh thing", "agent_id": da["id"]})
        dmid = m["id"]
        ev = await watch.wait_for(mission_is(dmid, "done", "failed"), 30)
        dmm = ev and ev["data"]["mission"]
        check("dsh mission done", dmm and dmm["status"] == "done", dmm and dmm.get("error"))
        dev = [e for e in watch.events if e.get("mission_id") == dmid]
        started = [e for e in dev if e["type"] == "run.started"]
        check("dsh session -> run.started", started and started[0]["data"]["session_id"] == "sess-fake-1"
              and started[0]["data"]["harness"] == "dsh" and started[0]["data"]["resumed"] is False)
        text = next((e["data"]["text"] for e in dev if e["type"] == "run.text"), "")
        check("dsh invoked correctly (args, patch.yml, env)", "PROBLEMS<<>>" in text, text[-200:])
        check("persona prepended to first task, task on stdin", "PERSONA-MARK" in text and "do the dsh thing" in text)
        check("dsh thinking passes extra fields", any(e["type"] == "run.thinking" and e["data"].get("truncated") is False for e in dev))
        check("dsh tool_call/tool_result mapped", any(e["type"] == "run.tool_call" and e["data"]["call_id"] == "c1" for e in dev)
              and any(e["type"] == "run.tool_result" and e["data"]["status"] == "error" and e["data"]["error"] == "exit 1" for e in dev))
        check("dsh todo -> plan", dmm and [(s["text"], s["status"]) for s in dmm["plan"]]
              == [("first", "done"), ("second", "doing"), ("third", "todo")], dmm and str(dmm["plan"]))
        check("dsh stderr -> run.stderr", any(e["type"] == "run.stderr" and "fake harness" in e["data"]["line"] for e in dev))
        check("unknown dsh event -> run.raw", any(e["type"] == "run.raw" and e["data"]["raw"].get("type") == "mystery" for e in dev))
        final = [e for e in dev if e["type"] == "run.final"]
        check(">100 KB final line read whole", final and len(final[0]["data"]["text"]) > 150000
              and final[0]["data"]["reason"] == {"kind": "completed"})
        check("result_text holds the big final", dmm and len(dmm["result_text"]) > 150000 and dmm["session_id"] == "sess-fake-1")
        check("dsh usage counted", dmm and dmm["stats"]["prompt_tokens"] == 1000 and dmm["stats"]["completion_tokens"] == 50
              and dmm["stats"]["tool_errors"] == 1, dmm and str(dmm["stats"]))
        st, files = await api.call("GET", f"/api/missions/{dmid}/files")
        check("dsh ran in the workspace", st == 200 and any(f["path"] == "from_fake_dsh.txt" for f in files["files"]))
        check("patch.yml written per run", (data_dir / "runs" / f"{dmid}-r1" / "patch.yml").is_file())
        mark = len(watch.events)
        st, m = await api.call("POST", f"/api/missions/{dmid}/resume", {"text": "second pass"})
        ev = await watch.wait_for(mission_is(dmid, "done", "failed", runs=2), 30, start=mark)
        text2 = next((e["data"]["text"] for e in watch.events[mark:] if e["type"] == "run.text" and e["mission_id"] == dmid), "")
        check("dsh resume uses --session-id, no persona", bool(ev) and ev["data"]["mission"]["result_text"].startswith("RESUMED")
              and "--session-id sess-fake-1" in text2 and "PERSONA-MARK" not in text2
              and "second pass" in text2, text2[:300])
        # stop a slow dsh run: the process tree must die
        st, m = await api.call("POST", "/api/missions", {"prompt": "SLOW job", "agent_id": da["id"]})
        smid = m["id"]
        await watch.wait_for(lambda e: e.get("mission_id") == smid and e["type"] == "run.started", 20)
        t_stop = time.monotonic()
        st, m = await api.call("POST", f"/api/missions/{smid}/stop")
        took = time.monotonic() - t_stop
        ended = [e for e in watch.events if e.get("mission_id") == smid and e["type"] == "run.ended"]
        check("stop kills a running dsh tree", st == 200 and m["status"] == "stopped" and took < 12
              and ended and ended[0]["data"]["ok"] is False, f"{m.get('status')} took={took:.1f}s")

        # ---------------- dsh missing
        os.environ["PATH"] = old_path
        if shutil.which("dsh") is None:
            st, m = await api.call("POST", "/api/missions", {"prompt": "no dsh here", "agent_id": da["id"]})
            ev = await watch.wait_for(mission_is(m["id"], "done", "failed"), 15)
            check("missing dsh -> failed with install hint", ev and ev["data"]["mission"]["status"] == "failed"
                  and "npm i -g @deepseek-ai/dsh" in ev["data"]["mission"]["error"], ev and ev["data"]["mission"]["error"])
        else:
            print("SKIP  missing-dsh check (a real dsh is on PATH)")
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path

        # ---------------- validation errors
        st, body = await api.call("POST", "/api/missions", raw="{not json")
        check("bad JSON -> 400", st == 400 and "error" in body)
        st, body = await api.call("POST", "/api/missions", {"agent_id": agents["Lead"]["id"]})
        check("missing prompt -> 400", st == 400 and "error" in body)
        st, body = await api.call("POST", "/api/missions", {"prompt": "x", "agent_id": "a_nope"})
        check("unknown agent -> 400", st == 400)
        st, body = await api.call("GET", "/api/missions/ms_nope")
        check("unknown mission -> 404 JSON", st == 404 and body == {"error": "mission not found"})
        st, body = await api.call("PATCH", f"/api/models/{fm['id']}", {"kind": "banana"})
        check("bad model kind -> 400", st == 400)
        st, body = await api.call("GET", "/api/nope")
        check("unknown API route -> 404 JSON", st == 404 and "error" in body)
        st, body = await api.call("PATCH", f"/api/agents/{echo['id']}", {"hue": "mint", "role": "repeats"})
        check("PATCH agent", st == 200 and body["hue"] == "mint" and body["role"] == "repeats")
        st, body = await api.call("DELETE", f"/api/missions/{mid3}")
        check("DELETE mission", st == 200 and body == {"ok": True} and not (data_dir / "workspaces" / mid3).exists())
        st, body = await api.call("DELETE", f"/api/threads/{t3['id']}")
        check("DELETE thread", st == 200 and body == {"ok": True})
        st, body = await api.call("DELETE", f"/api/agents/{ghost['id']}")
        check("DELETE agent", st == 200)
        st, body = await api.call("DELETE", f"/api/models/{dead['id']}")
        check("DELETE model", st == 200)
        await watch.wait_for(lambda e: e["type"] == "model.removed" and e["data"]["id"] == dead["id"], 5)
        check("removal events", {"mission.removed", "thread.removed", "agent.removed", "model.removed"}
              <= {e["type"] for e in watch.events})

        # ---------------- restart: state and seq survive, running -> stopped
        head_before = (await api.call("GET", "/api/state"))[1]["seq_head"]
        await ws.close()
        pump.cancel()
        ws = None
        # ---------------- box: boot strip, settings, taste, research runs started from the app (mock daemon in demo)
        st, b = await api.call("GET", "/api/boot")
        check("boot: no launcher -> present false, ready", st == 200 and b["present"] is False and b["ready"] is True, str(b)[:200])
        (data_dir / "boot.json").write_text(json.dumps({"epoch": time.time(), "ready": False, "stage": "models", "gpus": [{"index": 0}],
                                                       "steps": [{"id": "tools", "status": "done"},
                                                                 {"id": "model:reader", "status": "starting", "secs": 60, "expect_s": 600}]}),
                                            encoding="utf-8")
        st, b = await api.call("GET", "/api/boot")
        check("boot: launcher file is served", st == 200 and b["present"] and not b["ready"] and b["alive"] and len(b["steps"]) == 2, str(b)[:200])
        st, s = await api.call("GET", "/api/settings")
        check("settings: no keys", st == 200 and s["tavily_key"]["set"] is False and s["git_sync"] is False, str(s)[:200])
        st, s = await api.call("PUT", "/api/settings", {"tavily_key": "tvly-abcdefghijkl1234"})
        check("settings: key stored, only a hint comes back", st == 200 and s["tavily_key"]["set"] and s["tavily_key"]["hint"].endswith("1234")
              and "abcdefghijkl" not in json.dumps(s), str(s)[:200])
        check("settings: key reaches the environment", os.environ.get("TAVILY_API_KEY") == "tvly-abcdefghijkl1234")
        st, s = await api.call("PUT", "/api/settings", {"tavily_key": ""})
        check("settings: key cleared", st == 200 and s["tavily_key"]["set"] is False and "TAVILY_API_KEY" not in os.environ, str(s)[:200])
        st, s = await api.call("PUT", "/api/settings", {"git_repo": "not a repo"})
        check("settings: bad git_repo -> 400", st == 400, str(s)[:200])
        st, t = await api.call("PUT", "/api/taste", {"taste": "I trust ablations over leaderboards. Skip surveys.",
                                                    "loves": "https://arxiv.org/abs/2301.08243v2\nA paper title I like",
                                                    "topic": "Which world models help planning?", "hours": 0.5})
        check("taste: saved, arXiv id pulled out of a link", st == 200 and t["loves"] == ["2301.08243", "A paper title I like"]
              and t["hours"] == 0.5, str(t)[:200])
        st, t = await api.call("GET", "/api/taste")
        check("taste: survives a reload", st == 200 and t["taste"].startswith("I trust"), str(t)[:200])
        st, r = await api.call("POST", "/api/research/start", {"topic": "short"})
        check("research: a too-short question -> 400", st == 400, str(r)[:200])
        st, r = await api.call("POST", "/api/research/start", {"topic": "Which world models actually help a policy plan?"})
        check("research: start (mock daemon)", st == 200 and r["running"] and r["mock"] and r["mission"], str(r)[:300])
        rmid = r.get("mission", "") if isinstance(r, dict) else ""
        st, _ = await api.call("POST", "/api/research/start", {})
        check("research: one run at a time -> 409", st == 409)
        mroot = data_dir / "research" / "missions" / rmid
        check("research: brief carries the taste and the question", (mroot / "brief.md").is_file()
              and "I trust ablations" in (mroot / "brief.md").read_text("utf-8") and (mroot / "taste.md").is_file()
              and json.loads((mroot / "loves.json").read_text("utf-8")) == ["2301.08243"])
        ov = {}
        for _ in range(60):
            st, ov = await api.call("GET", "/api/research")
            if st == 200 and ((ov.get("mission") or {}).get("stats") or {}).get("cards", 0) >= 2:
                break
            await asyncio.sleep(0.5)
        check("research: overview shows the run and live cards", (ov.get("run") or {}).get("running")
              and (ov.get("mission") or {}).get("id") == rmid and ov["mission"]["stats"]["cards"] >= 2
              and len(ov.get("agents") or []) == 2, str(ov.get("run"))[:300])
        st, r = await api.call("POST", "/api/research/stop", {"mode": "finish"})
        check("research: finish accepted", st == 200 and r["finishing"], str(r)[:200])
        for _ in range(40):
            st, r = await api.call("GET", "/api/research")
            if not r["run"]["running"]:
                break
            await asyncio.sleep(0.5)
        check("research: daemon ends after finish", not r["run"]["running"] and r["run"]["ended"], str(r["run"])[:300])
        check("research: synthesis written", (mroot / "synthesis.md").is_file())
        async with http.get(f"{base}/api/research/export/{rmid}?token={TOKEN}") as resp:
            blob = await resp.read()
            zip_ok = resp.status == 200 and resp.content_type == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist() if zip_ok else []
        check("research: export zip has taste, cards, synthesis", zip_ok and f"{rmid}/synthesis.md" in names
              and f"{rmid}/taste.md" in names and any(n.startswith(f"{rmid}/cards/") for n in names), str(names[:8]))
        st, _ = await api.call("GET", "/api/research/export/no-such-mission")
        check("research: export of an unknown mission -> 404", st == 404)
        # ---------------- memory: search and read what the box did, and Talk using it through a tool call
        st, mem = await api.call("GET", "/api/memory/search?q=joint%20embedding%20predictive")
        hit = next((x for x in (mem.get("hits") or []) if x["id"].startswith("card:")), None) if st == 200 else None
        check("memory: search finds the research card", hit is not None and "2301.08243" in hit["id"], str(mem)[:300])
        st, doc = await api.call("GET", f"/api/memory/doc?id={hit['id'] if hit else 'x'}")
        check("memory: read a document", st == 200 and doc["parts"] >= 1 and "joint-embedding" in doc["text"].lower(), str(doc)[:200])
        st, _ = await api.call("GET", "/api/memory/doc?id=no-such-doc")
        check("memory: unknown id -> 404", st == 404)
        st, mm_model = await api.call("POST", "/api/models", {"name": "Fake-Memory", "kind": "chat", "base_url": fake_url,
                                                              "served_name": "fake-llm", "context_window": 8192})
        st, mm_agent = await api.call("POST", "/api/agents", {"name": "Rememberer", "role": "answers from memory",
                                                              "model_id": mm_model["id"], "harness": "chat", "hue": "sky"})
        st, t3 = await api.call("POST", "/api/threads", {"agent_id": mm_agent["id"]})
        st, r3 = await api.call("POST", f"/api/threads/{t3['id']}/messages", {"content": "MEMORYTEST what do we know about JEPA?"})
        mm = {}
        for _ in range(60):  # read the thread itself (the socket watcher is not listening at this point of the test)
            st, tt = await api.call("GET", f"/api/threads/{t3['id']}")
            mm = next((x for x in tt.get("messages") or [] if x.get("id") == r3.get("assistant_id")), {}) if st == 200 else {}
            if mm and not tt.get("busy"):
                break
            await asyncio.sleep(0.3)
        steps = mm.get("steps") or []
        check("talk: the agent searched memory and answered from the result", mm.get("content", "").startswith("Found it:")
              and steps and steps[0]["tool"] == "search_memory" and steps[0]["result"].endswith("found")
              and steps[0]["result"] != "nothing found", str(mm)[:500])
        st, r = await api.call("POST", "/api/research/start", {"topic": "A second question about planning with models?", "hours": 0.5})
        check("research: a second run starts after the first ended", st == 200 and r["running"], str(r)[:200])
        st, r = await api.call("POST", "/api/research/stop", {"mode": "now"})
        check("research: stop now", st == 200 and not r["running"] and r["how"] == "stopped from the app", str(r)[:200])

        await runner.cleanup()
        runner = None
        state_file = data_dir / "state.json"
        saved = json.loads(state_file.read_text("utf-8"))
        victim = saved["missions"][0]["id"]
        saved["missions"][0]["status"] = "running"
        state_file.write_text(json.dumps(saved), encoding="utf-8")
        runner = await start_hub(data_dir, port)
        st, state2 = await api.call("GET", "/api/state")
        check("restart: no re-seed, entities kept", len(state2["agents"]) == len(saved["agents"])
              and len(state2["missions"]) == len(saved["missions"]))
        vm = next(x for x in state2["missions"] if x["id"] == victim)
        check("restart: running mission -> stopped 'hub restarted'", vm["status"] == "stopped" and vm["error"] == "hub restarted")
        check("restart: seq continues", state2["seq_head"] >= head_before)
        st, t = await api.call("GET", f"/api/threads/{lead_thread}")
        check("restart: thread messages persisted", st == 200 and len(t["messages"]) >= 5 and t["busy"] is False)
        ws = await http.ws_connect(f"{base}/ws?token={TOKEN}&since={head_before - 3}")
        hello = await ws.receive_json(timeout=10)
        nxt = [await ws.receive_json(timeout=10) for _ in range(3)]
        check("restart: ws replay from since", hello["type"] == "hello"
              and [e["seq"] for e in nxt] == [head_before - 2, head_before - 1, head_before], str([e.get("seq") for e in nxt]))
        await ws.close()
        ws = None
    finally:
        if pump is not None:
            pump.cancel()
        if ws is not None:
            await ws.close()
        await http.close()
        if runner is not None:
            await runner.cleanup()
        await fake_runner.cleanup()
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED'} in {time.monotonic() - t0:.1f}s", flush=True)
    for name in FAILS:
        print(f"  failed: {name}")
    return 0 if not FAILS else 1


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
