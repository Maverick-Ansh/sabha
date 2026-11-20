"""Every model exchange in full, for Council and Talk (the research engine writes its own, research/trace.py).

A caller marks what it is about to ask, then streams as usual:

    with recording(path, track=agent_name, step="turn", round=2):
        await stream_chat(...)

stream_chat (talk.py) sees the mark and appends one JSON line to `path` when the call ends: who asked (track), what for
(step), which model answered, the system prompt, the whole input, the answer (and the thinking), tokens, seconds, an
error. The same row shape as research's state/calls.jsonl, so one reader (index / listing / full) and one app panel
(web/js/exchanges.js, "Conversations") serve all three. Files: <data>/exchanges/<scope>/<id>.jsonl. Never raises.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import threading
import time

RECORD: contextvars.ContextVar = contextvars.ContextVar("sabha_exchange", default=None)
MAX_FULL = 60000
_lock = threading.Lock()
_next: dict[str, int] = {}
SCOPES = ("council", "talk")


def path_for(data_dir: str, scope: str, ident: str) -> str:
    if scope not in SCOPES or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", ident or ""):
        raise ValueError("bad exchange scope or id")
    return os.path.join(data_dir, "exchanges", scope, ident + ".jsonl")


@contextlib.contextmanager
def recording(path: str, **meta):
    tok = RECORD.set({"path": path, "meta": meta})
    try:
        yield
    finally:
        RECORD.reset(tok)


def _n(path: str) -> int:
    with _lock:
        if path not in _next:
            try:
                with open(path, "rb") as f:
                    _next[path] = sum(1 for _ in f)
            except OSError:
                _next[path] = 0
        _next[path] += 1
        return _next[path]


def write(model: dict, messages: list, text: str, thinking: str, usage: dict | None, t0: float, error: str = "",
          calls: list | None = None) -> None:
    rec = RECORD.get()
    if not rec:
        return
    try:
        path = rec["path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        rest = [m for m in messages if m.get("role") != "system"]
        inp = rest[-1].get("content") if len(rest) == 1 else "\n\n".join(
            f"[{m.get('role')}{' ' + m['name'] if m.get('name') else ''}]\n{m.get('content') or json.dumps(m.get('tool_calls') or '')}"
            for m in rest)
        base = (model.get("base_url") or "")
        out = {"n": _n(path), "t": round(time.time(), 2), **rec["meta"],
               "role": model.get("served_name") or "", "endpoint": model.get("name") or "", "model": model.get("served_name") or "",
               "remote": not base.startswith(("http://127.0.0.1", "http://localhost")), "attempt": 0, "json": False,
               "system": system[:MAX_FULL], "input": str(inp or "")[:MAX_FULL], "output": (text or "")[:MAX_FULL],
               "thinking": (thinking or "")[:MAX_FULL], "tok": (usage or {}).get("completion_tokens", 0),
               "prompt_tok": (usage or {}).get("prompt_tokens", 0), "secs": round(time.time() - t0, 2), "error": error[:2000],
               "tool_calls": calls or []}
        line = json.dumps(out, ensure_ascii=False, default=str)
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # recording must never break a chat
        pass


# ------------------------------------------------------------ reading (shared with research_api)

_ix: dict[str, dict] = {}


def index(path: str) -> dict:
    """Light rows (no full text) + each line's byte offset, read incrementally as the file grows."""
    ix = _ix.setdefault(path, {"size": 0, "rows": [], "off": {}})
    try:
        size = os.path.getsize(path)
    except OSError:
        return ix
    if size < ix["size"]:
        ix.update(size=0, rows=[], off={})
    if size == ix["size"]:
        return ix
    with open(path, "rb") as f:
        f.seek(ix["size"])
        pos = ix["size"]
        for raw in f:
            if not raw.endswith(b"\n"):  # a line still being written: next time
                break
            try:
                r = json.loads(raw)
            except ValueError:
                pos += len(raw)
                continue
            n = int(r.get("n") or 0)
            ix["off"][n] = pos

            def prev(s, k=220):
                return (s or "")[:k].replace("\n", " ")
            ix["rows"].append({"n": n, "t": r.get("t"), "track": r.get("track"), "paper": r.get("paper"),
                               "title": r.get("title") or "", "step": r.get("step") or "", "asked": r.get("asked") or "",
                               "role": r.get("role"), "endpoint": r.get("endpoint") or "", "remote": bool(r.get("remote")),
                               "attempt": r.get("attempt") or 0, "json": bool(r.get("json")), "tok": r.get("tok") or 0,
                               "prompt_tok": r.get("prompt_tok") or 0, "secs": r.get("secs") or 0, "round": r.get("round"),
                               "error": r.get("error") or "", "looped": bool(r.get("looped")),
                               "in_len": len(r.get("input") or ""), "out_len": len(r.get("output") or ""),
                               "input": prev(r.get("input"), 160), "output": prev(r.get("output"))})
            pos += len(raw)
        ix["size"] = pos
    return ix


def listing(path: str, before: int = 0, limit: int = 150, track: str = "", step: str = "", endpoint: str = "",
            q: str = "", errors: bool = False) -> dict:
    rows = index(path)["rows"]
    ql = q.lower().strip()
    sel = [r for r in rows if (not track or r["track"] == track) and (not step or r["step"] == step)
           and (not endpoint or r["endpoint"] == endpoint) and (not errors or r["error"])
           and (not ql or ql in (r["input"] + r["output"] + r["title"] + r["step"]).lower())]
    if before:
        sel = [r for r in sel if r["n"] < before]
    page = sel[-max(1, min(limit, 500)):]
    by_ep: dict[str, dict] = {}
    for r in rows:
        e = by_ep.setdefault(r["endpoint"] or "?", {"endpoint": r["endpoint"] or "?", "remote": r["remote"], "calls": 0,
                                                    "errors": 0, "tok": 0, "secs": 0.0})
        e["calls"] += 1
        e["errors"] += bool(r["error"])
        e["tok"] += r["tok"]
        e["secs"] += r["secs"]
    return {"total": len(rows), "matched": len(sel), "rows": page, "more": len(sel) > len(page),
            "endpoints": sorted(by_ep.values(), key=lambda x: -x["calls"]),
            "steps": sorted({r["step"] for r in rows if r["step"]}), "tracks": sorted({r["track"] for r in rows if r["track"]})}


def full(path: str, n: int) -> dict | None:
    off = index(path)["off"].get(int(n))
    if off is None:
        return None
    with open(path, "rb") as f:
        f.seek(off)
        return json.loads(f.readline())
