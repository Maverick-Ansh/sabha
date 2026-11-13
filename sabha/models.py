"""Model registry.

CRUD for models, health probes (GET {base_url}/models must answer 200), and a
launcher that runs a serving command (vLLM or anything OpenAI-compatible) as a
child process in its own process group. The child's stdout+stderr go into a
400-line ring buffer and out as ephemeral model.log events. Mock models are
answered by the hub itself and are always `ready`.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import subprocess

import aiohttp

from .util import ApiError, group_kwargs, kill_tree, new_id, now, read_lines

log = logging.getLogger("sabha.models")

KINDS = ("chat", "decision", "embedding", "custom", "mock")
LOG_LINES = 400
PROBE_EVERY_S = 30.0
LAUNCH_PROBE_EVERY_S = 5.0
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def api_key(model: dict) -> str:
    env = model.get("api_key_env") or ""
    return os.environ.get(env, "") if env else ""


def auth_headers(model: dict) -> dict:
    key = api_key(model)
    return {"Authorization": f"Bearer {key}"} if key else {}


def clean_model_fields(body: dict, partial: bool) -> dict:
    """Validate the user-editable model fields. Unknown keys are ignored."""
    out: dict = {}
    if "name" in body:
        name = body["name"]
        if not isinstance(name, str) or not name.strip():
            raise ApiError(400, "name must be a non-empty string")
        out["name"] = name.strip()[:80]
    if "kind" in body:
        if body["kind"] not in KINDS:
            raise ApiError(400, f"kind must be one of {', '.join(KINDS)}")
        out["kind"] = body["kind"]
    for key in ("base_url", "served_name", "notes"):
        if key in body:
            value = body[key] if body[key] is not None else ""
            if not isinstance(value, str):
                raise ApiError(400, f"{key} must be a string")
            out[key] = value.strip()
    if "base_url" in out:
        url = out["base_url"].rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise ApiError(400, "base_url must start with http:// or https://")
        out["base_url"] = url
    if "api_key_env" in body:
        env = body["api_key_env"] or ""
        if not isinstance(env, str) or (env and not _ENV_NAME.match(env)):
            raise ApiError(400, "api_key_env must be an environment variable name")
        out["api_key_env"] = env
    if "context_window" in body:
        try:
            cw = int(body["context_window"])
        except (TypeError, ValueError):
            raise ApiError(400, "context_window must be an integer") from None
        if cw <= 0:
            raise ApiError(400, "context_window must be positive")
        out["context_window"] = cw
    if "extra_body" in body:
        extra = body["extra_body"] if body["extra_body"] is not None else {}
        if not isinstance(extra, dict):
            raise ApiError(400, "extra_body must be a JSON object")
        out["extra_body"] = extra
    if not partial and "name" not in out:
        raise ApiError(400, "name is required")
    return out


class ModelManager:
    def __init__(self, hub):
        self.hub = hub
        self.store = hub.store
        self.bus = hub.bus
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.logs: dict[str, collections.deque] = {}
        self._tasks: set[asyncio.Task] = set()
        self._prober: asyncio.Task | None = None

    # ------------------------------------------------------------ helpers

    def get(self, model_id: str) -> dict:
        model = self.store.models.get(model_id)
        if model is None:
            raise ApiError(404, "model not found")
        return model

    def _changed(self, model: dict) -> None:
        self.bus.emit("model.updated", {"model": model})
        self.store.mark_dirty()

    def _set_status(self, model: dict, status: str) -> None:
        if model.get("status") != status:
            model["status"] = status
            self._changed(model)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _alive(self, model_id: str) -> bool:
        proc = self.procs.get(model_id)
        return proc is not None and proc.returncode is None

    # ------------------------------------------------------------ lifecycle

    def recover(self) -> None:
        """After a restart: we no longer own any launched process."""
        for m in self.store.models.values():
            m.setdefault("launch", None)
            m.setdefault("notes", "")
            m.setdefault("api_key_env", "")
            m.setdefault("context_window", 32768)
            m.setdefault("extra_body", {})
            if isinstance(m.get("launch"), dict):
                m["launch"]["pid"] = None
            m["status"] = "ready" if m.get("kind") == "mock" else "unknown"

    def start(self) -> None:
        self._prober = asyncio.create_task(self._probe_loop())

    async def close(self) -> None:
        if self._prober:
            self._prober.cancel()
        for model_id, proc in list(self.procs.items()):
            await kill_tree(proc)
        for task in list(self._tasks):
            task.cancel()

    # ------------------------------------------------------------ CRUD

    def create(self, body: dict) -> dict:
        f = clean_model_fields(body, partial=False)
        kind = f.get("kind", "chat")
        model = {
            "id": new_id("m"),
            "name": f["name"],
            "kind": kind,
            "base_url": f.get("base_url", ""),
            "served_name": f.get("served_name") or ("" if kind == "mock" else f["name"]),
            "api_key_env": f.get("api_key_env", ""),
            "context_window": f.get("context_window", 32768),
            "extra_body": f.get("extra_body", {}),
            "status": "ready" if kind == "mock" else "unknown",
            "launch": None,
            "notes": f.get("notes", ""),
        }
        self.store.models[model["id"]] = model
        self._changed(model)
        if kind != "mock" and model["base_url"]:
            self._spawn(self._recheck(model))
        return model

    def patch(self, model_id: str, body: dict) -> dict:
        model = self.get(model_id)
        f = clean_model_fields(body, partial=True)
        reprobe = any(k in f and f[k] != model.get(k) for k in ("base_url", "kind", "api_key_env"))
        model.update(f)
        if model["kind"] == "mock":
            model["status"] = "ready"
        elif reprobe and not model["base_url"]:
            model["status"] = "unknown"
        self._changed(model)
        if reprobe and model["kind"] != "mock" and model["base_url"]:
            self._spawn(self._recheck(model))
        return model

    async def delete(self, model_id: str) -> None:
        self.get(model_id)
        proc = self.procs.get(model_id)
        if proc is not None:
            await kill_tree(proc)
        self.store.models.pop(model_id, None)
        self.logs.pop(model_id, None)
        self.bus.emit("model.removed", {"id": model_id})
        for agent in self.store.agents.values():
            if agent.get("model_id") == model_id:
                agent["model_id"] = ""
                self.bus.emit("agent.updated", {"agent": agent}, agent_id=agent["id"])
        self.store.mark_dirty()

    # ------------------------------------------------------------ probing

    async def check(self, model: dict) -> bool:
        """True when GET {base_url}/models answers 200 within 5 s."""
        if model.get("kind") == "mock":
            return True
        base = (model.get("base_url") or "").rstrip("/")
        if not base or self.hub.http is None:
            return False
        try:
            async with self.hub.http.get(
                base + "/models", headers=auth_headers(model),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return False

    async def _recheck(self, model: dict) -> None:
        if model.get("kind") == "mock":
            self._set_status(model, "ready")
            return
        if not model.get("base_url"):
            return
        ok = await self.check(model)
        if model["id"] not in self.store.models:
            return
        if ok:
            self._set_status(model, "ready")
        elif self._alive(model["id"]) and model.get("status") == "starting":
            pass  # still loading weights; the launch prober owns it
        else:
            self._set_status(model, "down")

    async def probe(self, model_id: str) -> dict:
        model = self.get(model_id)
        if model["kind"] == "mock":
            model["status"] = "ready"
        elif not model.get("base_url"):
            model["status"] = "unknown"
        elif await self.check(model):
            model["status"] = "ready"
        else:
            model["status"] = "starting" if self._alive(model_id) else "down"
        self._changed(model)
        return model

    async def _probe_loop(self) -> None:
        while True:
            try:
                models = [
                    m for m in self.store.models.values()
                    if m.get("kind") == "mock"
                    or (m.get("base_url") and not (m.get("status") == "starting" and self._alive(m["id"])))
                ]
                await asyncio.gather(*(self._recheck(m) for m in models), return_exceptions=True)
            except Exception:
                log.exception("model probe loop")
            await asyncio.sleep(PROBE_EVERY_S)

    # ------------------------------------------------------------ launcher

    async def launch(self, model_id: str, body: dict) -> dict:
        model = self.get(model_id)
        if model["kind"] == "mock":
            raise ApiError(400, "mock models are answered by the hub; nothing to launch")
        cmd = body.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ApiError(400, "cmd must be a non-empty string")
        env = body.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and k for k in env):
            raise ApiError(400, "env must be an object of NAME: value")
        env = {k: "" if v is None else str(v) for k, v in env.items()}
        if self._alive(model_id):
            raise ApiError(409, "this model is already running; stop it first")
        ring = collections.deque(maxlen=LOG_LINES)
        self.logs[model_id] = ring
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={**os.environ, **env},
                **group_kwargs(),
            )
        except OSError as e:
            raise ApiError(500, f"could not start command: {e}") from None
        self.procs[model_id] = proc
        model["launch"] = {"cmd": cmd, "env": env, "pid": proc.pid, "started_ts": now()}
        model["status"] = "starting"
        self._changed(model)
        self._log(model_id, f"[sabha] $ {cmd}")
        self._spawn(self._watch(model_id, proc))
        return model

    def _log(self, model_id: str, line: str) -> None:
        ring = self.logs.setdefault(model_id, collections.deque(maxlen=LOG_LINES))
        ring.append(line)
        self.bus.emit("model.log", {"model_id": model_id, "line": line}, ephemeral=True)

    async def _watch(self, model_id: str, proc: asyncio.subprocess.Process) -> None:
        reader = asyncio.create_task(
            read_lines(proc.stdout, lambda line: self._log(model_id, line), split_cr=True)
        )
        prober = asyncio.create_task(self._launch_probe(model_id, proc))
        code = await proc.wait()
        prober.cancel()
        try:
            await asyncio.wait_for(reader, 3)
        except (asyncio.TimeoutError, Exception):
            reader.cancel()
        if self.procs.get(model_id) is proc:
            del self.procs[model_id]
        self._log(model_id, f"[sabha] process exited with code {code}")
        model = self.store.models.get(model_id)
        if model is not None:
            if isinstance(model.get("launch"), dict):
                model["launch"]["pid"] = None
            model["status"] = "down"
            self._changed(model)

    async def _launch_probe(self, model_id: str, proc: asyncio.subprocess.Process) -> None:
        """While the launched process lives and the model is `starting`, probe every 5 s."""
        await asyncio.sleep(2.0)
        while proc.returncode is None:
            model = self.store.models.get(model_id)
            if model is None or model.get("status") != "starting":
                return
            if await self.check(model):
                if proc.returncode is None:
                    self._set_status(model, "ready")
                    self._log(model_id, "[sabha] endpoint answered /models: ready")
                return
            await asyncio.sleep(LAUNCH_PROBE_EVERY_S)

    async def stop(self, model_id: str) -> dict:
        model = self.get(model_id)
        proc = self.procs.get(model_id)
        if proc is not None:
            self._log(model_id, "[sabha] stopping")
            await kill_tree(proc)
            for _ in range(50):  # let the watcher record the exit
                if model_id not in self.procs:
                    break
                await asyncio.sleep(0.05)
        if model["kind"] != "mock":
            if isinstance(model.get("launch"), dict):
                model["launch"]["pid"] = None
            model["status"] = "down"
            self._changed(model)
        return model

    def log_lines(self, model_id: str) -> list[str]:
        self.get(model_id)
        return list(self.logs.get(model_id, []))
