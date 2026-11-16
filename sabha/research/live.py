"""Live activity of the research daemon, read by the hub's Research view.

Each daemon thread owns one slot: w0..w5 are the paper workers, maint is the maintenance
thread (scouting, consolidation, reports, synthesis), seeds resolves seed papers.
A thread calls slot() once, then begin() / set() / idle() as it moves through its steps.
A flusher thread writes missions/<id>/state/live.json atomically, at most once every 2 s
and at least every 10 s (the heartbeat the hub uses to tell that the daemon is alive).

Nothing here may raise or block the pipeline: every call is a dict update under a lock,
and a thread that never called slot() (the one-paper test mode) is a no-op.

    {"ts": "...Z", "epoch": 1.7e9, "mission": "001-...", "pid": 123,
     "slots": {"w0": {"kind": "worker", "step": "map", "paper": "2504.01848", "title": "...",
                      "track": "t1", "i": 3, "n": 10, "since": 1.7e9, "ts": 1.7e9},
               "maint": {"kind": "maintenance", "step": "scouting", "track": "t2", "query": "..."}},
     "tracks": {"t1": {"weight": 1.0, "saturated": false, "paused": false}}}

Worker steps: fetch, gate, map (i of n), reduce, verify. Maintenance steps: seeds, scouting
(with the query), snowballing, consolidating, report, synthesis. Every slot rests at idle.
"""
from __future__ import annotations

import json
import os
import threading
import time

MIN_S = 2.0    # never write more often than this
BEAT_S = 10.0  # always write at least this often

_lock = threading.Lock()
_tl = threading.local()
_slots: dict[str, dict] = {}
_extra: dict = {}
_path = ""
_mission = ""
_dirty = False
_last = 0.0
_flusher: threading.Thread | None = None


def bind(path: str, mission: str = "") -> None:
    """Point the module at one mission's live.json and start the flusher (safe to call again)."""
    global _path, _mission, _dirty, _flusher
    with _lock:
        _path, _mission, _dirty = path, mission, True
        _slots.clear()
        _extra.clear()
        if _flusher is None or not _flusher.is_alive():
            _flusher = threading.Thread(target=_run, name="live-flusher", daemon=True)
            _flusher.start()


def slot(key: str, kind: str = "worker") -> None:
    """Give the calling thread its slot. Call once at the top of the thread."""
    _tl.key = key
    t = time.time()
    with _lock:
        _slots[key] = {"kind": kind, "step": "idle", "since": t, "ts": t}
        _mark()


def key() -> str | None:
    """The calling thread's slot, to hand to helper threads (see tick)."""
    return getattr(_tl, "key", None)


def begin(_key: str | None = None, **fields) -> None:
    """Start a new job in the slot: replaces everything the slot said before."""
    k = _key or key()
    if not k:
        return
    t = time.time()
    with _lock:
        kind = _slots.get(k, {}).get("kind", "worker")
        _slots[k] = {"kind": kind, **_clean(fields), "since": t, "ts": t}
        _mark()


def set(_key: str | None = None, **fields) -> None:  # noqa: A001 (the module is always used as live.set)
    """Update fields of the current job (usually the step)."""
    k = _key or key()
    if not k:
        return
    with _lock:
        cur = _slots.get(k)
        if cur is None:
            return
        cur.update(_clean(fields))
        cur["ts"] = time.time()
        _mark()


def tick(_key: str | None, field: str = "i") -> None:
    """Count one finished unit (one mapped section). Safe from pool threads: pass key()."""
    if not _key:
        return
    with _lock:
        cur = _slots.get(_key)
        if cur is None:
            return
        cur[field] = int(cur.get(field) or 0) + 1
        cur["ts"] = time.time()
        _mark()


def idle(_key: str | None = None) -> None:
    begin(_key, step="idle")


def put(name: str, value) -> None:
    """A top-level extra in live.json (the daemon posts per-track state under "tracks")."""
    with _lock:
        if _extra.get(name) != value:
            _extra[name] = value
            _mark()


def _clean(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if isinstance(v, str):
            v = v[:200]
        out[k] = v
    return out


def _mark() -> None:
    global _dirty
    _dirty = True


def _snapshot() -> str:
    return json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "epoch": round(time.time(), 1),
                       "mission": _mission, "pid": os.getpid(),
                       "slots": {k: dict(v) for k, v in _slots.items()}, **_extra},
                      ensure_ascii=False, indent=1, default=str)


def _run() -> None:
    global _dirty, _last
    while True:
        time.sleep(0.5)
        try:
            with _lock:
                age = time.time() - _last
                if not _path or not ((_dirty and age >= MIN_S) or age >= BEAT_S):
                    continue
                text, path = _snapshot(), _path
                _dirty, _last = False, time.time()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except Exception:  # a full disk or a git operation in the way: try again on the next beat
            pass
