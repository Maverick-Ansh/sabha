"""HTTP side of the hub.

`Hub` owns the store, the bus and the managers. `create_app` builds the
aiohttp application: static files from web/ at "/" (no auth), the REST API
under /api (PROTOCOL.md section 3) and the event stream at /ws (section 4),
both behind the shared token.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from . import __version__
from .bus import Bus
from .council import CouncilManager, public as council_public
from .harness import NAMES as HARNESSES
from .harness.dsh import dsh_path
from .missions import MissionManager
from .models import ModelManager
from .research_api import Research, add_routes as add_research_routes
from .research_run import Runner, read_boot
from .settings import Settings, Taste
from .store import Store
from .talk import TalkManager, thread_public
from .telemetry import Telemetry, detect_platform
from .util import ApiError, inside, new_id, now

log = logging.getLogger("sabha.server")

HUES = ("sun", "coral", "mint", "sky", "lilac", "rose")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
NO_CACHE = {"Cache-Control": "no-cache"}
FILE_MAX_BYTES = 200 * 1024
FILES_MAX = 2000
REPLAY_MAX = 5000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache"}
LOOPBACK = ("127.0.0.1", "localhost", "::1")

PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8"><title>Sabha</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;line-height:1.5">
<h1>Sabha hub is running</h1>
<p>The web UI is not built yet: <code>web/index.html</code> is missing.</p>
<p>The API is up at <code>/api/state</code> (needs the token) and events stream from <code>/ws</code>.</p>
</body></html>"""


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: Path = field(default_factory=lambda: Path("sabha_data").resolve())
    token: str = ""
    no_auth: bool = False
    demo: bool = False
    session_hours: float = 12.0
    web_dir: Path = WEB_DIR
    research_repo: str = ""  # data repo of the research daemon (missions/<id>/...); "" = no Research view data


