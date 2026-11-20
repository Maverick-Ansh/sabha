"""Research view API (PROTOCOL.md section 8).

The research daemon (`python -m sabha.research.run`) is a separate process that owns a data
repo: <repo>/missions/<id>/{plan.json, cards/, tracks/, memory/, state/}. The hub only reads
those files. The one thing it writes is a line in state/inbox.jsonl, the daemon's command
inbox (research/inbox.py); when no daemon is working a mission it may also write the brief of
a new mission, which is the file a human would otherwise write by hand.

"Agents" in the Research view are the mission's tracks (t1..t6). What an agent is doing right
now comes from state/live.json, which the daemon rewrites every few seconds (research/live.py).
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import re
import threading
import time

import aiohttp
from aiohttp import web

from .models import auth_headers
from .research import inbox as rinbox
from .research.memory import latest, read_json, read_jsonl
from .util import ApiError

log = logging.getLogger("sabha.research")

HUES = ("sky", "lilac", "mint", "sun", "rose", "coral")
STATES = ("carded", "queued", "reading", "rejected", "failed")
ALIVE_S = 90            # live.json is rewritten at least every 10 s; older than this means the daemon is not running
SUMMARY_TTL_S = 300
DOC_MAX = 200 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,80}$")
DOCS = {  # kind -> path parts; "{t}" is the track id
    "status": ("STATUS.md",), "digest": ("DIGEST.md",), "synthesis": ("synthesis.md",), "brief": ("brief.md",),
    "living": ("REPORT.md",), "knowledge": ("knowledge.md",), "notebook": ("NOTEBOOK.md",), "final": ("final.md",),
    "report": ("tracks", "{t}", "report.md"), "track_digest": ("tracks", "{t}", "digest.md"),
    "assignment": ("tracks", "{t}", "assignment.md"),
}
SUMMARY_SYSTEM = (
    "You report on the work of one research agent to its owner, who reads this on a phone. "
    "Write 4 to 6 plain sentences in one paragraph. No lists, no headings, no markdown. "
    "Say what it has read so far, the most important things it found (name papers by a short title), "
    "what it is doing right now, and anything that went wrong or is stuck. "
    "Use only the facts given below. Do not invent numbers, papers or findings.")


def _epoch(iso) -> float:
    try:
        return float(calendar.timegm(time.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def _read_text(path: str, limit: int = DOC_MAX) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _load_registry(path: str) -> dict:
    reg = latest(path)
    counts: dict[str, dict] = {}
    total = {s: 0 for s in STATES}
    for r in reg.values():
        s = r.get("state")
        if s in total:
            total[s] += 1
            counts.setdefault(r.get("track") or "", {k: 0 for k in STATES})[s] += 1
    return {"reg": reg, "counts": counts, "total": total}


def _published(c: dict) -> str:
    from .research.notebook import published
    return published(c)


def _load_card(path: str) -> dict:
    c = read_json(path, {}) or {}
    q = c.get("quality") or {}
    repo = c.get("official_repo") or {}
    return {"id": c.get("id", ""), "title": c.get("title", ""), "one_line": c.get("one_line", ""),
            "grade": q.get("grade", ""), "quote_pass": q.get("quote_pass"), "causal": c.get("causal_evidence", ""),
            "repo": repo.get("url") if repo.get("status") == "verified" else None,
            "tracks": c.get("tracks", []), "t": _epoch(c.get("ts")), "published": _published(c)}


def _slot_text(s: dict) -> str:
    """One plain line for a live slot (used in summaries; the UI formats its own)."""
    step = s.get("step") or "idle"
    if s.get("kind") == "worker":
        part = f" part {s.get('i', 0)} of {s.get('n', 0)}" if step == "map" else ""
        return f"reading \"{s.get('title') or s.get('paper')}\" (step {step}{part})"
    if step == "scouting":
        return "searching for papers" + (f" with the query \"{s['query']}\"" if s.get("query") else " (planning queries)")
    return {"consolidating": "folding new cards into the claims ledger", "report": "rewriting the report",
            "synthesis": "writing the synthesis", "seeds": "resolving seed papers",
            "snowballing": "sorting papers cited by the cards"}.get(step, step)


def _event_text(e: dict, title_of) -> str:
    k = e.get("kind")
    if k == "carded":
        return f"Wrote the card for \"{title_of(e.get('id'))}\" ({e.get('grade')}, quote check {e.get('quote_pass')}, {e.get('secs')} s)"
    if k == "scouted":
        return f"Searched: {e.get('new', 0)} new papers seen, {e.get('rel2', 0)} worth reading"
    if k == "consolidated":
        return f"Folded new cards into the ledger ({e.get('ops', 0)} changes)"
    if k == "report":
        return f"Rewrote the report from {e.get('cards', 0)} cards"
    if k == "saturated":
        return "Searches stopped finding useful new papers (saturated)"
    if k == "query_refused":
        return f"Skipped a repeated or banned search: \"{e.get('text', '')}\""
    if k in ("scout_fail", "consolidate_fail", "search_fail"):
        return f"{k.replace('_', ' ')}: {e.get('err', '')}"
    if k == "command":
        return f"Command {e.get('cmd')}" + (f" \"{e.get('text')}\"" if e.get("text") else "") + f": {e.get('result', '')}"
    if k == "seeds":
        return f"Seed papers resolved: {e.get('resolved', 0)} of {e.get('total', 0)}"
    if k == "planned":
        return f"Mission planned: {e.get('tracks', 0)} agents"
    if k == "synthesis":
        return "Wrote the " + ("final" if e.get("final") else "draft") + " synthesis"
    if k == "steering":
        return f"Validator steering {e.get('seq')} applied: " + "; ".join(str(x) for x in (e.get("items") or [])[:3])
    if k == "error":
        return f"Error: {e.get('msg', '')}"
    if k == "wave":
        return (f"Wave {e.get('n')} closed ({e.get('cards', 0)} papers): {e.get('headline', '')}"
                + ("" if e.get("final") else f". Context cleared; wave {e.get('next')} starts from the new state of knowledge"))
    if k == "replanned":
        return f"Re-planned for wave {e.get('wave')}: " + "; ".join(str(x) for x in (e.get("changes") or [])[:5])
    if k == "memory":
        return "Long-term memory: " + ("; ".join(str(x) for x in (e.get("ops") or [])[:4]) or "nothing new worth keeping")
    return str(k)


class Research:
    """Reads one research data repo for the hub. All file work is blocking: call through asyncio.to_thread."""

    def __init__(self, hub):
        self.hub = hub
        self.repo = str(getattr(hub.cfg, "research_repo", "") or "")
        self._cache: dict[str, tuple] = {}
        self._summaries: dict[tuple, dict] = {}
        self._sum_locks: dict[tuple, asyncio.Lock] = {}
        self._refining: dict[str, dict] = {}  # mission id -> {running, started, error, done}
        self._calls_ix: dict[str, dict] = {}  # calls.jsonl path -> incremental index (Conversations)

    # ------------------------------------------------------------ files

    def _cached(self, path: str, loader, default):
        """loader(path), remembered until the file's mtime or size changes."""
        try:
            st = os.stat(path)
        except OSError:
            return default
        key = (st.st_mtime_ns, st.st_size)
        hit = self._cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        val = loader(path)
        if len(self._cache) > 4000:
            self._cache.clear()
        self._cache[path] = (key, val)
        return val

    def available(self) -> bool:
        return bool(self.repo) and os.path.isdir(os.path.join(self.repo, "missions"))

    def _mroot(self, mid: str) -> str:
        return os.path.join(self.repo, "missions", mid)

    def missions(self) -> list[dict]:
        root = os.path.join(self.repo, "missions")
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return []
        out = []
        for name in names:
            mroot = os.path.join(root, name)
            if not SAFE_ID.match(name) or not os.path.isfile(os.path.join(mroot, "brief.md")):
                continue
            st = read_json(os.path.join(mroot, "state", "mission.json"), {}) or {}
            plan = self._cached(os.path.join(mroot, "plan.json"), lambda p: read_json(p, {}) or {}, {})
            head = self._cached(os.path.join(mroot, "brief.md"), lambda p: _read_text(p, 600), "")
            tm = re.search(r"^title:\s*(.+)$", head, re.M) or re.search(r"^#\s+(.+)$", head, re.M)
            out.append({"id": name, "status": st.get("status") or "waiting", "error": st.get("error") or "",
                        "started": _epoch(st.get("started")),
                        "title": (_read_text(os.path.join(mroot, "state", "report_title.txt"), 300).strip()  # the finished report's own title
                                  or (tm.group(1).strip() if tm else name))[:120],
                        "one_line": plan.get("one_line", ""), "tracks": len(plan.get("tracks") or []),
                        "mode": "preempt" if "mode: preempt" in head[:400] else "queue"})
        return out

    def _active(self, ms: list[dict], wanted: str | None) -> str:
        ids = [m["id"] for m in ms]
        if wanted and wanted in ids:
            return wanted
        best, best_t = "", 0.0
        for mid in ids:  # the mission the daemon is writing live.json for right now
            ep = (read_json(os.path.join(self._mroot(mid), "state", "live.json"), {}) or {}).get("epoch") or 0
            if isinstance(ep, (int, float)) and ep > best_t:
                best, best_t = mid, float(ep)
        if best and time.time() - best_t < ALIVE_S:
            return best
        running = [m for m in ms if m["status"] == "running"]
        if running:
            return max(running, key=lambda m: m["started"])["id"]
        waiting = [m for m in ms if m["status"] != "done"]
        return (waiting[0] if waiting else ms[-1])["id"]

    # ------------------------------------------------------------ one mission, read once per request

    def _load(self, mission_id: str | None) -> dict | None:
        ms = self.missions()
        if not ms:
            return None
        mid = self._active(ms, mission_id)
        root = self._mroot(mid)
        now = time.time()
        plan = self._cached(os.path.join(root, "plan.json"), lambda p: read_json(p, {}) or {}, {})
        regd = self._cached(os.path.join(root, "memory", "registry.jsonl"), _load_registry,
                            {"reg": {}, "counts": {}, "total": {s: 0 for s in STATES}})
        events = self._cached(os.path.join(root, "state", "events.jsonl"), read_jsonl, [])
        live = read_json(os.path.join(root, "state", "live.json"), {})
        if not isinstance(live, dict):
            live = {}
        age = now - float(live.get("epoch") or 0) if isinstance(live.get("epoch"), (int, float)) else 1e9
        alive = age < ALIVE_S
        return {"missions": ms, "meta": next(m for m in ms if m["id"] == mid), "id": mid, "root": root, "now": now,
                "plan": plan, "regd": regd, "events": events, "live": live, "alive": alive, "age": age,
                "slots": (live.get("slots") or {}) if alive else {},
                "ltracks": live.get("tracks") or {},
                "weights": read_json(os.path.join(root, "state", "weights.json"), {}) or {},
                "D": rinbox.directives(root), "applied": rinbox.applied(root), "pending": rinbox.pending(root)}

    def _track_ids(self, L: dict) -> list[str]:
        return [t.get("id") for t in L["plan"].get("tracks") or [] if t.get("id")]

    def _title_cache(self, mid: str, aid) -> str:
        """A paper's title from a mission's registry (cached by file mtime), for the live feed."""
        regd = self._cached(os.path.join(self._mroot(mid), "memory", "registry.jsonl"), _load_registry,
                            {"reg": {}, "counts": {}, "total": {}})
        return (regd["reg"].get(aid) or {}).get("title") or ""

    def _title(self, L: dict, aid) -> str:
        return (L["regd"]["reg"].get(aid) or {}).get("title") or str(aid or "")

    def _row(self, L: dict, i: int, t: dict) -> dict:
        tid = t["id"]
        counts = L["regd"]["counts"].get(tid) or {s: 0 for s in STATES}
        try:
            weight = float(L["weights"].get(tid, t.get("weight", 1.0)))
        except (TypeError, ValueError):
            weight = 1.0
        lt = L["ltracks"].get(tid) or {}
        saturated = bool(lt.get("saturated"))
        paused = tid in L["D"]["paused"] or (weight <= 0 and not saturated)
        doing = []
        for name, s in sorted(L["slots"].items()):
            if isinstance(s, dict) and s.get("track") == tid and (s.get("step") or "idle") != "idle":
                doing.append({"slot": name, **{k: s.get(k) for k in ("kind", "step", "paper", "title", "i", "n", "query", "via", "since") if s.get(k) is not None}})
        last = None
        for e in reversed(L["events"]):
            if e.get("kind") == "carded" and e.get("track") == tid:
                last = {"id": e.get("id"), "title": self._title(L, e.get("id")), "t": _epoch(e.get("ts"))}
                break
        retired = t.get("retired")
        state = "working" if doing else "retired" if retired else "paused" if paused else "saturated" if saturated else "idle"
        return {"id": tid, "name": t.get("name", tid), "one_line": t.get("one_line", ""), "hue": HUES[i % len(HUES)],
                "weight": weight, "paused": paused, "saturated": saturated, "state": state, "counts": counts,
                "now": doing, "last_card": last, "retired": retired or 0, "since_wave": t.get("since_wave") or 1,
                "instructions": sum(1 for x in L["D"]["instructions"] if x.get("track") in (tid, "all")),
                "focus": sum(1 for x in L["D"]["focus"] if x.get("track") in (tid, "all")),
                "pending": sum(1 for c in L["pending"] if c.get("track") in (tid, "all"))}

    def _waves(self, root: str) -> dict:
        """Continuous runs (research/waves.py): the wave in progress and the closed ones, newest last."""
        w = read_json(os.path.join(root, "state", "waves.json"), {}) or {}
        if not isinstance(w, dict) or not w.get("n"):
            return {"on": False}
        hist = [{k: h.get(k) for k in ("n", "started", "ended", "cards", "headline", "memory")}
                for h in (w.get("history") or []) if isinstance(h, dict)]
        return {"on": True, "n": int(w.get("n") or 1), "started": w.get("started") or 0, "ends": w.get("ends") or 0,
                "wave_s": w.get("wave_s") or 0, "closed": bool(w.get("closed")), "history": hist[-24:],
                "plan_changes": (w.get("plan_changes") or [])[:12]}

    def _feed(self, L: dict, keep, n: int) -> list[dict]:
        out = []
        for e in reversed(L["events"]):
            if e.get("kind") == "digest" or not keep(e):
                continue
            out.append({"kind": e.get("kind"), "track": e.get("track"), "id": e.get("id") if e.get("kind") == "carded" else None,
                        "ok": e.get("ok", True), "t": _epoch(e.get("ts")), "text": _event_text(e, lambda a: self._title(L, a))[:400]})
            if len(out) >= n:
                break
        return out

    # ------------------------------------------------------------ GET /api/research

    def overview(self, mission_id: str | None = None) -> dict:
        if not self.available():
            return {"available": False, "repo": self.repo,
                    "hint": "Start the hub with --research-repo <path> (or env SABHA_RESEARCH_REPO) pointing at the research data repo."}
        L = self._load(mission_id)
        out: dict = {"available": True, "repo": self.repo, "now": time.time(), "missions": L["missions"] if L else [],
                     "mission": None, "agents": [], "team": [], "feed": [], "commands": {"pending": [], "applied": []},
                     "daemon": {"alive": False, "age_s": None}}
        if L is None:
            return out
        root, plan, total = L["root"], L["plan"], L["regd"]["total"]
        line = _read_text(os.path.join(root, "state", "status_line.txt"), 600).strip()
        try:
            line_age = L["now"] - os.stat(os.path.join(root, "state", "status_line.txt")).st_mtime
        except OSError:
            line_age = None
        left = re.search(r"\bleft (-?\d+)m", line)
        errs = re.search(r"\berr (\d+)", line)
        pph = sum(1 for e in L["events"] if e.get("kind") == "carded" and L["now"] - _epoch(e.get("ts")) < 3600)
        left_m = None
        if left and line_age is not None and L["alive"]:
            left_m = max(int(left.group(1)) - int(line_age // 60), 0)
        out["mission"] = {**L["meta"], "hours": plan.get("hours"), "status_line": line,
                          "status_age_s": None if line_age is None else int(line_age),
                          "stats": {"cards": total["carded"], "queued": total["queued"], "reading": total["reading"],
                                    "rejected": total["rejected"], "failed": total["failed"], "per_hour": pph,
                                    "left_m": left_m, "errors": int(errs.group(1)) if errs else 0},
                          "has_synthesis": os.path.isfile(os.path.join(root, "synthesis.md")),
                          "has_report": os.path.isfile(os.path.join(root, "REPORT.md")),
                          "has_knowledge": os.path.isfile(os.path.join(root, "knowledge.md")),
                          "has_notebook": os.path.isfile(os.path.join(root, "NOTEBOOK.md")) or os.path.isdir(os.path.join(root, "cards")),
                          "has_final": os.path.isfile(os.path.join(root, "final.md")),
                          "refining": {k: v for k, v in (self._refining.get(L["id"]) or {}).items() if k != "thread"} or None,
                          "waves": self._waves(root)}
        out["daemon"] = {"alive": L["alive"], "age_s": None if L["age"] > 1e8 else int(L["age"])}
        out["agents"] = [self._row(L, i, t) for i, t in enumerate(plan.get("tracks") or []) if t.get("id")]
        workers = [s for s in L["slots"].values() if isinstance(s, dict) and s.get("kind") == "worker"]
        out["workers"] = {"total": len(workers), "busy": sum(1 for s in workers if (s.get("step") or "idle") != "idle")}
        out["team"] = [{"slot": k, **{f: s.get(f) for f in ("kind", "step", "n", "query", "since") if s.get(f) is not None}}
                       for k, s in sorted(L["slots"].items())
                       if isinstance(s, dict) and not s.get("track") and s.get("kind") != "worker" and (s.get("step") or "idle") != "idle"]
        out["feed"] = self._feed(L, lambda e: True, 12)
        out["commands"] = {"pending": L["pending"][-10:], "applied": L["applied"]["items"][-8:][::-1]}
        out["standing"] = {"instructions": [x for x in L["D"]["instructions"] if x.get("track") == "all"],
                           "focus": [x for x in L["D"]["focus"] if x.get("track") == "all"]}
        return out

    # ------------------------------------------------------------ GET /api/research/timeline

    TL_MARKS = {"scouted": "search", "consolidated": "think", "report": "write", "saturated": "dry", "error": "error",
                "scout_fail": "error", "search_fail": "error", "command": "human", "replanned": "plan", "synthesis": "write",
                "memory": "think", "planned": "plan", "seeds": "search"}

    def timeline(self, mission_id: str | None = None, limit: int = 3000) -> dict:
        """The run's history for Watch's timeline: one lane per track, a span per paper read (a card's events carry
        how long it took), the paper being read now as an open span, marks for searches, reports, errors and
        commands, and the wave changes (PROTOCOL.md section 12)."""
        if not self.available():
            return {"mission": None}
        L = self._load(mission_id)
        if L is None:
            return {"mission": None}
        now = time.time()
        tracks = [{"id": t["id"], "name": t.get("name", t["id"]), "hue": HUES[i % len(HUES)], "retired": t.get("retired") or 0}
                  for i, t in enumerate(L["plan"].get("tracks") or []) if t.get("id")]
        spans, marks = [], []
        for e in L["events"][-limit:]:
            t = _epoch(e.get("ts"))
            if not t:
                continue
            k = e.get("kind")
            if k == "carded":
                secs = float(e.get("secs") or 0)
                spans.append({"track": e.get("track"), "t0": t - secs, "t1": t, "id": e.get("id"),
                              "title": self._title(L, e.get("id"))[:140], "grade": e.get("grade"), "open": False})
            elif k in self.TL_MARKS:
                marks.append({"track": e.get("track") or None, "t": t, "kind": self.TL_MARKS[k],
                              "text": _event_text(e, lambda a: self._title(L, a))[:200]})
        for name, sl in L["slots"].items():  # papers in hand right now
            if isinstance(sl, dict) and sl.get("kind") == "worker" and sl.get("paper") and sl.get("track"):
                spans.append({"track": sl["track"], "t0": float(sl.get("since") or now), "t1": now, "id": sl.get("paper"),
                              "title": (sl.get("title") or self._title(L, sl.get("paper")))[:140], "grade": None, "open": True,
                              "step": sl.get("step")})
        w = read_json(os.path.join(L["root"], "state", "waves.json"), {}) or {}
        waves = [{"n": int(h.get("n") or 0), "t": float(h.get("ended") or 0), "headline": str(h.get("headline") or "")[:200]}
                 for h in (w.get("history") or []) if isinstance(h, dict) and h.get("ended")]
        return {"mission": L["id"], "title": L["meta"].get("title"), "now": now, "running": bool(L["alive"]),
                "tracks": tracks, "spans": spans, "marks": marks, "waves": waves}

    # ------------------------------------------------------------ GET /api/research/agent/{tid}

    def agent(self, tid: str, mission_id: str | None = None) -> dict:
        L = self._load(mission_id) if self.available() else None
        if L is None:
            raise ApiError(404, "no research mission")
        tracks = [t for t in L["plan"].get("tracks") or [] if t.get("id")]
        idx = next((i for i, t in enumerate(tracks) if t["id"] == tid), -1)
        if idx < 0:
            raise ApiError(404, f"no agent {tid} in mission {L['id']}")
        t, root = tracks[idx], L["root"]
        out = self._row(L, idx, t)
        out["mission"] = L["id"]
        out["daemon_alive"] = L["alive"]
        out["deliverable"] = t.get("deliverable", "")
        out["questions"] = [{"id": q.get("id"), "text": q.get("text", "")} for q in t.get("questions") or [] if isinstance(q, dict)]
        out["events"] = self._feed(L, lambda e: e.get("track") == tid or (e.get("kind") == "command" and e.get("track") == "all"), 15)
        seen, cards = set(), []
        for e in reversed(L["events"]):
            if e.get("kind") == "carded" and e.get("track") == tid and e.get("id") not in seen:
                seen.add(e.get("id"))
                c = self._cached(os.path.join(root, "cards", f"{e.get('id')}.json"), _load_card, None)
                if c:
                    cards.append({**c, "t": _epoch(e.get("ts")) or c["t"], "secs": e.get("secs")})
                if len(cards) >= 8:
                    break
        out["cards"] = cards
        queued = sorted((r for r in L["regd"]["reg"].values() if r.get("state") == "queued" and r.get("track") == tid),
                        key=lambda r: -float(r.get("prio") or 0))
        out["queue"] = [{"id": r.get("id"), "title": r.get("title") or r.get("id"), "via": r.get("via"),
                         "published": _published({"id": r.get("id"), "first_submitted": r.get("created")})} for r in queued[:5]]
        out["digest"] = _read_text(os.path.join(root, "tracks", tid, "digest.md"), 6000)
        rep = _read_text(os.path.join(root, "tracks", tid, "report.md"), DOC_MAX)
        out["report_head"] = rep[:1800]
        out["report_chars"] = len(rep)
        out["standing"] = {"instructions": [x for x in L["D"]["instructions"] if x.get("track") in (tid, "all")],
                           "focus": [x for x in L["D"]["focus"] if x.get("track") in (tid, "all")]}
        board = latest(os.path.join(root, "memory", "board.jsonl"))
        opened = [b for b in board.values() if b.get("status") == "open" and b.get("to") in (tid, "all")]
        out["board"] = [{"id": b.get("id"), "from": b.get("from"), "q": b.get("q", "")} for b in opened[-8:][::-1]]
        out["board_open"] = len(opened)
        out["pending_commands"] = [c for c in L["pending"] if c.get("track") in (tid, "all")]
        s = self._summaries.get((L["id"], tid))
        out["summary"] = {k: s[k] for k in ("text", "t", "model", "fallback")} if s else None
        return out

    # ------------------------------------------------------------ documents

    # ------------------------------------------------------------ conversations: every model exchange (state/calls.jsonl)

    def _calls_index(self, path: str) -> dict:
        """Light rows (no full text) + each line's byte offset, read incrementally as the file grows."""
        ix = self._calls_ix.setdefault(path, {"size": 0, "rows": [], "off": {}})
        try:
            size = os.path.getsize(path)
        except OSError:
            return ix
        if size < ix["size"]:  # rewritten: start over
            ix.update(size=0, rows=[], off={})
        if size == ix["size"]:
            return ix
        with open(path, "rb") as f:
            f.seek(ix["size"])
            pos = ix["size"]
            for raw in f:
                if not raw.endswith(b"\n"):  # a line still being written: next time
                    break
                try:
                    r = json.loads(raw)
                except ValueError:
                    pos += len(raw)
                    continue
                n = int(r.get("n") or 0)
                ix["off"][n] = pos
                prev = lambda s, k=220: (s or "")[:k].replace("\n", " ")  # noqa: E731
                ix["rows"].append({"n": n, "t": r.get("t"), "track": r.get("track"), "paper": r.get("paper"),
                                   "title": r.get("title") or "", "step": r.get("step") or "", "asked": r.get("asked"),
                                   "role": r.get("role"), "endpoint": r.get("endpoint") or "", "remote": bool(r.get("remote")),
                                   "attempt": r.get("attempt") or 0, "json": bool(r.get("json")), "tok": r.get("tok") or 0,
                                   "prompt_tok": r.get("prompt_tok") or 0, "secs": r.get("secs") or 0,
                                   "error": r.get("error") or "", "looped": bool(r.get("looped")),
                                   "in_len": len(r.get("input") or ""), "out_len": len(r.get("output") or ""),
                                   "input": prev(r.get("input"), 160), "output": prev(r.get("output"))})
                pos += len(raw)
            ix["size"] = pos
        return ix

    def calls(self, mission_id: str | None = None, before: int = 0, limit: int = 150, track: str = "", step: str = "",
              endpoint: str = "", q: str = "", errors: bool = False) -> dict:
        L = self._load(mission_id) if self.available() else None
        if L is None:
            raise ApiError(404, "no research mission")
        ix = self._calls_index(os.path.join(L["root"], "state", "calls.jsonl"))
        rows = ix["rows"]
        ql = q.lower().strip()
        sel = [r for r in rows if (not track or r["track"] == track) and (not step or r["step"] == step)
               and (not endpoint or r["endpoint"] == endpoint) and (not errors or r["error"])
               and (not ql or ql in (r["input"] + r["output"] + r["title"] + r["step"]).lower())]
        if before:
            sel = [r for r in sel if r["n"] < before]
        page = sel[-max(1, min(limit, 500)):]
        by_ep: dict[str, dict] = {}
        for r in rows:
            e = by_ep.setdefault(r["endpoint"] or "?", {"endpoint": r["endpoint"] or "?", "remote": r["remote"], "calls": 0,
                                                        "errors": 0, "tok": 0, "secs": 0.0})
            e["calls"] += 1
            e["errors"] += bool(r["error"])
            e["tok"] += r["tok"]
            e["secs"] += r["secs"]
        return {"mission": L["id"], "total": len(rows), "matched": len(sel), "rows": page,
                "more": len(sel) > len(page), "endpoints": sorted(by_ep.values(), key=lambda x: -x["calls"]),
                "steps": sorted({r["step"] for r in rows if r["step"]}), "tracks": sorted({r["track"] for r in rows if r["track"]})}

    def call_full(self, n: int, mission_id: str | None = None) -> dict:
        L = self._load(mission_id) if self.available() else None
        if L is None:
            raise ApiError(404, "no research mission")
        path = os.path.join(L["root"], "state", "calls.jsonl")
        off = self._calls_index(path)["off"].get(int(n))
        if off is None:
            raise ApiError(404, f"no exchange {n}")
        with open(path, "rb") as f:
            f.seek(off)
            return json.loads(f.readline())

    def card_md(self, aid: str, mission_id: str | None = None) -> dict:
        L = self._load(mission_id) if self.available() else None
        if L is None or not SAFE_ID.match(aid or "") or ".." in aid:
            raise ApiError(404, "card not found")
        text = _read_text(os.path.join(L["root"], "cards", aid + ".md"))
        if not text:
            raise ApiError(404, "card not found")
        return {"id": aid, "title": self._title(L, aid), "md": text, "url": f"https://arxiv.org/abs/{aid}"}

    def doc(self, kind: str, tid: str = "", mission_id: str | None = None) -> dict:
        L = self._load(mission_id) if self.available() else None
        if L is None:
            raise ApiError(404, "no research mission")
        parts = DOCS.get(kind)
        if parts is None:
            raise ApiError(400, f"kind must be one of {', '.join(DOCS)}")
        if any("{t}" in p for p in parts) and tid not in self._track_ids(L):
            raise ApiError(404, f"no agent {tid!r}")
        path = os.path.join(L["root"], *[p.replace("{t}", tid) for p in parts])
        if kind == "notebook" and not os.path.exists(path):  # a run from before the notebook: build it from the event log
            from .research import notebook as NB
            from .research.memory import Mission
            try:
                NB.backfill(Mission(L["root"]))
            except Exception as e:  # the doc stays "not written yet"
                log.warning("notebook backfill %s: %s", L["id"], e)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            raise ApiError(404, f"{kind} is not written yet") from None
        return {"kind": kind, "track": tid, "mission": L["id"], "md": _read_text(path), "mtime": mtime}

    # ------------------------------------------------------------ GET /api/research/papers

    def papers(self, mission_id: str | None = None) -> dict:
        """Every paper card of a mission, compact: the report's citation chips and the Papers shelf read this."""
        L = self._load(mission_id) if self.available() else None
        if L is None:
            raise ApiError(404, "no research mission")
        from .research.notebook import best_result
        tracks = [t for t in L["plan"].get("tracks") or [] if t.get("id")]
        hue = {t["id"]: HUES[i % len(HUES)] for i, t in enumerate(tracks)}
        d = os.path.join(L["root"], "cards")
        out = []
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if not f.endswith(".json"):
                continue
            c = self._cached(os.path.join(d, f), lambda p: read_json(p, {}) or {}, {})
            if not c.get("id"):
                continue
            repo = c.get("official_repo") or {}
            res = [{"metric": str(r.get("metric") or "").replace("_", " "), "value": str(r.get("value") or ""),
                    "setting": str(r.get("setting") or "")[:80], "claim": str(r.get("claim") or "")[:240]}
                   for r in c.get("results") or [] if str(r.get("value") or "").strip()][:4]
            tr = c.get("tracks") or []
            out.append({"id": c["id"], "title": c.get("title", ""), "one_line": c.get("one_line", ""),
                        "why": c.get("why_it_matters", ""), "tracks": tr, "hue": hue.get(tr[0]) if tr else None,
                        "best": best_result(c), "results": res, "grade": (c.get("quality") or {}).get("grade", ""),
                        "relevance": c.get("relevance"), "submitted": str(c.get("first_submitted") or "")[:16],
                        "published": _published(c),
                        "model_type": c.get("model_type") or [], "wave": c.get("wave"),
                        "repo": repo.get("url") if repo.get("status") == "verified" else None,
                        "method": str((c.get("training_method") or {}).get("text") or "")[:300],
                        "limits": [str(x.get("text", ""))[:200] for x in (c.get("limitations") or [])[:2]],
                        "t": _epoch(c.get("ts"))})
        out.sort(key=lambda x: -x["t"])
        return {"mission": L["id"], "tracks": [{"id": t["id"], "name": t.get("name", t["id"]), "hue": hue[t["id"]]} for t in tracks],
                "papers": out}

    # ------------------------------------------------------------ POST /api/research/refine

    def refine(self, mission_id: str | None = None) -> dict:
        """Write the finished report of a run that has ended (a run from before the notebook, or one whose refine pass
        failed): notebook backfill, refine, REPORT.md. Runs in a thread on the box's models; the overview shows it."""
        L = self._load(mission_id) if self.available() else None
        if L is None:
            raise ApiError(404, "no research mission")
        if L["alive"] and L["meta"].get("status") == "running":
            raise ApiError(409, "the run is still going: its report is refined when it ends")
        cur = self._refining.get(L["id"]) or {}
        if cur.get("running"):
            return {k: v for k, v in cur.items() if k != "thread"}
        runner = getattr(self.hub, "runner", None)
        served = runner._served() if runner is not None and hasattr(runner, "_served") else set()
        if getattr(self.hub.cfg, "demo", False) or not ({"deep", "reader"} & served):
            raise ApiError(409, "no model is ready to write the report: wait until a model shows as ready")
        from .research import llm, notebook as NB, waves as W
        from .research.memory import Mission
        for role in ("deep", "reader", "s1"):
            if role not in served:
                llm.OVERRIDE[role] = "reader" if "reader" in served else "deep"
        for mdl in self.hub.store.models.values():
            role = (mdl.get("served_name") or "").lower()
            if role in llm.ROLES and mdl.get("context_window"):
                name, port, conc, _ = llm.ROLES[role]
                llm.ROLES[role] = (name, port, conc, int(mdl["context_window"]))
        st = {"running": True, "started": time.time(), "error": "", "done": 0.0}
        root, mid = L["root"], L["id"]

        def work():
            try:
                m = Mission(root)
                NB.backfill(m)
                NB.render(m)
                NB.refine(m)
                W.render_report(m, final=True)
                st["done"] = time.time()
            except Exception as e:
                log.exception("refine %s", mid)
                st["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            finally:
                st["running"] = False
        st["thread"] = threading.Thread(target=work, name=f"refine-{mid}", daemon=True)
        self._refining[mid] = st
        st["thread"].start()
        return {k: v for k, v in st.items() if k != "thread"}

    # ------------------------------------------------------------ POST /api/research/summary/{tid}

    def _summary_context(self, tid: str, mission_id: str | None) -> tuple[str, str, str]:
        a = self.agent(tid, mission_id)
        c = a["counts"]
        L = [f"AGENT {tid} \"{a['name']}\". Its job: {a['one_line']}",
             f"COUNTS: {c['carded']} paper cards written, {c['queued']} papers waiting to be read, {c['reading']} being read, "
             f"{c['rejected']} rejected as not useful, {c['failed']} failed. State: {a['state']}.",
             "RIGHT NOW: " + ("; ".join(_slot_text(s) for s in a["now"]) or
                              ("nothing; the research daemon is not running" if not a["daemon_alive"] else "nothing (idle)"))]
        if a["standing"]["instructions"]:
            L.append("STANDING INSTRUCTIONS FROM THE OWNER: " + " | ".join(x.get("text", "") for x in a["standing"]["instructions"]))
        if a["standing"]["focus"]:
            L.append("PRIORITY TOPICS: " + ", ".join(x.get("topic", "") for x in a["standing"]["focus"]))
        L.append("PAPER CARDS IT WROTE (newest first):")
        L += [f"- {x['title'][:110]}: {x['one_line'][:260]} (grade {x['grade']})" for x in a["cards"]] or ["- none yet"]
        if a["queue"]:
            L.append("NEXT IN ITS QUEUE: " + "; ".join(q["title"][:90] for q in a["queue"][:4]))
        L.append("RECENT EVENTS (newest first):")
        L += [f"- {time.strftime('%H:%M', time.gmtime(e['t']))} UTC {e['text'][:220]}" for e in a["events"]] or ["- none yet"]
        if a["board_open"]:
            L.append(f"OPEN QUESTIONS ON THE BOARD FOR IT: {a['board_open']}, for example: {a['board'][0]['q'][:200]}")
        newest = a["cards"][0] if a["cards"] else None
        fallback = (f"{a['name']} has written {c['carded']} paper cards so far, with {c['queued']} papers waiting, "
                    f"{c['rejected']} rejected and {c['failed']} failed. "
                    + (f"The newest card is \"{newest['title'][:100]}\": {newest['one_line'][:240]} " if newest else "It has no cards yet. ")
                    + "Right now it is " + ("; ".join(_slot_text(s) for s in a["now"]) or "idle") + ".")
        return a["mission"], "\n".join(L)[:9000], fallback

    def _reader(self) -> tuple[str, str, dict]:
        """Base URL, served name and headers of the summary model: a registered model called reader, else the box default."""
        for m in self.hub.store.models.values():
            if m.get("base_url") and "reader" in ((m.get("served_name") or "").lower(), (m.get("name") or "").lower()):
                return m["base_url"].rstrip("/"), m.get("served_name") or m["name"], auth_headers(m)
        return (os.environ.get("SABHA_READER_URL", "http://127.0.0.1:8002/v1").rstrip("/"),
                os.environ.get("SABHA_READER_NAME", "reader"), {})

    async def summary(self, tid: str, mission_id: str | None = None) -> dict:
        mid, user, fallback = await asyncio.to_thread(self._summary_context, tid, mission_id)
        key = (mid, tid)
        lock = self._sum_locks.setdefault(key, asyncio.Lock())
        async with lock:  # two taps on the button make one model call
            hit = self._summaries.get(key)
            if hit and time.time() - hit["t"] < SUMMARY_TTL_S and not hit.get("fallback"):
                return {**hit, "cached": True}
            base, name, headers = self._reader()
            body = {"model": name, "max_tokens": 340, "temperature": 0.3, "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                    "messages": [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}]}
            rec = {"tid": tid, "mission": mid, "t": time.time(), "model": name, "fallback": False, "error": ""}
            try:
                async with self.hub.http.post(base + "/chat/completions", json=body, headers=headers,
                                              timeout=aiohttp.ClientTimeout(total=150)) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:200]}")
                    d = await r.json(content_type=None)
                text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                if not text:
                    raise RuntimeError("the model returned no text")
                rec["text"] = text[:2400]
            except Exception as e:  # reader down or busy: still answer, from counts, and say so
                log.warning("summary for %s failed: %s", tid, e)
                rec.update(text=fallback, fallback=True, model="none",
                           error=f"The reader model did not answer ({type(e).__name__}: {str(e)[:160]}); this is built from the counts.")
            self._summaries[key] = rec
            return {**rec, "cached": False}

    # ------------------------------------------------------------ POST /api/research/command

    def command(self, body: dict) -> dict:
        if not self.available():
            raise ApiError(409, "no research repo is attached to this hub")
        L = self._load(None)  # commands always go to the mission the daemon is working on
        try:
            rec = rinbox.clean(body, self._track_ids(L) if L else [])
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        working = bool(L) and L["alive"] and L["meta"]["status"] == "running"
        if rec["cmd"] == "mission" and not working:
            # No daemon is polling an inbox, so write the brief here; the daemon looks for new briefs when it has no mission.
            mid = rinbox.new_mission(self.repo, rec["title"], rec["brief"], rec["mode"], rec["hours"])
            return {"ok": True, "applied_by": "hub", "mission": mid, "daemon_alive": bool(L and L["alive"]),
                    "note": f"Brief written as {mid}. The daemon picks it up when it is running and free."}
        if L is None:
            raise ApiError(409, "there is no mission yet: send a new mission first")
        rinbox.submit(L["root"], rec)
        note = ("Queued. The daemon applies it within about 15 seconds." if L["alive"]
                else "Queued, but the research daemon is not running: it will apply this when it starts.")
        return {"ok": True, "applied_by": "daemon", "command": {k: v for k, v in rec.items() if k != "brief"},
                "mission": L["id"], "daemon_alive": L["alive"], "note": note}


# ---------------------------------------------------------------- routes


def add_routes(router, hub_of, body_of) -> None:
    """Register the Research endpoints. hub_of(request) -> Hub, body_of(request) -> dict (both from server.py)."""

    def res(request) -> Research:
        return hub_of(request).research

    def mid(request) -> str | None:
        return request.query.get("mission") or None

    async def get_overview(request):
        out = await asyncio.to_thread(res(request).overview, mid(request))
        runner = getattr(hub_of(request), "runner", None)  # runs started from the app (research_run.py)
        if runner is not None:
            out["run"] = await asyncio.to_thread(runner.status)
        return web.json_response(out)

    async def get_agent(request):
        return web.json_response(await asyncio.to_thread(res(request).agent, request.match_info["tid"], mid(request)))

    async def get_card(request):
        return web.json_response(await asyncio.to_thread(res(request).card_md, request.match_info["aid"], mid(request)))

    async def get_doc(request):
        q = request.query
        return web.json_response(await asyncio.to_thread(res(request).doc, q.get("kind", ""), q.get("track", ""), mid(request)))

    async def get_papers(request):
        return web.json_response(await asyncio.to_thread(res(request).papers, mid(request)))

    async def post_refine(request):
        body = await body_of(request)
        return web.json_response(await asyncio.to_thread(res(request).refine, (body or {}).get("mission") or mid(request)))

    async def post_summary(request):
        return web.json_response(await res(request).summary(request.match_info["tid"], mid(request)))

    async def post_command(request):
        body = await body_of(request)
        return web.json_response(await asyncio.to_thread(res(request).command, body))

    router.add_get("/api/research", get_overview)

    async def get_timeline(request):
        return web.json_response(await asyncio.to_thread(res(request).timeline, mid(request)))
    router.add_get("/api/research/timeline", get_timeline)
    router.add_get("/api/research/agent/{tid}", get_agent)
    router.add_get("/api/research/card/{aid}", get_card)
    router.add_get("/api/research/doc", get_doc)
    router.add_get("/api/research/papers", get_papers)

    async def get_calls(request):
        q = request.query
        return web.json_response(await asyncio.to_thread(
            res(request).calls, mid(request), int(q.get("before") or 0), int(q.get("limit") or 150), q.get("track", ""),
            q.get("step", ""), q.get("endpoint", ""), q.get("q", ""), q.get("errors") == "1"))

    async def get_call(request):
        return web.json_response(await asyncio.to_thread(res(request).call_full, int(request.match_info["n"]), mid(request)))
    router.add_get("/api/research/calls", get_calls)
    router.add_get("/api/research/calls/{n}", get_call)
    router.add_post("/api/research/refine", post_refine)
    router.add_post("/api/research/summary/{tid}", post_summary)
    router.add_post("/api/research/command", post_command)
