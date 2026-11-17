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

A run without a trace (a daemon started before research/trace.py existed) still shows its actions: the feed then
watches state/live.json (each agent's step, paper, section and search query) and state/events.jsonl (cards,
searches, reports, waves, errors) and emits the same kinds of events from them. No model text in that case.
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
STEP_TEXT = {"fetch": "fetching the paper", "gate": "deciding whether it deserves a full read",
             "map": "reading it section by section, pulling out facts with exact quotes", "reduce": "writing the card from the verified facts",
             "verify": "checking every quote against the paper", "scouting": "planning the next searches",
             "consolidating": "folding the new cards into the claims ledger", "report": "rewriting its report",
             "synthesis": "writing the synthesis", "wave chapter": "writing this wave's chapter",
             "compacting knowledge": "compacting what the team knows", "planning next wave": "re-planning the tracks",
             "memory": "tending long-term memory", "seeds": "finding the named seed papers"}


class ResearchFeed:
    def __init__(self, hub):
        self.hub = hub
        self.offsets_path = Path(hub.cfg.data_dir) / "run" / "feed_offsets.json"
        try:
            self.offsets = json.loads(self.offsets_path.read_text("utf-8"))
        except (OSError, ValueError):
            self.offsets = {}
        self.calls: dict[str, str] = {}  # slot or track -> call_id of its last tool call
        self.seen: dict[str, tuple] = {}  # fallback: slot -> (paper, step, query, i) last seen in live.json
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

    def _mission(self) -> str:
        return self.hub.runner.status().get("mission") or ""

    def _tail(self, p: str, first_tail: int) -> list[dict]:
        """New JSON lines of p since the saved offset (first sight: its last first_tail lines)."""
        size = os.path.getsize(p)
        at = self.offsets.get(p)
        if at is None:
            with open(p, "rb") as f:
                f.seek(max(0, size - 24000))
                tail = f.read().split(b"\n")
            keep = b"\n".join(tail[-first_tail - 1:])
            at = size - len(keep)
        at = min(int(at), size)
        if size == at:
            return []
        with open(p, "rb") as f:
            f.seek(at)
            chunk = f.read(min(size - at, 400_000))
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return []
        chunk = chunk[:last_nl + 1]
        self.offsets[p] = at + len(chunk)
        out = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out[-MAX_PER_TICK:]

    def _fallback(self, mid: str) -> list[dict]:
        """No trace: trace-like records from live.json changes and new events.jsonl lines."""
        root = os.path.join(self.hub.cfg.research_repo, "missions", mid, "state")
        recs: list[dict] = []
        try:
            live = json.loads(Path(root, "live.json").read_text("utf-8"))
        except (OSError, ValueError):
            live = {}
        for slot, sl in (live.get("slots") or {}).items():
            if not isinstance(sl, dict):
                continue
            step, paper, query = sl.get("step") or "idle", sl.get("paper"), sl.get("query")
            i, n = sl.get("i"), sl.get("n")
            prev = self.seen.get(slot)
            cur = (paper, step, query, i)
            self.seen[slot] = cur
            if prev == cur or step == "idle":
                continue
            track, title = sl.get("track"), sl.get("title") or paper or ""
            if sl.get("kind") == "worker":
                if not prev or prev[0] != paper:
                    recs += [{"kind": "call", "tool": "fetch_paper", "input": f"arXiv {paper}", "track": track, "slot": slot},
                             {"kind": "result", "tool": "fetch_paper", "text": title, "track": track, "slot": slot}]
                if not prev or prev[1] != step or prev[0] != paper:
                    recs.append({"kind": "think", "step": step, "track": track, "slot": slot,
                                 "text": f"{title}: {STEP_TEXT.get(step, step)}" + (f" ({n} sections)" if step == "map" and n else "")})
                elif step == "map" and i and n and prev[3] != i:
                    recs.append({"kind": "think", "step": step, "track": track, "slot": slot, "text": f"{title}: read section {i} of {n}"})
            else:
                if step == "scouting" and query and (not prev or prev[2] != query):
                    recs.append({"kind": "call", "tool": f"{sl.get('via') or 'arxiv'}_search", "input": query, "track": track, "slot": slot})
                elif not prev or prev[1] != step:
                    recs.append({"kind": "think", "step": step, "track": track, "slot": slot,
                                 "text": STEP_TEXT.get(step, step) + (f" ({n})" if n else "")})
        evp = os.path.join(root, "events.jsonl")
        if os.path.isfile(evp):
            for e in self._tail(evp, 12):
                k, track = e.get("kind"), e.get("track")
                title = self.hub.research._title_cache(mid, e.get("id")) if e.get("id") else ""
                if k == "carded":
                    recs.append({"kind": "text", "track": track, "text": f"Wrote the card for \"{title or e.get('id')}\" "
                                 f"({e.get('grade')}, quotes {e.get('quote_pass')}, {e.get('secs')} s)"})
                elif k == "scouted":
                    recs.append({"kind": "result", "tool": "arxiv_search", "track": track,
                                 "text": f"{e.get('new', 0)} new papers seen, {e.get('rel2', 0)} worth reading"})
                elif k in ("report", "consolidated", "saturated", "replanned", "memory", "synthesis", "planned"):
                    from .research_api import _event_text
                    recs.append({"kind": "text", "track": track or "team", "text": _event_text(e, lambda a: a)})
                elif k == "wave":
                    recs.append({"kind": "text", "track": "team", "text": f"Wave {e.get('n')} closed ({e.get('cards', 0)} papers): "
                                 f"{e.get('headline', '')}"})
                elif k in ("error", "scout_fail", "search_fail"):
                    recs.append({"kind": "error", "track": track or "team", "text": str(e.get("msg") or e.get("err") or k)[:400]})
        return recs

    def _read_new(self) -> tuple[str, list[dict]]:
        found = self._trace_path()
        if not found:
            mid = self._mission()
            if not mid or not os.path.isdir(os.path.join(self.hub.cfg.research_repo, "missions", mid)):
                return "", []
            st = self.hub.runner.status()
            if not st.get("running"):
                return mid, []
            recs = self._fallback(mid)
            self._save()
            return mid, recs
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
        self._save()
        recs = []
        for line in chunk.decode("utf-8", "replace").splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue
        return mid, recs[-MAX_PER_TICK:]

    def _save(self) -> None:
        try:
            self.offsets_path.parent.mkdir(parents=True, exist_ok=True)
            self.offsets_path.write_text(json.dumps(self.offsets), encoding="utf-8")
        except OSError:
            pass

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
