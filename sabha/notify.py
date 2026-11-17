"""Tell the owner when something happens, on their phone: push (ntfy), Telegram, WhatsApp, email, a phone call.

Every channel is optional and set in the app (Settings > Notifications) or from Kaggle / Colab secrets
(sabha/box.py copies them into <data>/notify.json, mode 600). A secret never goes back to the browser.

    ntfy      topic [+ server]. Free, no account: install the ntfy app and subscribe to the topic.
              The topic name is the password: anyone who knows it reads the messages, so make it long.
    telegram  bot token + chat id. Free. Two-way: the owner texts the box (/status, /research ..., /report).
    whatsapp  phone + CallMeBot API key. Free for personal use (callmebot.com: message their number once).
    email     SMTP host, port, user, password (for Gmail an app password), to.
    call      Twilio sid + token + from + to (a real phone call that reads the message out), or a free
              CallMeBot Telegram voice call to a Telegram @username.

Events (kind, and who gets it by default):
    link            the box is reachable: the app link (sent at once, so the phone can open it)      important
    box_ready       the models answer: research can start                                            all
    run_started     a research run began                                                             all
    wave            a wave closed: its headline (research/waves.py writes state/notify.jsonl)        all
    run_done        a run finished: the answer's first sentence and the link                         important + call
    problem         the daemon died, a model stopped answering                                       important + call
    session_ending  30 minutes left on the box: download the results                                 important
Each channel has a level: all, important, off. Calls only ring for run_done and problem.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import re
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from .util import ApiError

log = logging.getLogger("sabha.notify")

CHANNELS = {  # channel -> (fields, secret fields)
    "ntfy": (("topic", "server"), ("topic",)),
    "telegram": (("bot_token", "chat_id"), ("bot_token",)),
    "whatsapp": (("phone", "apikey"), ("apikey",)),
    "email": (("host", "port", "user", "password", "to"), ("password",)),
    "call": (("provider", "twilio_sid", "twilio_token", "twilio_from", "to", "telegram_user"), ("twilio_token",)),
}
LEVELS = ("all", "important", "off")
IMPORTANT = {"link", "run_done", "problem", "session_ending"}
CALL_EVENTS = {"run_done", "problem"}
DEFAULT_LEVEL = {"ntfy": "all", "telegram": "all", "whatsapp": "important", "email": "important", "call": "important"}
SECRET_ENV = {  # Kaggle / Colab secret name -> (channel, field); box.py imports these
    "NTFY_TOPIC": ("ntfy", "topic"), "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"), "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "WHATSAPP_PHONE": ("whatsapp", "phone"), "CALLMEBOT_KEY": ("whatsapp", "apikey"),
    "SMTP_HOST": ("email", "host"), "SMTP_PORT": ("email", "port"), "SMTP_USER": ("email", "user"),
    "SMTP_PASSWORD": ("email", "password"), "NOTIFY_EMAIL": ("email", "to"),
    "TWILIO_SID": ("call", "twilio_sid"), "TWILIO_TOKEN": ("call", "twilio_token"), "TWILIO_FROM": ("call", "twilio_from"),
    "CALL_TO": ("call", "to"), "CALL_TELEGRAM_USER": ("call", "telegram_user"),
}
TIMEOUT = 20
HELP = ("Sabha on your phone. Commands:\n"
        "/status  what the box and the agents are doing\n"
        "/research <question>  start research (add 'for 4h', 'until end' or 'instant')\n"
        "/report  where the research stands now\n"
        "/tell <instruction>  tell every research agent something\n"
        "/finish  write the final report now and end the run\n"
        "/stop  stop the run at once\n"
        "/link  the app link")


def _clip(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = TIMEOUT) -> tuple[int, str]:
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "sabha-box/1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(600).decode("utf-8", "replace")


class Notifier:
    def __init__(self, hub):
        self.hub = hub
        self.path = Path(hub.cfg.data_dir) / "notify.json"
        self.offsets_path = Path(hub.cfg.data_dir) / "run" / "notify_offsets.json"
        self.lock = threading.Lock()
        self.cfg = self._read()
        self.recent: list[dict] = []
        self.tasks: list[asyncio.Task] = []
        self.sent_once: set[str] = set()
        self.tg_offset = 0
        self.last_run: dict = {}

    # ------------------------------------------------------------ config

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def ch(self, name: str) -> dict:
        return dict(self.cfg.get(name) or {})

    def ready(self, name: str) -> bool:
        c = self.ch(name)
        if name == "ntfy":
            return bool(c.get("topic"))
        if name == "telegram":
            return bool(c.get("bot_token") and c.get("chat_id"))
        if name == "whatsapp":
            return bool(c.get("phone") and c.get("apikey"))
        if name == "email":
            return bool(c.get("host") and c.get("user") and c.get("password") and c.get("to"))
        if name == "call":
            if (c.get("provider") or "twilio") == "telegram":
                return bool(c.get("telegram_user"))
            return bool(c.get("twilio_sid") and c.get("twilio_token") and c.get("twilio_from") and c.get("to"))
        return False

    def level(self, name: str) -> str:
        lv = self.ch(name).get("level")
        return lv if lv in LEVELS else DEFAULT_LEVEL[name]

    def public(self) -> dict:
        out = {}
        for name, (fields, secrets) in CHANNELS.items():
            c = self.ch(name)
            row = {"ready": self.ready(name), "level": self.level(name)}
            for f in fields:
                v = str(c.get(f) or "")
                if f in secrets:
                    row[f] = {"set": bool(v), "hint": ("…" + v[-4:]) if len(v) >= 10 else ("set" if v else "")}
                else:
                    row[f] = v
            out[name] = row
        out["recent"] = self.recent[-20:][::-1]
        return out

    def update(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "send an object of channels")
        with self.lock:
            for name, vals in body.items():
                if name not in CHANNELS or not isinstance(vals, dict):
                    continue
                fields, _ = CHANNELS[name]
                cur = self.ch(name)
                for f, v in vals.items():
                    if f == "level":
                        if v not in LEVELS:
                            raise ApiError(400, f"{name}.level must be one of {', '.join(LEVELS)}")
                        cur["level"] = v
                        continue
                    if f not in fields:
                        continue
                    v = "" if v is None else str(v).strip()
                    if len(v) > 300 or ("\n" in v):
                        raise ApiError(400, f"{name}.{f} must be one short line")
                    if v:
                        cur[f] = v
                    else:
                        cur.pop(f, None)
                if name == "ntfy" and cur.get("topic") and not re.fullmatch(r"[A-Za-z0-9_\-]{6,64}", cur["topic"]):
                    raise ApiError(400, "the ntfy topic must be 6-64 letters, digits, - or _ (make it long: it is the password)")
                self.cfg[name] = cur
            self._write()
        return self.public()

    def import_env(self, env: dict) -> list[str]:
        """Fill empty fields from secrets (box.py passes the names it found). Returns the names used."""
        got = []
        with self.lock:
            for var, (name, field) in SECRET_ENV.items():
                v = str(env.get(var) or "").strip()
                if v and not (self.cfg.get(name) or {}).get(field):
                    self.cfg.setdefault(name, {})[field] = v
                    got.append(var)
            if got:
                self._write()
        return got

    # ------------------------------------------------------------ sending

    def send(self, kind: str, title: str, text: str = "", link: str = "", only: str = "") -> list[dict]:
        """Blocking: one message to every channel whose level wants this kind. Returns one result per channel."""
        link = link or self.app_link()
        results = []
        for name in CHANNELS:
            if only and name != only:
                continue
            if not self.ready(name):
                continue
            lv = self.level(name)
            if not only and (lv == "off" or (lv == "important" and kind not in IMPORTANT)):
                continue
            if name == "call" and not only and kind not in CALL_EVENTS:
                continue
            t0 = time.time()
            try:
                ok, info = getattr(self, f"_send_{name}")(kind, title, text, link)
            except Exception as e:  # a channel must never break the hub
                ok, info = False, f"{type(e).__name__}: {str(e)[:160]}"
            r = {"channel": name, "kind": kind, "title": _clip(title, 120), "ok": ok, "info": _clip(info, 200),
                 "t": round(t0, 1), "ms": int((time.time() - t0) * 1000)}
            results.append(r)
            self.recent = (self.recent + [r])[-40:]
            (log.info if ok else log.warning)("notify %s %s: %s %s", name, kind, "ok" if ok else "FAILED", info[:160])
        return results

    async def asend(self, kind: str, title: str, text: str = "", link: str = "", only: str = "") -> list[dict]:
        return await asyncio.to_thread(self.send, kind, title, text, link, only)

    def app_link(self) -> str:
        return getattr(self.hub, "public_link", "") or ""

    def _send_ntfy(self, kind, title, text, link):
        c = self.ch("ntfy")
        server = (c.get("server") or "https://ntfy.sh").rstrip("/")
        prio = 4 if kind in ("problem", "run_done", "session_ending") else 3
        tags = {"problem": "warning", "run_done": "white_check_mark", "wave": "ocean", "link": "link",
                "box_ready": "rocket", "run_started": "mag", "session_ending": "hourglass"}.get(kind, "bell")
        # JSON publishing (POST to the server root): titles and text stay UTF-8, which ntfy's headers cannot carry
        msg = {"topic": c["topic"], "title": _clip(title, 200), "message": _clip(text or title, 3800), "priority": prio,
               "tags": [tags], "markdown": True}
        if link:
            msg["click"] = link
            msg["actions"] = [{"action": "view", "label": "Open Sabha", "url": link}]
        code, body = _http("POST", server + "/", json.dumps(msg).encode("utf-8"), {"Content-Type": "application/json"})
        return code == 200, f"HTTP {code}" + ("" if code == 200 else f" {body[:120]}")

    def _send_telegram(self, kind, title, text, link):
        c = self.ch("telegram")
        msg = f"<b>{_esc(title)}</b>" + (f"\n{_esc(_clip(text, 3400))}" if text else "") + (f"\n\n{_esc(link)}" if link else "")
        return self.tg_send(c["chat_id"], msg, html=True)

    def tg_send(self, chat_id: str, msg: str, html: bool = False) -> tuple[bool, str]:
        c = self.ch("telegram")
        data = {"chat_id": chat_id, "text": _clip(msg, 4000), "disable_web_page_preview": "true"}
        if html:
            data["parse_mode"] = "HTML"
        code, body = _http("POST", f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
                           urllib.parse.urlencode(data).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        if code == 400 and html:  # a stray < or & in an agent's text: send it plain
            return self.tg_send(chat_id, re.sub(r"</?b>", "", msg).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
        return code == 200, f"HTTP {code}" + ("" if code == 200 else f" {body[:120]}")

    def _send_whatsapp(self, kind, title, text, link):
        c = self.ch("whatsapp")
        msg = f"*{title}*" + (f"\n{_clip(text, 900)}" if text else "") + (f"\n{link}" if link else "")
        q = urllib.parse.urlencode({"phone": c["phone"], "text": msg, "apikey": c["apikey"]})
        code, body = _http("GET", f"https://api.callmebot.com/whatsapp.php?{q}")
        ok = code == 200 and "error" not in body.lower()[:300]
        return ok, f"HTTP {code}" + ("" if ok else f" {re.sub(r'<[^>]+>', ' ', body)[:120]}")

    def _send_email(self, kind, title, text, link):
        c = self.ch("email")
        m = EmailMessage()
        m["Subject"] = _clip(f"Sabha: {title}", 180)
        m["From"] = c["user"]
        m["To"] = c["to"]
        m.set_content((text or title) + (f"\n\nOpen Sabha: {link}" if link else "") + "\n\n(sent by your Sabha box)")
        port = int(c.get("port") or 465)
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(c["host"], port, timeout=TIMEOUT, context=ctx) as s:
                s.login(c["user"], c["password"])
                s.send_message(m)
        else:
            with smtplib.SMTP(c["host"], port, timeout=TIMEOUT) as s:
                s.starttls(context=ctx)
                s.login(c["user"], c["password"])
                s.send_message(m)
        return True, f"sent to {c['to']}"

    def _send_call(self, kind, title, text, link):
        c = self.ch("call")
        say = _clip(re.sub(r"https?://\S+|\[[^\]]*\]|[*_#`]", " ", f"Sabha. {title}. {text}"), 600)
        if (c.get("provider") or "twilio") == "telegram":
            q = urllib.parse.urlencode({"user": c["telegram_user"], "text": say, "lang": "en-US-Standard-C", "rpt": "2"})
            code, body = _http("GET", f"https://api.callmebot.com/start.php?{q}", timeout=40)
            return code == 200, f"HTTP {code}"
        twiml = f"<Response><Say voice=\"alice\">{_esc(say)}</Say><Pause length=\"1\"/><Say voice=\"alice\">{_esc(say)}</Say></Response>"
        auth = base64.b64encode(f"{c['twilio_sid']}:{c['twilio_token']}".encode()).decode()
        data = urllib.parse.urlencode({"To": c["to"], "From": c["twilio_from"], "Twiml": twiml}).encode()
        code, body = _http("POST", f"https://api.twilio.com/2010-04-01/Accounts/{c['twilio_sid']}/Calls.json", data,
                           {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
        return code in (200, 201), f"HTTP {code}" + ("" if code in (200, 201) else f" {body[:120]}")

    # ------------------------------------------------------------ Telegram: find the chat, read commands

    def tg_find_chat(self) -> dict:
        """After the owner messages the bot once: the newest chat that wrote to it (the app saves it as chat_id)."""
        c = self.ch("telegram")
        if not c.get("bot_token"):
            raise ApiError(400, "save the bot token first")
        code, body = _http("GET", f"https://api.telegram.org/bot{c['bot_token']}/getUpdates?timeout=0&limit=50")
        if code != 200:
            raise ApiError(502, f"Telegram answered HTTP {code}: is the bot token right?")
        chats = []
        for u in json.loads(body).get("result") or []:
            msg = u.get("message") or u.get("edited_message") or {}
            ch = msg.get("chat") or {}
            if ch.get("id"):
                chats.append({"id": str(ch["id"]), "name": ch.get("username") or ch.get("first_name") or ch.get("title") or ""})
        if not chats:
            raise ApiError(404, "no message yet: open your bot in Telegram, send it any message, then try again")
        found = chats[-1]
        self.update({"telegram": {"chat_id": found["id"]}})
        return found

    def _tg_poll(self) -> list[dict]:
        c = self.ch("telegram")
        if not (c.get("bot_token") and c.get("chat_id")):
            return []
        code, body = _http("GET", f"https://api.telegram.org/bot{c['bot_token']}/getUpdates?timeout=20&offset={self.tg_offset}",
                           timeout=30)
        if code != 200:
            return []
        out = []
        for u in json.loads(body).get("result") or []:
            self.tg_offset = max(self.tg_offset, int(u.get("update_id", 0)) + 1)
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) == str(c["chat_id"]) and msg.get("text"):  # only the owner's chat
                out.append({"text": msg["text"], "date": msg.get("date", 0)})
        return out

    async def _telegram_loop(self) -> None:
        if self.ready("telegram"):  # skip what was sent while the hub was down only once: old commands must not replay
            try:
                await asyncio.to_thread(self._tg_poll)
            except Exception:
                pass
        while True:
            try:
                if not self.ready("telegram"):
                    await asyncio.sleep(15)
                    continue
                for m in await asyncio.to_thread(self._tg_poll):
                    reply = await self.command(m["text"])
                    if reply:
                        await asyncio.to_thread(self.tg_send, self.ch("telegram")["chat_id"], reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram loop: %s", e)
                await asyncio.sleep(20)

    async def command(self, text: str) -> str:
        """One message from the owner's phone -> the reply. The same commands work from any two-way channel."""
        hub = self.hub
        t = text.strip()
        cmd, _, arg = t.partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip()
        if cmd in ("/start", "/help", "help"):
            return HELP
        if cmd == "/link":
            return self.app_link() or "No public link yet."
        if cmd == "/status":
            return await asyncio.to_thread(self.status_text)
        if cmd == "/report":
            return await asyncio.to_thread(self.report_text)
        if cmd in ("/stop", "/finish"):
            try:
                st = await asyncio.to_thread(hub.runner.stop, "finish" if cmd == "/finish" else "now")
            except ApiError as e:
                return f"Could not: {e.message}"
            return "Finishing: final reports and the answer now." if cmd == "/finish" and st.get("running") else "Stopped."
        if cmd == "/tell":
            if not arg:
                return "Write the instruction after /tell."
            try:
                await asyncio.to_thread(hub.research.command, {"cmd": "instruct", "text": arg, "track": "all"})
            except ApiError as e:
                return f"Could not: {e.message}"
            return "Told every agent. They read it at their next step."
        if cmd == "/research":
            if len(arg) < 12:
                return "Write the question after /research, a sentence or more. Add 'for 4h', 'until end' or 'instant'."
            mode, hours = "continuous", 0.0
            m = re.search(r"\b(?:for\s+)?(\d+(?:\.\d+)?)\s*h(?:ours?)?\b", arg, re.I)
            if re.search(r"\binstant\b|\bquick\b", arg, re.I):
                mode = "instant"
            elif m:
                hours = float(m.group(1))
            elif not re.search(r"until (the )?end", arg, re.I):
                hours = 4.0
            q = re.sub(r"\b(for\s+)?\d+(\.\d+)?\s*h(ours?)?\b|\buntil (the )?end\b|\binstant\b|\bquick\b", "", arg, flags=re.I).strip(" ,.;")
            try:
                r = await asyncio.to_thread(hub.runner.start, {"topic": q, "mode": mode, **({"hours": hours} if mode == "continuous" else {})})
            except ApiError as e:
                return f"Did not start: {e.message}"
            return (f"Research started: {r.get('title')}\n"
                    + ("Instant: one pass, about 20 minutes." if mode == "instant" else f"Continuous for {r.get('hours', 0):g} h, in waves.")
                    + " I will text you as it goes.")
        if t.startswith("/"):
            return "I do not know that command.\n\n" + HELP
        return ("I read commands for now. To start research send:\n/research " + _clip(t, 300) + " for 4h\n\n" + HELP)

    # ------------------------------------------------------------ texts

    def status_text(self) -> str:
        hub = self.hub
        from .research_run import read_boot
        b = read_boot(hub.cfg.data_dir)
        lines = []
        if b.get("present"):
            lines.append("Box: " + ("ready" if b.get("ready") else ", ".join(f"{s['short']} {s['status']}" for s in b.get("steps") or [])))
        st = hub.runner.status()
        if st.get("running"):
            ov = hub.research.overview(st.get("mission"))
            m = ov.get("mission") or {}
            s = m.get("stats") or {}
            w = m.get("waves") or {}
            lines.append(f"Research: {m.get('title', '')}")
            lines.append(f"{s.get('cards', 0)} papers read, {s.get('queued', 0)} waiting, {s.get('per_hour', 0)}/h, "
                         f"{(s.get('left_m') if s.get('left_m') is not None else '?')} min left")
            if w.get("on"):
                lines.append(f"Wave {w['n']}" + (f", last: {w['history'][-1]['headline']}" if w.get("history") else ""))
        elif st.get("mission"):
            lines.append(f"No run going. Last run {st.get('mission')}: {st.get('how') or 'ended'}.")
        else:
            lines.append("No research run yet. Send /research <question>.")
        return "\n".join(lines)

    def report_text(self, limit: int = 3500) -> str:
        hub = self.hub
        st = hub.runner.status()
        mid = st.get("mission")
        if not mid:
            return "No research run yet."
        root = os.path.join(hub.cfg.research_repo, "missions", mid)
        for name in ("synthesis.md", "knowledge.md"):
            try:
                text = open(os.path.join(root, name), encoding="utf-8").read()
            except OSError:
                continue
            text = re.sub(r"\[(\d{4}\.\d{4,5})\]", r"(arXiv \1)", text)
            return _clip(re.sub(r"\n{3,}", "\n\n", text), limit) + ("\n\nFull report: " + self.app_link() if self.app_link() else "")
        return "Nothing written yet: the first wave is still reading."

    # ------------------------------------------------------------ watching the box

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._watch(), name="notify-watch"),
                      asyncio.create_task(self._telegram_loop(), name="notify-telegram")]

    async def close(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    def once(self, key: str) -> bool:
        if key in self.sent_once:
            return False
        self.sent_once.add(key)
        return True

    def on_link(self, link: str) -> None:
        """The hub's public link is known (called by __main__ right after the tunnel comes up)."""
        self.hub.public_link = link
        if link.startswith("http://127.0.0.1") or not self.once("link:" + link):
            return
        asyncio.get_running_loop().create_task(self.asend(
            "link", "Sabha is up", "Your box is reachable. Open this link on your phone. The models need about 15 minutes "
            "the first time; the app shows the progress.", link))

    def _offsets(self) -> dict:
        try:
            d = json.loads(self.offsets_path.read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_offsets(self, d: dict) -> None:
        self.offsets_path.parent.mkdir(parents=True, exist_ok=True)
        self.offsets_path.write_text(json.dumps(d), encoding="utf-8")

    def _scan(self) -> list[tuple[str, str, str]]:
        """New lines of every mission's state/notify.jsonl since the last scan -> (kind, title, text)."""
        out = []
        offs = self._offsets()
        first = not offs.get("_init")
        for p in glob.glob(os.path.join(self.hub.cfg.research_repo, "missions", "*", "state", "notify.jsonl")):
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            at = int(offs.get(p, 0 if not first else size))
            if size < at:
                at = 0
            if size > at:
                with open(p, "rb") as f:
                    f.seek(at)
                    chunk = f.read(size - at).decode("utf-8", "replace")
                mid = p.split(os.sep + "missions" + os.sep)[-1].split(os.sep)[0]
                for line in chunk.splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    out.append(self._event_text(mid, e))
            offs[p] = size
        offs["_init"] = 1
        self._save_offsets(offs)
        return [x for x in out if x]

    def _title_of(self, mid: str) -> str:
        for m in self.hub.research.missions():
            if m["id"] == mid:
                return m["title"]
        return mid

    def _event_text(self, mid: str, e: dict):
        title = _clip(self._title_of(mid), 80)
        if e.get("kind") == "wave":
            if e.get("final"):
                return None  # the run_done message follows at once
            return ("wave", f"Wave {e.get('n')} done · {title}",
                    f"{e.get('headline', '')}\n\n{e.get('cards', 0)} papers this wave. The agents cleared their context and "
                    f"started wave {int(e.get('n') or 0) + 1} from the new state of knowledge.")
        if e.get("kind") == "done":
            return ("run_done", f"Research finished · {title}",
                    f"{e.get('headline', '')}\n\n{e.get('cards', 0)} papers read" +
                    (f" in {e.get('waves')} waves" if e.get("waves") else "") + ". The full report is in the app (Research, Report).")
        return None

    async def _watch(self) -> None:
        from .research_run import read_boot
        hub = self.hub
        was_ready = None
        while True:
            try:
                await asyncio.sleep(15)
                for kind, title, text in await asyncio.to_thread(self._scan):
                    await self.asend(kind, title, text)
                b = await asyncio.to_thread(read_boot, hub.cfg.data_dir)
                if b.get("present"):
                    if was_ready is False and b.get("ready") and self.once("box_ready"):
                        await self.asend("box_ready", "Models ready", "The models answer. Research, Talk and Work can start.")
                    was_ready = bool(b.get("ready"))
                    for s in b.get("steps") or []:  # a model server that was up and stopped answering (once an hour per role)
                        if str(s.get("id", "")).startswith("model:") and str(s.get("detail", "")).startswith("stopped answering") \
                                and self.once(f"down:{s['id']}:{int(time.time() // 3600)}"):
                            await self.asend("problem", f"The {s.get('short')} model stopped answering",
                                             "The box is restarting it (about 5 to 10 minutes). Research waits and picks up again "
                                             "on its own. " + str(s.get("detail", ""))[:200])
                st = await asyncio.to_thread(hub.runner.status)
                key = st.get("mission") or ""
                prev = self.last_run
                if key and st.get("running") and prev.get("mission") != key and self.once("start:" + key):
                    await self.asend("run_started", f"Research started · {_clip(self._title_of(key), 80)}",
                                     f"{'Instant: one pass' if st.get('mode') == 'instant' else 'Continuous'}, about "
                                     f"{float(st.get('hours') or 0):g} h. You get a message after every wave.")
                if key and prev.get("mission") == key and prev.get("running") and not st.get("running"):
                    how = st.get("how") or ""
                    if "finished" not in how and "app" not in how and self.once("died:" + key):
                        await self.asend("problem", "Research stopped by itself",
                                         f"The run '{_clip(self._title_of(key), 80)}' ended: {how}. What it wrote is kept; "
                                         "start a new run from the app.")
                self.last_run = {"mission": key, "running": bool(st.get("running"))}
                left = getattr(hub, "session_end", 0) - time.time()
                if 0 < left < 30 * 60 and self.once("session_ending"):
                    await self.asend("session_ending", "30 minutes left on the box",
                                     "The Kaggle / Colab session ends soon. Download the results from the app now "
                                     "(Research, Download). Long-term memory is in the zip too.")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("notify watch: %s", e)


def _esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
