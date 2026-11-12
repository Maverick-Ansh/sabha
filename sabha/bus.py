"""Event bus.

`Bus.emit` builds the protocol envelope, gives persisted events the next seq
and appends them to events.jsonl, then fans the serialised event out to every
connected WebSocket client. Ephemeral events (seq 0) skip the log.
"""

from __future__ import annotations

import asyncio
import json
import time

from .store import Store

QUEUE_MAX = 20000


class Subscriber:
    """One WebSocket client's outbox. A None in the queue means "drop me"."""

    def __init__(self, maxsize: int = QUEUE_MAX):
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize)
        self.overflowed = False

    def push(self, line: str) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(line)
        except asyncio.QueueFull:
            # The client is too slow. Empty its queue and tell the sender to
            # close; the UI reconnects with `since` and replays from the log.
            self.overflowed = True
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)


class Bus:
    def __init__(self, store: Store):
        self.store = store
        self.subscribers: set[Subscriber] = set()

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def emit(
        self,
        type_: str,
        data: dict | None = None,
        *,
        mission_id: str | None = None,
        agent_id: str | None = None,
        thread_id: str | None = None,
        ephemeral: bool = False,
    ) -> dict:
        seq = 0 if ephemeral else self.store.next_seq()
        event = {
            "seq": seq,
            "ts": round(time.time(), 3),
            "type": type_,
            "mission_id": mission_id,
            "agent_id": agent_id,
            "thread_id": thread_id,
            "data": data if data is not None else {},
        }
        line = json.dumps(event, ensure_ascii=False, default=str)
        try:
            raw = line.encode("utf-8")
        except UnicodeEncodeError:  # lone surrogates from a harness: fall back to \u escapes
            line = json.dumps(event, ensure_ascii=True, default=str)
            raw = line.encode("utf-8")
        if not ephemeral:
            self.store.events.append(seq, mission_id, raw)
        for sub in list(self.subscribers):
            sub.push(line)
        return event
