"""The research library: every finished report, kept past the box (PROTOCOL.md section 17).

A Kaggle or Colab box is wiped when its session ends, so a report that lives only in the box's research folder is
lost with it. The library keeps each finished report as a page that looks exactly like the app's reader (the
standalone .html the reader builds), with its markdown, its lab notebook and a small meta.json:

- on the box, in <data>/library/<lid>/;
- in GitHub, when a repository and a token are set (settings git_repo + git_token, or the SABHA_GIT_REPO and
  SABHA_GIT_TOKEN secrets), under library/<lid>/ with library/index.json listing them all. A fresh box lists the
  GitHub library and opens any old report from there, so past research is never "new" again.

Writes go through the GitHub contents API (one HTTPS call per file): no clone, nothing to merge. <lid> is the
mission's start date plus its id, so a new box that numbers its missions from 001 again never overwrites an old one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

from .util import ApiError

log = logging.getLogger("sabha.library")
SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,120}$")
REMOTE_TTL_S = 90
MAX_HTML = 6 * 2**20


def _read(path: str, limit: int = 2 * 2**20) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Library:
    def __init__(self, hub):
        self.hub = hub
        self.dir = os.path.join(str(hub.cfg.data_dir), "library")
        self._remote: tuple[float, list[dict], str] = (0.0, [], "")
        self._lock = threading.Lock()

    # ------------------------------------------------------------ GitHub (contents API)

    def _gh(self) -> tuple[str, str]:
        s = self.hub.settings
        repo = s.get("git_repo") or os.environ.get("SABHA_GIT_REPO", "")
        tok = s.get("git_token") or os.environ.get("SABHA_GIT_TOKEN", "")
        return (repo, tok) if repo and tok else ("", "")

    def _call(self, method: str, path: str, body: dict | None = None, raw: bool = False, timeout: float = 30):
        repo, tok = self._gh()
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {tok}", "User-Agent": "sabha-library",
                                              "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return data if raw else json.loads(data or b"{}")

    def _sha(self, path: str) -> str:
        try:
            return self._call("GET", path).get("sha", "")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ""
            raise

    def _put(self, path: str, text: str, msg: str) -> None:
        body = {"message": msg, "content": base64.b64encode(text.encode()).decode()}
        sha = self._sha(path)
        if sha:
            body["sha"] = sha
        self._call("PUT", path, body, timeout=60)

    def _remote_index(self, fresh: bool = False) -> tuple[list[dict], str]:
        """library/index.json from GitHub (cached a minute and a half). ([], error) when it cannot be read."""
        if not self._gh()[0]:
            return [], ""
        t, rows, err = self._remote
        if not fresh and time.time() - t < REMOTE_TTL_S:
            return rows, err
        try:
            rows, err = json.loads(self._call("GET", "library/index.json", raw=True) or b"[]"), ""
        except urllib.error.HTTPError as e:
            rows, err = [], "" if e.code == 404 else f"GitHub said {e.code}"
        except Exception as e:  # noqa: BLE001 (offline, DNS: the local library still works)
            rows, err = [], f"GitHub not reachable: {type(e).__name__}"
        self._remote = (time.time(), rows if isinstance(rows, list) else [], err)
        return self._remote[1], err

    # ------------------------------------------------------------ list

    def _local(self) -> dict[str, dict]:
        out = {}
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for n in names:
            try:
                meta = json.loads(_read(os.path.join(self.dir, n, "meta.json")) or "{}")
            except ValueError:
                continue
            if meta.get("lid"):
                out[meta["lid"]] = meta
        return out

    def _mission_meta(self, mid: str) -> dict:
        """What the library card shows, from a mission folder on this box."""
        res = self.hub.research
        if not res.available():
            raise ApiError(404, "no research on this box")
        root = os.path.join(res.repo, "missions", mid)
        if not SAFE.match(mid or "") or not os.path.isdir(root):
            raise ApiError(404, f"no mission {mid!r}")
        st = json.loads(_read(os.path.join(root, "state", "mission.json")) or "{}")
        plan = json.loads(_read(os.path.join(root, "plan.json")) or "{}")
        waves = json.loads(_read(os.path.join(root, "state", "waves.json")) or "{}")
        brief = _read(os.path.join(root, "brief.md"), 20000)
        tm = re.search(r"^title:\s*(.+)$", brief, re.M)
        from .research.waves import EMBED
        qm = re.search(r"^## The question\s*\n(.*?)(?=^#{1,2} |\Z)", brief, re.S | re.M)
        question = " ".join(EMBED.sub("", qm.group(1) if qm else "").split())
        started = str(st.get("started") or "")
        day = started[:10] if re.match(r"\d{4}-\d{2}-\d{2}", started) else time.strftime("%Y-%m-%d", time.gmtime())
        cards = len([f for f in os.listdir(os.path.join(root, "cards")) if f.endswith(".json")]) if os.path.isdir(os.path.join(root, "cards")) else 0
        return {"lid": f"{day}-{mid}"[:120], "mission": mid, "title": _read(os.path.join(root, "state", "report_title.txt"), 300).strip()
                or (tm.group(1).strip() if tm else mid), "question": question[:400], "one_line": plan.get("one_line", "")[:300],
                "started": started, "papers": cards, "waves": len(waves.get("history") or []),
                "tracks": len(plan.get("tracks") or []), "hours": plan.get("hours"), "status": st.get("status") or "",
                "final": os.path.isfile(os.path.join(root, "final.md")), "root": root}

    def list(self) -> dict:
        local = self._local()
        remote, err = self._remote_index()
        rows: dict[str, dict] = {}
        for r in remote:
            if isinstance(r, dict) and r.get("lid"):
                rows[r["lid"]] = {**r, "in_github": True, "on_box": False}
        for lid, r in local.items():
            rows[lid] = {**rows.get(lid, {}), **r, "on_box": True, "in_github": lid in rows or bool(r.get("in_github"))}
        missions = {}
        if self.hub.research.available():  # runs on this box that are not in the library yet (still running, or never opened)
            for m in self.hub.research.missions():
                missions[m["id"]] = m
        by_mission = {r.get("mission"): lid for lid, r in rows.items() if r.get("on_box")}
        for mid, m in missions.items():
            lid = by_mission.get(mid)
            if lid:
                rows[lid]["live_mission"] = mid
                rows[lid]["status"] = m["status"]
                continue
            try:
                meta = self._mission_meta(mid)
            except ApiError:
                continue
            meta.pop("root", None)
            rows[meta["lid"]] = {**rows.get(meta["lid"], {}), **meta, "live_mission": mid, "saved": False,
                                 "on_box": True, "in_github": meta["lid"] in rows and rows[meta["lid"]].get("in_github", False)}
        out = sorted(rows.values(), key=lambda r: (str(r.get("started") or r.get("saved_at") or ""), r.get("lid", "")), reverse=True)
        repo, _ = self._gh()
        return {"entries": out, "github": {"on": bool(repo), "repo": repo or self.hub.settings.get("git_repo") or "", "error": err}}

    # ------------------------------------------------------------ save

    def save(self, body: dict) -> dict:
        """Keep a mission's report: body {mission, html} (html is the reader's standalone page). Local first, then
        GitHub in the background; returns at once with the card."""
        mid = str(body.get("mission") or "")
        html = body.get("html")
        if not isinstance(html, str) or "<html" not in html[:400] or len(html) > MAX_HTML:
            raise ApiError(400, "html must be the reader's saved page (under 6 MB)")
        meta = self._mission_meta(mid)
        root = meta.pop("root")
        d = os.path.join(self.dir, meta["lid"])
        digest = hashlib.sha1(html.encode()).hexdigest()
        old = self._local().get(meta["lid"], {})
        files = {"report.html": html, "report.md": _read(os.path.join(root, "REPORT.md")),
                 "notebook.md": _read(os.path.join(root, "NOTEBOOK.md"))}
        for name, text in files.items():
            if text:
                _write(os.path.join(d, name), text)
        meta.update(saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), digest=digest,
                    in_github=bool(old.get("in_github")) and old.get("digest") == digest)
        _write(os.path.join(d, "meta.json"), json.dumps(meta, indent=1))
        if self._gh()[0] and not meta["in_github"]:
            threading.Thread(target=self._push, args=(meta, files), name=f"library-{meta['lid']}", daemon=True).start()
            meta["pushing"] = True
        return meta

    def _push(self, meta: dict, files: dict) -> None:
        lid = meta["lid"]
        with self._lock:
            try:
                for name, text in files.items():
                    if text:
                        self._put(f"library/{lid}/{name}", text, f"library: {meta['title'][:60]} ({name})")
                card = {k: v for k, v in meta.items() if k not in ("pushing", "in_github")}
                rows, _ = self._remote_index(fresh=True)
                rows = [r for r in rows if isinstance(r, dict) and r.get("lid") != lid] + [card]
                self._put("library/index.json", json.dumps(sorted(rows, key=lambda r: r.get("lid", "")), indent=1),
                          f"library: index ({len(rows)} reports)")
                self._remote = (time.time(), rows, "")
                meta["in_github"] = True
                _write(os.path.join(self.dir, lid, "meta.json"), json.dumps({k: v for k, v in meta.items() if k != "pushing"}, indent=1))
                log.info("library: %s kept in GitHub", lid)
            except Exception as e:  # noqa: BLE001 (the local copy stays; the next save tries again)
                log.warning("library: %s not pushed: %s", lid, e)
                self._remote = (0.0, self._remote[1], f"could not save to GitHub: {str(e)[:120]}")

    # ------------------------------------------------------------ search

    def search(self, q: str, limit: int = 20) -> dict:
        """Full-text search over every report and notebook this box holds (kept copies and live runs). Entries that
        live only in GitHub are matched on their title, question and summary. Each hit carries a short snippet."""
        words = [w for w in re.findall(r"\w{2,}", (q or "").lower())][:8]
        if not words:
            return {"q": q, "hits": []}
        rows = self.list()["entries"]
        roots = {}
        if self.hub.research.available():
            for r in rows:
                if r.get("live_mission"):
                    roots[r["lid"]] = os.path.join(self.hub.research.repo, "missions", r["live_mission"])
        hits = []
        for r in rows:
            texts = []
            d = os.path.join(self.dir, r["lid"])
            for name in ("report.md", "notebook.md"):
                texts.append(_read(os.path.join(d, name), 400000))
            if r["lid"] in roots and not any(texts):
                for name in ("REPORT.md", "NOTEBOOK.md"):
                    texts.append(_read(os.path.join(roots[r["lid"]], name), 400000))
            head = " ".join(str(r.get(k) or "") for k in ("title", "question", "one_line"))
            body = "\n".join(t for t in texts if t)
            low_head, low = head.lower(), body.lower()
            if not all(w in low_head or w in low for w in words):
                continue
            score = sum(5 * low_head.count(w) + min(low.count(w), 30) for w in words)
            i = min((low.find(w) for w in words if w in low), default=-1)
            snip = ""
            if i >= 0:
                a = max(0, body.rfind("\n", 0, i) + 1 if i - body.rfind("\n", 0, i) < 160 else i - 120)
                snip = " ".join(re.sub(r"[#*_>|`\[\]]", " ", body[a:a + 260]).split())
            hits.append({"lid": r["lid"], "live_mission": r.get("live_mission"), "title": r.get("title"), "snippet": snip,
                         "score": score, "in_github": r.get("in_github"), "started": r.get("started")})
        hits.sort(key=lambda h: -h["score"])
        return {"q": q, "hits": hits[:limit]}

    # ------------------------------------------------------------ read

    def html(self, lid: str) -> str:
        if not SAFE.match(lid or ""):
            raise ApiError(404, "no such report")
        text = _read(os.path.join(self.dir, lid, "report.html"), MAX_HTML)
        if text:
            return text
        if self._gh()[0]:
            try:
                return self._call("GET", f"library/{lid}/report.html", raw=True, timeout=60).decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                raise ApiError(404, f"the report is not in the library on GitHub ({str(e)[:80]})") from None
        raise ApiError(404, "no such report on this box (connect GitHub to see reports from earlier boxes)")


def add_routes(router, hub_of, body_of) -> None:
    import asyncio
    from aiohttp import web

    def lib(request) -> Library:
        return hub_of(request).library

    async def get_list(request):
        return web.json_response(await asyncio.to_thread(lib(request).list))

    async def post_save(request):
        return web.json_response(await asyncio.to_thread(lib(request).save, await body_of(request)))

    async def get_search(request):
        return web.json_response(await asyncio.to_thread(lib(request).search, request.query.get("q", "")))

    async def get_html(request):
        text = await asyncio.to_thread(lib(request).html, request.match_info["lid"])
        return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-store"})

    router.add_get("/api/library", get_list)
    router.add_get("/api/library/search", get_search)
    router.add_post("/api/library/save", post_save)
    router.add_get("/api/library/{lid}/html", get_html)
