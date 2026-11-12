"""Harness adapter interface (PROTOCOL.md section 6).

An adapter has one coroutine, `run(ctx)`. It emits run events through
`ctx.emit(type, data)`, watches `ctx.stop_event`, sets `ctx.exit_code`, and
returns once its process (if any) is gone. The mission manager wraps every
run: it emits `run.ended` and decides whether the run was ok.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class RunContext:
    mission: dict
    agent: dict
    model: dict | None
    task: str  # text for this invocation (prompt, approved plan, notes...)
    cwd: str  # the mission workspace
    session_id: str  # harness session to resume; "" for a first run
    emit: Callable[[str, dict], None]
    stop_event: asyncio.Event
    run: int = 1  # 1-based invocation number
    resumed: bool = False
    data_dir: Path = Path(".")
    http: Any = None  # aiohttp.ClientSession shared by the hub
    crew: list = field(default_factory=list)  # the other agents
    previous_result: str = ""  # result_text of the run before this one
    exit_code: int | None = None  # set by the adapter

    async def sleep(self, seconds: float) -> bool:
        """Sleep, but wake early on stop. Returns True if a stop was requested."""
        if self.stop_event.is_set():
            return True
        try:
            await asyncio.wait_for(self.stop_event.wait(), max(seconds, 0.0))
            return True
        except asyncio.TimeoutError:
            return False


class Harness:
    name = "base"

    async def run(self, ctx: RunContext) -> None:
        raise NotImplementedError
