"""Hub state and the event log.

State (models, agents, missions, threads with their messages) lives in memory
and is saved to <data>/state.json atomically (temp file + os.replace) a moment
after each change. Persisted events go to <data>/events.jsonl, one JSON object
per line. An in-memory index of (seq, mission_id, byte offset) lets the hub
replay any range of events without keeping their bodies in memory.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("sabha.store")

SAVE_DELAY_S = 0.5


class DataDirBusy(Exception):
    """Another hub process already owns this data dir."""


class DirLock:
    """An exclusive OS lock on <data>/hub.lock, held while the hub runs.

    Two hubs writing one events.jsonl would corrupt it, so the second one
    refuses to start. The OS drops the lock if the process dies.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise DataDirBusy(f"{self.path.parent} is in use by another Sabha hub "
                              "(stop it, or pass a different --data dir)") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


class EventLog:
    """Append-only events.jsonl with an offset index for replay."""

    def __init__(self, path: Path):
        self.path = path
        self.seqs: list[int] = []
        self.offsets: list[int] = []
        self.by_mission: dict[str, list[int]] = {}  # mission_id -> index positions
        self.size = 0
        self._fh = None

    def _index(self, seq: int, mission_id: str | None, offset: int) -> None:
        pos = len(self.seqs)
        self.seqs.append(seq)
        self.offsets.append(offset)
        if mission_id:
            self.by_mission.setdefault(mission_id, []).append(pos)

    def load(self) -> int:
        """Index the existing file and return the highest seq in it."""
        head = 0
        good_end = 0
        if self.path.exists():
            with open(self.path, "rb") as f:
                offset = 0
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break  # torn last line from a crash
                    try:
                        ev = json.loads(raw)
                        seq = int(ev.get("seq") or 0)
                    except (ValueError, AttributeError, TypeError):
                        ev, seq = None, 0
                    if ev is not None and seq > head:
                        self._index(seq, ev.get("mission_id"), offset)
                        head = seq
                    offset += len(raw)
                good_end = offset
            if good_end < self.path.stat().st_size:
                log.warning("events.jsonl: dropping a torn last line")
                with open(self.path, "r+b") as f:
                    f.truncate(good_end)
        self.size = good_end
        self._fh = open(self.path, "ab")
        return head

    def append(self, seq: int, mission_id: str | None, raw: bytes) -> None:
        if self._fh is None:  # after close (late events during shutdown)
            log.debug("event %s dropped: log closed", seq)
            return
        self._fh.write(raw + b"\n")
        self._fh.flush()
        end = self._fh.tell()  # the real end of file, even if something else appended
        self.size = end
        self._index(seq, mission_id, end - len(raw) - 1)

    def _read(self, positions) -> list[str]:
        out: list[str] = []
        if not positions:
            return out
        with open(self.path, "rb") as f:
            at = -1
            for pos in positions:
                off = self.offsets[pos]
                if off != at:
                    f.seek(off)
                line = f.readline()
                at = off + len(line)
                out.append(line.rstrip(b"\r\n").decode("utf-8", errors="replace"))
        return out

    def range(self, since: int, limit: int, mission_id: str | None = None) -> tuple[list[str], bool]:
        """Events with seq > since, oldest first, at most `limit`; plus a "more" flag."""
        if mission_id is not None:
            positions = self.by_mission.get(mission_id, [])
            start = bisect.bisect_right(positions, since, key=lambda p: self.seqs[p])
            chosen = positions[start:start + limit + 1]
        else:
            start = bisect.bisect_right(self.seqs, since)
            chosen = list(range(start, min(len(self.seqs), start + limit + 1)))
        more = len(chosen) > limit
        return self._read(chosen[:limit]), more

    def tail(self, since: int, limit: int, upto: int) -> list[str]:
        """Events with since < seq <= upto; if more than `limit`, the oldest are dropped."""
        start = bisect.bisect_right(self.seqs, since)
        end = bisect.bisect_right(self.seqs, upto)
        start = max(start, end - limit)
        return self._read(list(range(start, end)))

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class Store:
    """All hub entities plus the event log, with debounced atomic saving."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_path = data_dir / "state.json"
        self.events = EventLog(data_dir / "events.jsonl")
        self.models: dict[str, dict] = {}
        self.agents: dict[str, dict] = {}
        self.missions: dict[str, dict] = {}
        self.threads: dict[str, dict] = {}
        self.councils: dict[str, dict] = {}
        self.seq_head = 0
        self.had_state = False
        self._save_handle: asyncio.TimerHandle | None = None
        self._lock = DirLock(data_dir / "hub.lock")

    def load(self) -> None:
        """Lock the data dir, then read state.json and index events.jsonl."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock.acquire()
        (self.data_dir / "workspaces").mkdir(exist_ok=True)
        head = 0
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text("utf-8"))
                for key in ("models", "agents", "missions", "threads", "councils"):
                    table = getattr(self, key)
                    for item in data.get(key) or []:
                        if isinstance(item, dict) and item.get("id"):
                            table[item["id"]] = item
                head = int(data.get("seq_head") or 0)
                self.had_state = True
            except (ValueError, OSError, TypeError) as e:
                bad = self.state_path.with_name(f"state.bad-{int(time.time())}.json")
                log.error("state.json unreadable (%s); moved to %s", e, bad.name)
                try:
                    os.replace(self.state_path, bad)
                except OSError:
                    pass
        self.seq_head = max(head, self.events.load())

    def next_seq(self) -> int:
        self.seq_head += 1
        return self.seq_head

    def mark_dirty(self) -> None:
        """Ask for a save shortly; many changes in a burst become one write."""
        if self._save_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save_now()
            return
        self._save_handle = loop.call_later(SAVE_DELAY_S, self._save_cb)

    def _save_cb(self) -> None:
        self._save_handle = None
        try:
            self.save_now()
        except Exception:  # never let a failed save kill the loop
            log.exception("saving state.json failed")

    def save_now(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        data = {
            "version": 1,
            "saved_ts": time.time(),
            "seq_head": self.seq_head,
            "models": list(self.models.values()),
            "agents": list(self.agents.values()),
            "missions": list(self.missions.values()),
            "threads": list(self.threads.values()),
            "councils": list(self.councils.values()),
        }
        text = json.dumps(data, separators=(",", ":"), default=str)
        tmp = self.state_path.with_name("state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, self.state_path)
                return
            except PermissionError:  # Windows: target briefly open elsewhere
                time.sleep(0.05 * (attempt + 1))
        log.error("could not replace state.json (file locked?)")

    def close(self) -> None:
        try:
            self.save_now()
        finally:
            self.events.close()
            self._lock.release()
