"""Linked boxes (multiKaggle): other Sabha boxes whose models this hub uses, and the hub this box reports to.

One hub, many boxes. Every box is a normal Sabha box (hub + its own model servers + its own link). A box is
linked in one of two ways:
    add       the owner pastes a box's link in Nirmana (POST /api/boxes)
    announce  a box started with SABHA_JOIN=<this hub's link> (the "new box" cell Nirmana gives) reports
              itself (POST /api/boxes/announce), again whenever its own tunnel link changes
Then this hub registers each model of that box as a remote model, base_url = <box link>/api/relay/<port>/v1,
with the box's token kept here (boxes.json, 0600) and handed to the model as api_key_env SABHA_BOX_KEY_<id>.
Talk, Council, Work and the research engine's pool (research/llm.py) use them like local models.

Every 30 s each box is asked GET /api/box. New models there are registered, gone ones removed. A box that does
not answer three times in a row (~90 s) is marked down: an app toast (box.updated) and a phone alert; and again
when it is back. When this hub's own link changes (a hub restart), every box is told the new one
(POST <box>/api/boxes/hub), so the boxes keep announcing to the right place. PROTOCOL.md section 20.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

from .research_run import read_boot
from .util import ApiError

log = logging.getLogger("sabha.boxes")

POLL_S = float(os.environ.get("SABHA_BOX_POLL_S") or 30)  # tests use a few seconds
LOCAL_OK = os.environ.get("SABHA_BOXES_LOCAL") == "1"  # tests: boxes on 127.0.0.1 links (a real box with no tunnel is skipped)
DOWN_AFTER = 3          # missed polls before a box counts as down
ANNOUNCE_EVERY_S = 300  # a joined box re-announces this often even when nothing changed (the hub may have lost it)
LOCAL_URL = re.compile(r"^http://(?:127\.0\.0\.1|localhost):(\d+)/v1/?$")


def split_link(link: str) -> tuple[str, str]:
    """'https://x.trycloudflare.com/#token=T' -> ('https://x.trycloudflare.com', 'T')."""
    link = (link or "").strip()
    m = re.search(r"token=([^&#\s]+)", link)
    token = m.group(1) if m else ""
    base = re.split(r"[#?]", link, maxsplit=1)[0].rstrip("/")
    if not re.match(r"^https?://[^/\s]+", base):
        raise ApiError(400, "paste the box's link: https://....trycloudflare.com/#token=...")
    return base, token


class Boxes:
    def __init__(self, hub):
        self.hub = hub
        self.path = Path(hub.cfg.data_dir) / "boxes.json"
        d = self._read()
        self.boxes: dict[str, dict] = d.get("boxes") or {}
        self.upstream: dict = d.get("hub") or {}  # the hub this box reports to: {base, token}
        self.my_id = d.get("my_id") or ("b" + secrets.token_hex(4))
        self._told_link = ""      # this hub's link the boxes were last told
        self._announced = (0.0, "")
        self._task: asyncio.Task | None = None
        for b in self.boxes.values():
            b.update(fails=0, status=b.get("status") or "new")
            self._key_env(b)
        join = os.environ.get("SABHA_JOIN") or ""
        if join:
            try:
                base, tok = split_link(join)
                self.upstream = {"base": base, "token": tok or hub.cfg.token}
            except ApiError:
                log.warning("SABHA_JOIN is not a Sabha link: %r", join[:80])
        self._save()

    # ------------------------------------------------------------ files

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"my_id": self.my_id, "boxes": self.boxes, "hub": self.upstream}, indent=1), "utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _env_name(bid: str) -> str:
        return "SABHA_BOX_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", bid).upper()

    def _key_env(self, b: dict) -> str:
        """The box's token lives in this process's environment (and so in every agent and research run started
        from it), never in the models list the app sees."""
        name = self._env_name(b["id"])
        os.environ[name] = b.get("token") or self.hub.cfg.token or ""
        return name

    # ------------------------------------------------------------ what this box tells others

    def self_info(self) -> dict:
        boot = read_boot(self.hub.cfg.data_dir)
        models = []
        for m in self.hub.store.models.values():
            mm = LOCAL_URL.match(m.get("base_url") or "")
            if mm and m.get("kind") != "mock":
                models.append({"name": m.get("name"), "served_name": m.get("served_name"), "port": int(mm.group(1)),
                               "status": m.get("status"), "context_window": m.get("context_window"),
                               "concurrency": m.get("concurrency") or 0})
        link = (self.hub.public_link or "").split("#")[0].rstrip("/")
        return {"id": self.my_id, "link": link, "platform": self.hub.platform, "gpus": boot.get("gpus") or [],
                "models": models, "ready": bool(boot.get("ready")), "stage": boot.get("stage") or ""}

    def public(self) -> dict:
        return {"me": {"id": self.my_id, "link": (self.hub.public_link or "").split("#")[0].rstrip("/")},
                "hub": {"base": self.upstream.get("base", "")} if self.upstream else None,
                "boxes": [{k: b.get(k) for k in ("id", "label", "base", "status", "last_ok", "gpus", "models", "added", "via", "error")}
                          for b in sorted(self.boxes.values(), key=lambda x: x.get("added") or 0)]}

    # ------------------------------------------------------------ linking

    async def _get(self, base: str, path: str, token: str, timeout: float = 8.0):
        async with self.hub.http.get(base + path, headers={"Authorization": f"Bearer {token}"},
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                raise ApiError(502, f"the box answered {r.status} on {path}" + (" (wrong token?)" if r.status == 401 else ""))
            return await r.json()

    async def _info(self, base: str, token: str) -> dict:
        """GET /api/box; an older box has no such path: build the same from /api/state and /api/boot."""
        try:
            return await self._get(base, "/api/box", token)
        except ApiError as e:
            if "401" in str(e):
                raise
        st = await self._get(base, "/api/state", token)
        try:
            boot = await self._get(base, "/api/boot", token)
        except ApiError:
            boot = {}
        models = []
        for m in st.get("models") or []:
            mm = LOCAL_URL.match(m.get("base_url") or "")
            if mm and m.get("kind") != "mock":
                models.append({"name": m.get("name"), "served_name": m.get("served_name"), "port": int(mm.group(1)),
                               "status": m.get("status"), "context_window": m.get("context_window"), "concurrency": 0})
        return {"id": "b" + hashlib.sha1(base.encode()).hexdigest()[:8], "link": base, "gpus": boot.get("gpus") or [],
                "models": models}

    def _label(self, info: dict, n: int) -> str:
        g = info.get("gpus") or []
        if not g:
            return f"Box {n}"
        name = re.sub(r"NVIDIA |Tesla |-SXM4|-PCIE", "", g[0].get("name") or "GPU")
        return f"{name}" + (f" x{len(g)}" if len(g) > 1 else "") + f" box {n}"

    async def add(self, link: str, via: str = "added") -> dict:
        base, token = split_link(link)
        token = token or self.hub.cfg.token
        me = (self.hub.public_link or "").split("#")[0].rstrip("/")
        if me and base == me:
            raise ApiError(400, "that is this box's own link")
        try:
            info = await self._info(base, token)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ApiError(502, f"the box does not answer at {base} ({e.__class__.__name__}): is its cell still running?") from None
        bid = info.get("id") or "b" + hashlib.sha1(base.encode()).hexdigest()[:8]
        if bid == self.my_id:
            raise ApiError(400, "that is this box's own link")
        b = self.boxes.get(bid)
        if b is None:
            b = self.boxes[bid] = {"id": bid, "added": time.time(), "via": via,
                                   "label": self._label(info, len(self.boxes) + 2)}
        moved = b.get("base") and b["base"] != base
        b.update(base=base, token=token, gpus=info.get("gpus") or [], status="up", last_ok=time.time(), fails=0, error="")
        self._key_env(b)
        await self._sync(b, info)
        self._save()
        self._emit(b, "linked" if not moved else "moved")
        asyncio.ensure_future(self._tell(b))
        return {k: b.get(k) for k in ("id", "label", "base", "status", "gpus", "models")}

    async def remove(self, bid: str) -> dict:
        b = self.boxes.pop(bid, None)
        if b is None:
            raise ApiError(404, "no such box")
        for mid in (b.get("model_ids") or {}).values():
            if mid in self.hub.store.models:
                await self.hub.models.delete(mid)
        os.environ.pop(self._env_name(bid), None)
        self._save()
        return {"removed": bid}

    async def _sync(self, b: dict, info: dict) -> None:
        """Make this hub's remote models match the box's local models: one per server port."""
        ids: dict[str, str] = dict(b.get("model_ids") or {})
        big = max([int(g.get("mem_mb") or 0) for g in info.get("gpus") or []] or [0]) >= 40000
        # A box with this hub's own token (the usual fleet) uses SABHA_TOKEN, which every process started from the hub has
        # (a research daemon started before the box was linked does not have SABHA_BOX_KEY_<id>: it got 401s).
        same = (b.get("token") or "") == (self.hub.cfg.token or "") and os.environ.get("SABHA_TOKEN") == self.hub.cfg.token
        key_env = "SABHA_TOKEN" if same else self._env_name(b["id"])
        seen = set()
        for m in info.get("models") or []:
            port = str(m["port"])
            seen.add(port)
            short = re.sub(r"\s*\((deep|reader|m\d+)\)\s*$", "", m.get("name") or m.get("served_name") or "model")
            want = {"name": f"{short} @ {b['label']}"[:80], "kind": "chat",
                    "base_url": f"{b['base']}/api/relay/{port}/v1", "served_name": m.get("served_name") or "",
                    "context_window": int(m.get("context_window") or 32768), "api_key_env": key_env,
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                    "concurrency": int(m.get("concurrency") or 0) or (16 if big else 0),
                    "notes": f"linked box {b['id']} ({b['label']}), port {port}"}
            mid = ids.get(port)
            cur = self.hub.store.models.get(mid) if mid else None
            if cur is None:
                cur = self.hub.models.create(want)
                ids[port] = cur["id"]
            elif any(cur.get(k) != v for k, v in want.items() if k != "kind"):
                self.hub.models.patch(cur["id"], {k: v for k, v in want.items() if k != "kind"})
        # a model gone from the box is kept (its status goes down): a box that restarts lists nothing for a while,
        # and deleting would unseat the crew. Removing the box removes its models.
        b["model_ids"] = ids
        b["models"] = [{"name": m.get("name"), "served_name": m.get("served_name"), "port": m["port"], "status": m.get("status")}
                       for m in info.get("models") or []]
        self.hub.store.mark_dirty()

    async def _tell(self, b: dict) -> None:
        """Tell a box where this hub is, so it announces itself here after its own link changes."""
        me = self.hub.public_link or ""
        if not me or (me.startswith("http://127.0.0.1") and not LOCAL_OK):
            return
        try:
            async with self.hub.http.post(b["base"] + "/api/boxes/hub", json={"link": me},
                                          headers={"Authorization": f"Bearer {b.get('token') or self.hub.cfg.token}"},
                                          timeout=aiohttp.ClientTimeout(total=8)) as r:
                await r.read()
        except Exception:  # noqa: BLE001 (an older box without the path, or down: the next poll tries again)
            pass

    def set_upstream(self, link: str) -> dict:
        base, tok = split_link(link)
        self.upstream = {"base": base, "token": tok or self.hub.cfg.token}
        self._announced = (0.0, "")
        self._save()
        return {"hub": base}

    def _emit(self, b: dict, what: str) -> None:
        self.hub.bus.emit("box.updated", {"box": {k: b.get(k) for k in ("id", "label", "base", "status", "error")}, "what": what})

    # ------------------------------------------------------------ the loop

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(min(5, POLL_S))
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("boxes tick failed")
            await asyncio.sleep(POLL_S)

    async def _tick(self) -> None:
        me = self.hub.public_link or ""
        if self.boxes and me and me != self._told_link and (LOCAL_OK or not me.startswith("http://127.0.0.1")):
            await asyncio.gather(*(self._tell(b) for b in self.boxes.values()))  # our link changed: tell every box
            self._told_link = me
        await asyncio.gather(*(self._poll(b) for b in list(self.boxes.values())))
        await self._announce()

    async def _poll(self, b: dict) -> None:
        try:
            info = await self._info(b["base"], b.get("token") or self.hub.cfg.token)
        except Exception as e:  # noqa: BLE001
            b["fails"] = b.get("fails", 0) + 1
            b["error"] = str(e)[:160] or e.__class__.__name__
            if b["fails"] >= DOWN_AFTER and b.get("status") != "down":
                b["status"] = "down"
                self._save()
                self._emit(b, "down")
                await self.hub.notifier.asend("box", f"Box down: {b['label']}",
                                              f"{b['base']} has not answered for {int(DOWN_AFTER * POLL_S)} s. Its models are "
                                              "skipped until it is back. If its tunnel changed, the box re-joins by itself; "
                                              "if its session ended, start it again from Nirmana, Add a box.")
            return
        was = b.get("status")
        b.update(status="up", last_ok=time.time(), fails=0, error="", gpus=info.get("gpus") or b.get("gpus") or [])
        await self._sync(b, info)
        self._save()
        if was == "down":
            self._emit(b, "up")
            await self.hub.notifier.asend("box", f"Box back: {b['label']}", f"{b['base']} answers again.")

    async def _announce(self) -> None:
        """A box with an upstream hub reports its link there: at once when the link changed, else every 5 min."""
        if not self.upstream or not self.hub.public_link or (self.hub.public_link.startswith("http://127.0.0.1") and not LOCAL_OK):
            return
        last_t, last_link = self._announced
        if self.hub.public_link == last_link and time.time() - last_t < ANNOUNCE_EVERY_S:
            return
        try:
            async with self.hub.http.post(self.upstream["base"] + "/api/boxes/announce", json={"link": self.hub.public_link},
                                          headers={"Authorization": f"Bearer {self.upstream.get('token') or ''}"},
                                          timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    self._announced = (time.time(), self.hub.public_link)
                else:
                    log.warning("announce to %s: %s", self.upstream["base"], r.status)
        except Exception as e:  # noqa: BLE001 (the hub is restarting: it tells us its new link when it is back)
            log.info("announce to %s failed: %s", self.upstream.get("base"), e.__class__.__name__)


# ---------------------------------------------------------------- routes

def add_routes(r, hub_of, body_of) -> None:
    from aiohttp import web

    async def me(request):
        return web.json_response(hub_of(request).boxes.self_info())

    async def listing(request):
        return web.json_response(hub_of(request).boxes.public())

    async def add(request):
        body = await body_of(request)
        return web.json_response(await hub_of(request).boxes.add(str(body.get("link") or "")))

    async def announce(request):
        body = await body_of(request)
        return web.json_response(await hub_of(request).boxes.add(str(body.get("link") or ""), via="joined"))

    async def set_hub(request):
        body = await body_of(request)
        return web.json_response(hub_of(request).boxes.set_upstream(str(body.get("link") or "")))

    async def remove(request):
        return web.json_response(await hub_of(request).boxes.remove(request.match_info["id"]))

    async def update(request):
        """{models?: bool, hub?: bool}: git pull this box's Sabha, then restart its model servers and/or its hub on the
        new code (box.py update, detached so it outlives the hub it restarts). The way to upgrade a box over its link."""
        body = await body_of(request)
        app = str(Path(__file__).resolve().parent.parent)

        def pull() -> dict:
            def git(*a):
                r = subprocess.run(["git", "-C", app, *a], capture_output=True, text=True, timeout=90)
                return r.returncode, (r.stdout + r.stderr).strip()
            _, before = git("rev-parse", "--short", "HEAD")
            rc, out = git("pull", "-q", "--ff-only")
            _, after = git("rev-parse", "--short", "HEAD")
            return {"before": before, "after": after, "ok": rc == 0, "log": out[-400:]}
        res = await asyncio.to_thread(pull)
        want = [w for w in ("models", "hub") if body.get(w)]
        if want and res["ok"]:
            box_py = os.path.join(app, "sabha", "box.py")
            logs = os.path.join(hub_of(request).cfg.data_dir, "logs")
            os.makedirs(logs, exist_ok=True)
            with open(os.path.join(logs, "update.log"), "ab") as f:
                subprocess.Popen([sys.executable, box_py, "update", *want], cwd=app, env=dict(os.environ), stdin=subprocess.DEVNULL,
                                 stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        return web.json_response({**res, "restarting": want if res["ok"] else []})

    r.add_get("/api/box", me)
    r.add_get("/api/boxes", listing)
    r.add_post("/api/boxes", add)
    r.add_post("/api/boxes/announce", announce)
    r.add_post("/api/boxes/hub", set_hub)
    r.add_delete("/api/boxes/{id}", remove)
    r.add_post("/api/box/update", update)
