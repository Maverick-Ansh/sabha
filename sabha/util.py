"""Small helpers shared by the hub modules: ids, API errors, a line reader that
copes with very long lines, process-tree control, and path checks."""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

IS_WINDOWS = os.name == "nt"


class ApiError(Exception):
    """An error the REST layer turns into {"error": message} with this status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def new_id(prefix: str) -> str:
    """Short id: prefix + "_" + 8 hex chars, e.g. ms_1a2b3c4d."""
    return f"{prefix}_{secrets.token_hex(4)}"


def now() -> float:
    return time.time()


def mock_speed() -> float:
    """Speed factor for the mock harness and mock chat (env SABHA_MOCK_SPEED)."""
    try:
        value = float(os.environ.get("SABHA_MOCK_SPEED") or 1.0)
    except ValueError:
        value = 1.0
    return min(max(value, 0.05), 1000.0)


def short(text: str, limit: int) -> str:
    """Collapse whitespace and cut to `limit` chars with an ellipsis."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def inside(base: Path, target: Path) -> bool:
    """True when target, after resolving, is base or below it."""
    try:
        return target.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- lines


def _decode(raw: bytes | bytearray) -> str:
    return bytes(raw).decode("utf-8", errors="replace")


async def read_lines(
    stream: asyncio.StreamReader,
    on_line: Callable[[str], None],
    *,
    split_cr: bool = False,
    chunk_size: int = 1 << 16,
) -> None:
    """Read `stream` to EOF and call on_line for each line (without the newline).

    Lines may be any length: we buffer chunks ourselves instead of using
    StreamReader.readline, which fails on lines over its 64 KiB limit.
    With split_cr, a bare "\\r" also ends a line (progress bars), and empty
    lines are skipped.
    """
    buf = bytearray()
    scan = 0  # bytes of buf already searched for a separator
    while True:
        data = await stream.read(chunk_size)
        if not data:
            break
        buf += data
        start = 0
        while True:
            i = buf.find(b"\n", max(start, scan))
            if split_cr:
                j = buf.find(b"\r", max(start, scan))
                if j != -1 and (i == -1 or j < i):
                    i = j
            if i == -1:
                break
            line = _decode(buf[start:i]).rstrip("\r")
            start = i + 1
            if line or not split_cr:
                on_line(line)
        if start:
            del buf[:start]
        scan = len(buf)
    if buf:
        line = _decode(buf).rstrip("\r")
        if line or not split_cr:
            on_line(line)


# ---------------------------------------------------------------- processes


def group_kwargs() -> dict:
    """Popen kwargs that give the child its own process group (or session)."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def kill_tree(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    """Terminate a child and everything it started, then wait for it to exit.

    Linux: the child was started in its own session, so we signal its process
    group (SIGTERM, then SIGKILL after `grace` seconds). We never signal our
    own group. Windows: `taskkill /T /F` kills the whole tree.
    """
    if IS_WINDOWS:
        if proc.returncode is None:
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/T", "/F", "/PID", str(proc.pid),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), 15)
            except (OSError, asyncio.TimeoutError):
                pass
    else:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = None
        if pgid is not None and pgid != os.getpgrp():
            _signal_group(pgid, signal.SIGTERM)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + grace
            while loop.time() < deadline and _group_alive(pgid):
                await asyncio.sleep(0.1)
            if _group_alive(pgid):
                _signal_group(pgid, signal.SIGKILL)
        elif proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass
