"""What the research agents do, step by step: one JSON line per model answer, search, paper fetch, card and wave.

The daemon binds it to missions/<id>/state/trace.jsonl at mission start. The hub (sabha/research_feed.py) tails
the file and replays each line as the same run.* events a crew agent's harness produces, under the agent id
'r:<track>' ('r:team' for run-wide steps), so the app's live stream, its filters, the timeline ticks and the orbit's
sparks show research the way they show the crew.

    think   a model's answer (text = its output, step = what it was for: gate, map, reduce, scouting, wave chapter ...)
    call    a tool call (tool, input): an arXiv / index / web search, a paper fetch
    result  that tool's result (tool, text)
    text    a milestone: a card written, a paper skipped, a wave closed, the plan changed
    final   the run's answer is written
    error   something failed (text)

Context (track, paper, title, step) comes from the calling thread's live slot (research/live.py). Never raises.
"""
from __future__ import annotations

import json
import threading
import time

from . import live

_path = ""
_lock = threading.Lock()
MAX_TEXT = 1200


def bind(path: str) -> None:
    global _path
    _path = path


def emit(kind: str, text: str = "", **fields) -> None:
    if not _path:
        return
    try:
        cur = live.current()
        rec = {"t": round(time.time(), 2), "kind": kind,
               "track": fields.pop("track", None) or cur.get("track"),
               "paper": fields.pop("paper", None) or cur.get("paper"),
               "title": (fields.pop("title", None) or cur.get("title") or "")[:160],
               "step": fields.pop("step", None) or cur.get("step"),
               "slot": live.key(), "text": str(text or "")[:MAX_TEXT], **fields}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _lock, open(_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # tracing must never break research
        pass
