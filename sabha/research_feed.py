"""Research work in the live stream: missions/<id>/state/trace.jsonl -> run.* events (PROTOCOL.md section 12).

The research daemon writes one JSON line per model answer, search, paper fetch, card and wave (research/trace.py).
Every 2 s this task reads the new lines of the running mission's trace and emits them on the bus as the events a
crew harness produces, so the app's stream, its filters, the timeline ticks and the orbit sparks need nothing new:

    think  -> run.thinking   {text}                (prefixed with the step, e.g. "[map] ...")
    call   -> run.tool_call  {call_id, tool, input}
    result -> run.tool_result {call_id, status, result}
    text   -> run.text       {text}
    final  -> run.final      {text, reason: {kind: completed}}
    error  -> run.error      {message}

agent_id is 'r:<track>' ('r:team' for run-wide steps), mission_id 'research:<mission>'. Offsets are kept in
<data>/run/feed_offsets.json, so a hub restart never replays a line twice; a trace seen for the first time starts at
its last ~40 lines.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("sabha.research.feed")

TICK_S = 2.0
MAX_PER_TICK = 300
FIRST_TAIL = 40


class ResearchFeed:
    def __init__(self, hub):
        self.hub = hub
        self.offsets_path = Path(hub.cfg.data_dir) / "run" / "feed_offsets.json"
        try:
            self.offsets = json.loads(self.offsets_path.read_text("utf-8"))
        except (OSError, ValueError):
            self.offsets = {}
        self.calls: dict[str, str] = {}  # slot or track -> call_id of its last tool call
        self.n = 0
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="research-feed")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    def _trace_path(self) -> tuple[str, str] | None:
        st = self.hub.runner.status()
        mid = st.get("mission") or ""
        if not mid:
            return None
        p = os.path.join(self.hub.cfg.research_repo, "missions", mid, "state", "trace.jsonl")
        return (mid, p) if os.path.isfile(p) else None

    def _read_new(self) -> tuple[str, list[dict]]:
        found = self._trace_path()
        if not found:
            return "", []
        mid, p = found
        size = os.path.getsize(p)
        at = self.offsets.get(p)
        if at is None:  # first sight: show the last few steps, not the whole history
            with open(p, "rb") as f:
                f.seek(max(0, size - 24000))
                tail = f.read().split(b"\n")
            keep = b"\n".join(tail[-FIRST_TAIL - 1:])
            at = size - len(keep)
        at = min(int(at), size)
        if size == at:
            return mid, []
        with open(p, "rb") as f:
            f.seek(at)
            chunk = f.read(min(size - at, 400_000))
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return mid, []
        chunk = chunk[:last_nl + 1]
        self.offsets[p] = at + len(chunk)
        try:
            self.offsets_path.parent.mkdir(parents=True, exist_ok=True)
            self.offsets_path.write_text(json.dumps(self.offsets), encoding="utf-8")
        except OSError:
            pass
        recs = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
        return mid, recs[-MAX_PER_TICK:]

    def emit(self, mid: str, r: dict) -> None:
        bus = self.hub.bus
        agent = f"r:{r.get('track') or 'team'}"
        mission = f"research:{mid}"
        kind, text = r.get("kind"), str(r.get("text") or "")
        key = r.get("slot") or r.get("track") or "team"
        step = r.get("step") or ""
        if kind == "think":
            bus.emit("run.thinking", {"text": (f"[{step}] " if step else "") + text, "role": r.get("role"),
                                      "paper": r.get("paper"), "title": r.get("title")}, mission_id=mission, agent_id=agent)
        elif kind == "call":
            self.n += 1
            cid = f"rc{self.n}"
            self.calls[f"{key}:{r.get('tool')}"] = cid
            bus.emit("run.tool_call", {"call_id": cid, "tool": r.get("tool") or "tool", "input": {"query": r.get("input") or ""}},
                     mission_id=mission, agent_id=agent)
        elif kind == "result":
            cid = self.calls.pop(f"{key}:{r.get('tool')}", None) or f"rc{self.n}"
            bad = r.get("status") == "error"
            bus.emit("run.tool_result", {"call_id": cid, "status": "error" if bad else "completed", "result": text,
                                         **({"error": text} if bad else {})}, mission_id=mission, agent_id=agent)
        elif kind == "text":
            bus.emit("run.text", {"text": text}, mission_id=mission, agent_id=agent)
        elif kind == "final":
            bus.emit("run.final", {"text": text, "reason": {"kind": "completed"}}, mission_id=mission, agent_id=agent)
        elif kind == "error":
            bus.emit("run.error", {"message": text}, mission_id=mission, agent_id=agent)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_S)
                mid, recs = await asyncio.to_thread(self._read_new)
                for r in recs:
                    self.emit(mid, r)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("research feed: %s", e)
