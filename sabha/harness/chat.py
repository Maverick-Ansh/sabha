"""Chat harness: a mission is one plain streamed completion, no tools.

Emits run.started, turn/step statuses, run.thinking, run.text and run.final.
A resumed run sends the original prompt, the previous answer and the
follow-up text, so the model sees the whole exchange.
"""

from __future__ import annotations

import asyncio
import random
import secrets

from ..talk import ChatError, mock_reply, stream_chat
from ..util import mock_speed
from .base import Harness, RunContext


class ChatHarness(Harness):
    name = "chat"

    async def run(self, ctx: RunContext) -> None:
        model = ctx.model
        agent = ctx.agent
        session = ctx.session_id or f"chat-{secrets.token_hex(4)}"
        ctx.emit("run.started", {"run": ctx.run, "harness": "chat", "model_id": (model or {}).get("id", ""),
                                 "session_id": session, "cwd": ctx.cwd, "resumed": ctx.resumed})
        ctx.emit("run.status", {"phase": "turn_start", "turn": 1})
        ctx.emit("run.status", {"phase": "step_start", "turn": 1, "step": 1})
        if model is None:
            ctx.emit("run.error", {"message": "this agent has no model assigned"})
            ctx.exit_code = 1
            return

        text = thinking = ""
        usage = None
        stopped = False
        if model.get("kind") == "mock":
            rng = random.Random(f"{ctx.mission.get('id')}:{ctx.run}")
            thinking, text = mock_reply(agent, model, ctx.task, ctx.run, rng)
            stopped = await ctx.sleep(rng.uniform(1.5, 3.0) / mock_speed())
        else:
            persona = (agent.get("persona") or "").strip() or f"You are {agent.get('name', 'an assistant')}."
            messages = [{"role": "system", "content": persona}]
            if ctx.resumed and ctx.previous_result:
                messages += [{"role": "user", "content": ctx.mission.get("prompt", "")},
                             {"role": "assistant", "content": ctx.previous_result}]
            messages.append({"role": "user", "content": ctx.task})
            parts = {"text": [], "thinking": []}
            call = asyncio.create_task(stream_chat(
                ctx.http, model, messages, lambda kind, s: parts[kind].append(s)))
            stopper = asyncio.create_task(ctx.stop_event.wait())
            done, _ = await asyncio.wait({call, stopper}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
            if call not in done:
                call.cancel()
                await asyncio.wait({call}, timeout=5)
                stopped = True
                text, thinking = "".join(parts["text"]), "".join(parts["thinking"])
            else:
                try:
                    text, thinking, usage = call.result()
                except ChatError as e:
                    ctx.emit("run.error", {"message": str(e)})
                    ctx.exit_code = 1
                    return

        if thinking.strip():
            ctx.emit("run.thinking", {"text": thinking.strip()})
        if text.strip():
            ctx.emit("run.text", {"text": text.strip()})
        end = {"phase": "step_end", "turn": 1, "step": 1}
        if usage:
            end["usage"] = usage
        ctx.emit("run.status", end)
        if stopped:
            ctx.exit_code = 130
            return
        ctx.emit("run.status", {"phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}})
        ctx.emit("run.final", {"text": text.strip(), "reason": {"kind": "completed"}, "session_id": session})
        ctx.exit_code = 0
