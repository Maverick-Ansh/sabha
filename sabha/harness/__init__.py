"""Harness adapters: dsh (DeepSeek Harness), mock (scripted demo), chat (one completion)."""

from __future__ import annotations

from .base import Harness, RunContext

NAMES = ("dsh", "mock", "chat")


def get_harness(name: str) -> Harness | None:
    if name == "dsh":
        from .dsh import DshHarness
        return DshHarness()
    if name == "mock":
        from .mock import MockHarness
        return MockHarness()
    if name == "chat":
        from .chat import ChatHarness
        return ChatHarness()
    return None


__all__ = ["Harness", "RunContext", "NAMES", "get_harness"]
