"""DeepSeek Harness (`dsh`) adapter.

Runs `dsh --profile headless --patch <run_dir>/patch.yml --json [--session-id ID] -`
in the mission workspace, writes the task to stdin and closes it, then maps
each NDJSON stdout line to a run event:

    session     -> run.started        status      -> run.status
    thinking    -> run.thinking       text        -> run.text
    tool_call   -> run.tool_call      tool_result -> run.tool_result
    final       -> run.final          error       -> run.error
    anything else -> run.raw          stderr lines -> run.stderr

Extra fields (for example `truncated`) are passed through in `data`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from ..util import group_kwargs, kill_tree, read_lines
from .base import Harness, RunContext

log = logging.getLogger("sabha.dsh")

DSH_MISSING = ("dsh not found on PATH; it needs Node >= 24.9 and a pinned version "
               "(pre-releases only): npm i -g @deepseek-ai/dsh@0.2.1-rc.5")


def dsh_path() -> str | None:
    """The dsh executable (dsh.cmd on Windows), or None."""
    return shutil.which("dsh")


def make_patch(model: dict) -> str:
    """patch.yml for one run. Written by hand; JSON strings are valid YAML scalars."""
    q = json.dumps
    served = model.get("served_name") or model.get("name") or "model"
    return (
        "- id: llm-pi-ai\n"
        "  config:\n"
        "    providers:\n"
        "      sabha:\n"
        "        displayName: Sabha\n"
        "        apiKeyEnv: SABHA_LLM_KEY\n"
        "        api: openai-completions\n"
        f"        baseURL: {q(model.get('base_url') or '')}\n"
        "        models:\n"
        f"          - id: {q(served)}\n"
        f"            contextWindow: {int(model.get('context_window') or 32768)}\n"
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: sabha\n"
        f"    model: {q(served)}\n"
    )


def _rename(d: dict, old: str, new: str) -> None:
    if old in d:
        d[new] = d.pop(old)


class _Mapper:
    """Turns dsh stdout lines into protocol run events."""

    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    def on_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            ev = json.loads(text)
        except ValueError:
            self.ctx.emit("run.raw", {"raw": text})
            return
        if not isinstance(ev, dict):
            self.ctx.emit("run.raw", {"raw": ev})
            return
        kind = ev.get("type")
        rest = {k: v for k, v in ev.items() if k != "type"}
        _rename(rest, "sessionId", "session_id")
        _rename(rest, "callId", "call_id")
        ctx = self.ctx
        if kind == "session":
            data = {
                "run": ctx.run,
                "harness": "dsh",
                "model_id": (ctx.model or {}).get("id", ""),
                "session_id": rest.pop("session_id", "") or "",
                "cwd": rest.pop("cwd", ctx.cwd),
                "resumed": ctx.resumed,
            }
            data.update(rest)
            ctx.emit("run.started", data)
        elif kind == "status":
            ctx.emit("run.status", rest)
        elif kind in ("thinking", "text"):
            ctx.emit(f"run.{kind}", rest)
        elif kind == "tool_call":
            data = {"call_id": rest.pop("call_id", ""), "tool": rest.pop("tool", ""),
                    "input": rest.pop("input", {})}
            data.update(rest)
            ctx.emit("run.tool_call", data)
        elif kind == "tool_result":
            data = {"call_id": rest.pop("call_id", ""), "status": rest.pop("status", "completed"),
                    "result": rest.pop("result", None)}
            data.update(rest)
            ctx.emit("run.tool_result", data)
        elif kind == "final":
            data = {"text": rest.pop("text", "") or "", "reason": rest.pop("reason", None)}
            data.update(rest)
            ctx.emit("run.final", data)
        elif kind == "error":
            data = {"message": str(rest.pop("message", "") or "error")}
            data.update(rest)
            ctx.emit("run.error", data)
        else:
            ctx.emit("run.raw", {"raw": ev})


class DshHarness(Harness):
    name = "dsh"

    async def run(self, ctx: RunContext) -> None:
        exe = dsh_path()
        if not exe:
            ctx.emit("run.error", {"message": DSH_MISSING})
            ctx.exit_code = 127
            return
        model = ctx.model
        if not model or model.get("kind") == "mock" or not model.get("base_url"):
            name = (model or {}).get("name", "none")
            ctx.emit("run.error", {"message": (
                f"dsh needs a model with an OpenAI-compatible base_url; this agent's model is "
                f"{name!r}. Pick a served model, or switch the agent to the mock harness.")})
            ctx.exit_code = 2
            return

        mission_id = ctx.mission["id"]
        run_dir = Path(ctx.data_dir) / "runs" / f"{mission_id}-r{ctx.run}"
        run_dir.mkdir(parents=True, exist_ok=True)
        patch = run_dir / "patch.yml"
        patch.write_text(make_patch(model), encoding="utf-8")
        home = Path(ctx.data_dir) / "dsh_home" / ctx.agent.get("id", "agent")
        home.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        key_env = model.get("api_key_env") or ""
        env.update({
            "DSH_HOME": str(home),
            "DSH_PERMISSION_MODE": "danger-full-access",
            "SABHA_LLM_KEY": (os.environ.get(key_env, "") if key_env else "") or "none",
        })
        args = [exe, "--profile", "headless", "--patch", str(patch), "--json"]
        if ctx.session_id:
            args += ["--session-id", ctx.session_id]
        args.append("-")

        task = ctx.task
        persona = (ctx.agent.get("persona") or "").strip()
        if not ctx.session_id and persona:
            task = f"{persona}\n\n---\n\n{task}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=ctx.cwd, env=env, **group_kwargs(),
            )
        except OSError as e:
            ctx.emit("run.error", {"message": f"could not start dsh: {e}"})
            ctx.exit_code = 126
            return

        mapper = _Mapper(ctx)

        def on_stderr(line: str) -> None:
            if line.strip():
                ctx.emit("run.stderr", {"line": line.rstrip()})

        readers = {
            asyncio.create_task(read_lines(proc.stdout, mapper.on_line)),
            asyncio.create_task(read_lines(proc.stderr, on_stderr)),
        }
        try:
            proc.stdin.write(task.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

        waiter = asyncio.create_task(proc.wait())
        stopper = asyncio.create_task(ctx.stop_event.wait())
        try:
            done, _ = await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if waiter not in done:
                await kill_tree(proc)  # stop: kill the whole tree, then await exit
            await waiter
        finally:
            stopper.cancel()
            if proc.returncode is None:
                await kill_tree(proc)
            _, pending = await asyncio.wait(readers, timeout=5)
            for t in pending:
                t.cancel()
        ctx.exit_code = proc.returncode