class Hub:
    """Everything the hub knows and does, shared by all request handlers."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started_ts = now()
        self.session_limit_s = int(cfg.session_hours * 3600)
        self.platform = detect_platform()
        self.store = Store(cfg.data_dir)
        self.store.load()
        self.bus = Bus(self.store)
        self.http: aiohttp.ClientSession | None = None
        self.models = ModelManager(self)
        self.talk = TalkManager(self)
        self.missions = MissionManager(self)
        self.councils = CouncilManager(self)
        self.telemetry = Telemetry(self)
        if not cfg.research_repo:  # runs started from the app live here; --research-repo points at a git-driven data repo instead
            cfg.research_repo = str(Path(cfg.data_dir) / "research")
        os.makedirs(os.path.join(cfg.research_repo, "missions"), exist_ok=True)
        self.settings = Settings(cfg.data_dir)  # also puts a saved Tavily key into the environment
        self.taste = Taste(cfg.data_dir)
        self.research = Research(self)
        self.runner = Runner(self, cfg.research_repo)
        self.sockets: set[web.WebSocketResponse] = set()

    async def start(self) -> None:
        self.http = aiohttp.ClientSession()
        self.models.recover()
        self.missions.recover()
        self.talk.recover()
        self.councils.recover()
        if self.cfg.demo and not self.store.had_state:
            from .demo import seed_demo
            await seed_demo(self)
        self.models.start()
        self.telemetry.start()
        self.store.save_now()

    async def close(self) -> None:
        await self.missions.close()
        await self.councils.close()
        await self.talk.close()
        await self.models.close()
        await self.telemetry.close()
        if self.http is not None:
            await self.http.close()
        self.store.close()

    def snapshot(self) -> dict:
        return {
            "app": {"name": "Sabha", "version": __version__, "demo": self.cfg.demo},
            "system": {
                "platform": self.platform,
                "started_ts": self.started_ts,
                "session_limit_s": self.session_limit_s,
                "data_dir": str(self.cfg.data_dir),
                "harnesses": {"dsh": dsh_path() is not None, "mock": True, "chat": True},
                "research": self.research.available(),
            },
            "models": list(self.store.models.values()),
            "agents": list(self.store.agents.values()),
            "missions": list(self.store.missions.values()),
            "threads": [thread_public(t) for t in self.store.threads.values()],
            "councils": [council_public(c) for c in self.store.councils.values()],
            "seq_head": self.store.seq_head,
        }

    # ------------------------------------------------------------ agents

    def _agent_fields(self, body: dict, partial: bool) -> dict:
        out: dict = {}
        if "name" in body:
            if not isinstance(body["name"], str) or not body["name"].strip():
                raise ApiError(400, "name must be a non-empty string")
            out["name"] = body["name"].strip()[:60]
        for key, limit in (("role", 200), ("persona", 20000)):
            if key in body:
                value = body[key] if body[key] is not None else ""
                if not isinstance(value, str):
                    raise ApiError(400, f"{key} must be a string")
                out[key] = value.strip()[:limit]
        if "model_id" in body:
            mid = body["model_id"] or ""
            if not isinstance(mid, str) or (mid and mid not in self.store.models):
                raise ApiError(400, "model_id must name an existing model (or be empty)")
            out["model_id"] = mid
        if "harness" in body:
            if body["harness"] not in HARNESSES:
                raise ApiError(400, f"harness must be one of {', '.join(HARNESSES)}")
            out["harness"] = body["harness"]
        if "hue" in body:
            if body["hue"] not in HUES:
                raise ApiError(400, f"hue must be one of {', '.join(HUES)}")
            out["hue"] = body["hue"]
        if not partial and "name" not in out:
            raise ApiError(400, "name is required")
        return out

    def create_agent(self, body: dict) -> dict:
        f = self._agent_fields(body, partial=False)
        model = self.store.models.get(f.get("model_id", ""))
        harness = f.get("harness") or ("mock" if model is None or model.get("kind") == "mock" else "dsh")
        agent = {
            "id": new_id("a"),
            "name": f["name"],
            "role": f.get("role", ""),
            "persona": f.get("persona", ""),
            "model_id": f.get("model_id", ""),
            "harness": harness,
            "hue": f.get("hue") or HUES[len(self.store.agents) % len(HUES)],
        }
        self.store.agents[agent["id"]] = agent
        self.bus.emit("agent.updated", {"agent": agent}, agent_id=agent["id"])
        self.store.mark_dirty()
        return agent

    def patch_agent(self, agent_id: str, body: dict) -> dict:
        agent = self.store.agents.get(agent_id)
        if agent is None:
            raise ApiError(404, "agent not found")
        agent.update(self._agent_fields(body, partial=True))
        self.bus.emit("agent.updated", {"agent": agent}, agent_id=agent_id)
        self.store.mark_dirty()
        return agent

    async def delete_agent(self, agent_id: str) -> None:
        if agent_id not in self.store.agents:
            raise ApiError(404, "agent not found")
        for mid in [m for m, _ in list(self.missions.active.items())]:
            mission = self.store.missions.get(mid)
            if mission and mission["agent_id"] == agent_id:
                await self.missions.stop(mid)
        for tid, thread in list(self.store.threads.items()):
            if thread.get("agent_id") == agent_id:
                await self.talk.stop(tid)
        self.store.agents.pop(agent_id, None)
        self.bus.emit("agent.removed", {"id": agent_id}, agent_id=agent_id)
        self.store.mark_dirty()


HUB_KEY = web.AppKey("hub", Hub)


# ---------------------------------------------------------------- helpers


def _hub(request: web.Request) -> Hub:
    return request.app[HUB_KEY]


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _body(request: web.Request) -> dict:
    if not request.body_exists:
        return {}
    try:
        data = await request.json(loads=json.loads)
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "body must be valid JSON") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(400, "body must be a JSON object")
    return data


def _int_query(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, f"{name} must be an integer") from None
    return min(max(value, lo), hi)


def _events_response(lines: list[str], more: bool) -> web.Response:
    text = '{"events":[' + ",".join(lines) + '],"more":' + ("true" if more else "false") + "}"
    return web.Response(text=text, content_type="application/json")


def _token_ok(request: web.Request, cfg: Config) -> bool:
    if cfg.no_auth:
        return True
    supplied = ""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        supplied = header[7:].strip()
    elif request.path == "/ws" or request.path.startswith("/api/research/export/"):  # a socket or a plain download link sends no header
        supplied = request.query.get("token", "")
    return bool(supplied) and hmac.compare_digest(supplied.encode(), cfg.token.encode())


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as e:
        return json_error(e.status, e.message)
    except web.HTTPException as e:
        if request.path.startswith("/api"):
            return json_error(e.status, (e.reason or "error").lower())
        raise
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("handler failed: %s %s", request.method, request.path)
        return json_error(500, f"internal error: {e}")


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if path == "/api" or path.startswith("/api/") or path == "/ws":
        if not _token_ok(request, _hub(request).cfg):
            return json_error(401, "unauthorized")
    return await handler(request)


# ---------------------------------------------------------------- state / models / agents


async def get_state(request):
    return web.json_response(_hub(request).snapshot())


# ---- box boot strip, settings, research taste and runs started from the app (PROTOCOL.md sections 8 and 9)

async def get_boot(request):
    return web.json_response(await asyncio.to_thread(read_boot, _hub(request).cfg.data_dir))


async def get_settings(request):
    return web.json_response(_hub(request).settings.public())


async def put_settings(request):
    return web.json_response(_hub(request).settings.update(await _body(request)))


async def get_taste(request):
    return web.json_response(_hub(request).taste.load())


async def put_taste(request):
    return web.json_response(_hub(request).taste.save(await _body(request)))


async def research_start(request):
    return web.json_response(await asyncio.to_thread(_hub(request).runner.start, await _body(request)))


async def research_stop(request):
    body = await _body(request)
    return web.json_response(await asyncio.to_thread(_hub(request).runner.stop, str(body.get("mode") or "now")))


async def research_export(request):
    name, blob = await asyncio.to_thread(_hub(request).runner.export, request.match_info["mid"])
    return web.Response(body=blob, content_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def create_model(request):
    return web.json_response(_hub(request).models.create(await _body(request)))


async def patch_model(request):
    return web.json_response(_hub(request).models.patch(request.match_info["id"], await _body(request)))


async def delete_model(request):
    await _hub(request).models.delete(request.match_info["id"])
    return web.json_response({"ok": True})


async def launch_model(request):
    return web.json_response(await _hub(request).models.launch(request.match_info["id"], await _body(request)))


async def stop_model(request):
    return web.json_response(await _hub(request).models.stop(request.match_info["id"]))


async def probe_model(request):
    return web.json_response(await _hub(request).models.probe(request.match_info["id"]))


async def model_log(request):
    return web.json_response({"lines": _hub(request).models.log_lines(request.match_info["id"])})


async def create_agent(request):
    return web.json_response(_hub(request).create_agent(await _body(request)))


async def patch_agent(request):
    return web.json_response(_hub(request).patch_agent(request.match_info["id"], await _body(request)))


async def delete_agent(request):
    await _hub(request).delete_agent(request.match_info["id"])
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- missions


async def create_mission(request):
    return web.json_response(await _hub(request).missions.create(await _body(request)))


async def get_mission(request):
    return web.json_response(_hub(request).missions.get(request.match_info["id"]))


async def approve_mission(request):
    return web.json_response(await _hub(request).missions.approve(request.match_info["id"], await _body(request)))


async def stop_mission(request):
    return web.json_response(await _hub(request).missions.stop(request.match_info["id"]))


async def note_mission(request):
    return web.json_response(await _hub(request).missions.note(request.match_info["id"], await _body(request)))


async def resume_mission(request):
    return web.json_response(await _hub(request).missions.resume(request.match_info["id"], await _body(request)))


async def delete_mission(request):
    await _hub(request).missions.delete(request.match_info["id"])
    return web.json_response({"ok": True})


async def mission_events(request):
    hub = _hub(request)
    mission_id = request.match_info["id"]
    hub.missions.get(mission_id)
    since = _int_query(request, "since", 0, 0, 2**62)
    limit = _int_query(request, "limit", 2000, 1, REPLAY_MAX)
    lines, more = hub.store.events.range(since, limit, mission_id=mission_id)
    return _events_response(lines, more)


def _list_files(root: Path) -> list[dict]:
    out: list[dict] = []
    if not root.is_dir():
        return out
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"path": p.relative_to(root).as_posix(), "size": st.st_size, "mtime": st.st_mtime})
            if len(out) >= FILES_MAX:
                return out
    return out


def _read_file(root: Path, rel: str) -> dict:
    if not rel:
        raise ApiError(400, "path is required")
    if "\x00" in rel or rel.startswith(("/", "\\")) or os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
        raise ApiError(400, "path must be relative to the mission workspace")
    # A backslash is a path separator on Windows but an ordinary character on Linux.
    # Treat it as a separator everywhere, so "..\..\x" is refused on every platform.
    target = root / rel.replace("\\", "/")
    if not inside(root, target):
        raise ApiError(400, "path escapes the mission workspace")
    target = target.resolve()
    if not target.is_file():
        raise ApiError(404, "file not found")
    size = target.stat().st_size
    with open(target, "rb") as f:
        data = f.read(FILE_MAX_BYTES + 1)
    shown = target.relative_to(root.resolve()).as_posix()
    if b"\x00" in data[:8192]:
        return {"path": shown, "size": size, "binary": True}
    truncated = size > FILE_MAX_BYTES
    text = data[:FILE_MAX_BYTES].decode("utf-8", errors="replace")
    if text.count("�") > max(8, len(text) // 100):
        return {"path": shown, "size": size, "binary": True}
    return {"path": shown, "size": size, "truncated": truncated, "text": text}


async def mission_files(request):
    hub = _hub(request)
    mission = hub.missions.get(request.match_info["id"])
    files = await asyncio.to_thread(_list_files, hub.missions.workspace(mission))
    return web.json_response({"files": files})


async def mission_file(request):
    hub = _hub(request)
    mission = hub.missions.get(request.match_info["id"])
    rel = request.query.get("path", "")
    return web.json_response(await asyncio.to_thread(_read_file, hub.missions.workspace(mission), rel))


# ---------------------------------------------------------------- threads / events


async def create_thread(request):
    return web.json_response(_hub(request).talk.create(await _body(request)))


async def get_thread(request):
    return web.json_response(_hub(request).talk.get(request.match_info["id"]))


async def post_message(request):
    return web.json_response(await _hub(request).talk.post(request.match_info["id"], await _body(request)))


async def stop_thread(request):
    await _hub(request).talk.stop(request.match_info["id"])
    return web.json_response({"ok": True})


async def delete_thread(request):
    await _hub(request).talk.delete(request.match_info["id"])
    return web.json_response({"ok": True})


# ---------------------------------------------------------------- councils


async def create_council(request):
    return web.json_response(_hub(request).councils.create(await _body(request)))


async def get_council(request):
    return web.json_response(_hub(request).councils.get(request.match_info["id"]))


async def say_council(request):
    return web.json_response(await _hub(request).councils.say(request.match_info["id"], await _body(request)))


async def stop_council(request):
    return web.json_response(await _hub(request).councils.stop(request.match_info["id"]))


async def delete_council(request):
    await _hub(request).councils.delete(request.match_info["id"])
    return web.json_response({"ok": True})


async def council_section(request):
    hub = _hub(request)
    return web.json_response(hub.councils.section(request.match_info["id"], request.query.get("id", "")))


async def all_events(request):
    hub = _hub(request)
    since = _int_query(request, "since", 0, 0, 2**62)
    limit = _int_query(request, "limit", 2000, 1, REPLAY_MAX)
    lines, more = hub.store.events.range(since, limit)
    return _events_response(lines, more)


# ---------------------------------------------------------------- websocket


async def _ws_sender(ws: web.WebSocketResponse, sub) -> None:
    try:
        while True:
            line = await sub.queue.get()
            if line is None:
                await ws.close(code=4000, message=b"too far behind; reconnect with since")
                return
            await ws.send_str(line)
    except (ConnectionResetError, RuntimeError, aiohttp.ClientError):
        pass


async def ws_handler(request):
    hub = _hub(request)
    try:
        since = int(request.query["since"]) if request.query.get("since") not in (None, "") else None
    except ValueError:
        since = None
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1 << 20)
    await ws.prepare(request)
    # Subscribe and snapshot with no await in between, so live events start
    # exactly after seq_head and nothing is lost or doubled.
    sub = hub.bus.subscribe()
    head = hub.store.seq_head
    hello = json.dumps({"type": "hello", "seq_head": head, "state": hub.snapshot()}, default=str)
    replay = hub.store.events.tail(since, REPLAY_MAX, head) if since is not None and since < head else []
    hub.sockets.add(ws)
    sender = None
    try:
        await ws.send_str(hello)
        for line in replay:
            await ws.send_str(line)
        if hub.telemetry.last_event is not None:
            await ws.send_str(json.dumps(hub.telemetry.last_event))
        sender = asyncio.create_task(_ws_sender(ws, sub))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "ts": time.time()}))
            elif msg.type == WSMsgType.ERROR:
                break
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        hub.bus.unsubscribe(sub)
        hub.sockets.discard(ws)
        if sender is not None:
            sender.cancel()
    return ws


# ---------------------------------------------------------------- static


async def static_handler(request):
    hub = _hub(request)
    rel = request.match_info.get("tail", "")
    if rel == "api" or rel.startswith("api/") or rel == "ws":
        return json_error(404, "not found")
    web_dir = hub.cfg.web_dir
    index = web_dir / "index.html"
    if rel not in ("", "index.html"):
        target = web_dir / rel
        if inside(web_dir, target) and target.is_file():
            return web.FileResponse(target, headers=NO_CACHE)
        if "." in rel.rsplit("/", 1)[-1]:
            raise web.HTTPNotFound()
        # an extension-less path: let the single-page UI route it
    if index.is_file():
        return web.FileResponse(index, headers=NO_CACHE)
    return web.Response(text=PLACEHOLDER, content_type="text/html", headers=NO_CACHE)


# ---------------------------------------------------------------- app


def create_app(hub: Hub) -> web.Application:
    app = web.Application(middlewares=[error_middleware, auth_middleware], client_max_size=8 * 2**20)
    app[HUB_KEY] = hub
    r = app.router
    r.add_get("/api/state", get_state)
    r.add_post("/api/models", create_model)
    r.add_patch("/api/models/{id}", patch_model)
    r.add_delete("/api/models/{id}", delete_model)
    r.add_post("/api/models/{id}/launch", launch_model)
    r.add_post("/api/models/{id}/stop", stop_model)
    r.add_post("/api/models/{id}/probe", probe_model)
    r.add_get("/api/models/{id}/log", model_log)
    r.add_post("/api/agents", create_agent)
    r.add_patch("/api/agents/{id}", patch_agent)
    r.add_delete("/api/agents/{id}", delete_agent)
    r.add_post("/api/missions", create_mission)
    r.add_get("/api/missions/{id}", get_mission)
    r.add_post("/api/missions/{id}/approve", approve_mission)
    r.add_post("/api/missions/{id}/stop", stop_mission)
    r.add_post("/api/missions/{id}/note", note_mission)
    r.add_post("/api/missions/{id}/resume", resume_mission)
    r.add_delete("/api/missions/{id}", delete_mission)
    r.add_get("/api/missions/{id}/events", mission_events)
    r.add_get("/api/missions/{id}/files", mission_files)
    r.add_get("/api/missions/{id}/file", mission_file)
    r.add_post("/api/threads", create_thread)
    r.add_get("/api/threads/{id}", get_thread)
    r.add_post("/api/threads/{id}/messages", post_message)
    r.add_post("/api/threads/{id}/stop", stop_thread)
    r.add_delete("/api/threads/{id}", delete_thread)
    r.add_post("/api/councils", create_council)
    r.add_get("/api/councils/{id}", get_council)
    r.add_post("/api/councils/{id}/say", say_council)
    r.add_post("/api/councils/{id}/stop", stop_council)
    r.add_delete("/api/councils/{id}", delete_council)
    r.add_get("/api/councils/{id}/section", council_section)
    r.add_get("/api/events", all_events)
    add_research_routes(r, _hub, _body)
    r.add_get("/api/boot", get_boot)
    r.add_get("/api/settings", get_settings)
    r.add_put("/api/settings", put_settings)
    r.add_get("/api/taste", get_taste)
    r.add_put("/api/taste", put_taste)
    r.add_post("/api/research/start", research_start)
    r.add_post("/api/research/stop", research_stop)
    r.add_get("/api/research/export/{mid}", research_export)
    r.add_get("/ws", ws_handler)
    r.add_get("/{tail:.*}", static_handler)

    async def on_startup(app):
        await hub.start()

    async def on_shutdown(app):
        for ws in list(hub.sockets):
            await ws.close(code=1001, message=b"hub shutting down")

    async def on_cleanup(app):
        await hub.close()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    return app
