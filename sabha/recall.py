"""The box's memory, for Talk: everything this box has done, searchable and readable (PROTOCOL.md section 10).

Documents (one id each):
    research:<mission>          a research run: its brief (question, taste) and status page
    synthesis:<mission>         the run's synthesis
    report:<mission>/<track>    a track's report          digest:<mission>/<track>  what the track believes
    card:<mission>/<arxiv id>   one paper card (facts with checked quotes, "why it matters to you")
    council:<id>                a council: the paper, every brief and turn, the verdict
    mission:<id>                a Work mission: the task, the final report, text files it wrote
    chat:<thread>               an earlier Talk conversation
    taste                       the owner's research taste
Research is read from the hub's research home plus any folder in SABHA_MEMORY_DIRS (os.pathsep separated)
that holds a missions/ folder, e.g. a clone of an older research repo.

Search is BM25 over words with a title boost, rebuilt at most every REBUILD_S seconds. Standard library
only; all of it is blocking, so the hub calls it through asyncio.to_thread.
"""
from __future__ import annotations

import glob
import math
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

REBUILD_S = 300.0  # also rebuilt at once whenever the signature changes
PART_CHARS = 6000
STOP = set("""a an the and or of to in on for with by is are was were be been being it its this that these those as at from
what which who whom how why when where do does did can could should would will shall may might must i you we they he she
my our your their me us them not no yes if then than so about into over under also just very more most some any all
there here has have had get got one two""".split())
WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")  # hyphens split words; arXiv ids like 2301.08243 stay whole
TEXT_FILES = (".md", ".txt", ".py", ".json", ".csv", ".tex", ".html", ".yaml", ".yml", ".sh", ".js", ".ts", ".toml", ".ipynb")
KINDS = ("research", "synthesis", "report", "digest", "card", "council", "mission", "chat", "taste")


def words(text: str) -> list[str]:
    return [w for w in WORD.findall((text or "").lower()) if len(w) > 1 and w not in STOP]


def _read(path: str, limit: int = 300_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


class Recall:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.docs: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.df: Counter = Counter()
        self.avg_len = 1.0
        self.built = 0.0

    # ------------------------------------------------------------ collect

    def research_roots(self) -> list[str]:
        roots: list[str] = []
        extra = [x for x in os.environ.get("SABHA_MEMORY_DIRS", "").split(os.pathsep) if x.strip()]
        for r in [getattr(self.hub.cfg, "research_repo", ""), *extra]:
            r = str(r or "").strip()
            if r and os.path.isdir(os.path.join(r, "missions")) and r not in roots:
                roots.append(r)
        return roots

    def _collect(self) -> list[dict]:
        docs: list[dict] = []
        store = self.hub.store

        def add(did: str, kind: str, title: str, text: str, ts: float = 0.0) -> None:
            text = (text or "").strip()
            if text:
                docs.append({"id": did, "kind": kind, "title": " ".join(title.split())[:160], "text": text, "ts": ts or 0.0})

        def name_of(agent_id) -> str:
            return (store.agents.get(agent_id) or {}).get("name") or "an agent"

        for root in self.research_roots():
            for mdir in sorted(glob.glob(os.path.join(root, "missions", "*"))):
                mid = os.path.basename(mdir)
                brief_p = os.path.join(mdir, "brief.md")
                if not os.path.isfile(brief_p):
                    continue
                brief = _read(brief_p)
                m = re.search(r"^title:\s*(.+)$", brief, re.M) or re.search(r"^#\s+(.+)$", brief, re.M)
                mt = m.group(1).strip() if m else mid
                add(f"research:{mid}", "research", f"Research run: {mt}",
                    brief + "\n\n" + _read(os.path.join(mdir, "STATUS.md"), 5000), _mtime(brief_p))
                syn = os.path.join(mdir, "synthesis.md")
                add(f"synthesis:{mid}", "synthesis", f"Synthesis of the research run '{mt}'", _read(syn), _mtime(syn))
                for kind, name in (("report", "report.md"), ("digest", "digest.md")):
                    for p in sorted(glob.glob(os.path.join(mdir, "tracks", "*", name))):
                        tid = os.path.basename(os.path.dirname(p))
                        add(f"{kind}:{mid}/{tid}", kind, f"Track {tid} {kind} of '{mt}'", _read(p), _mtime(p))
                for p in sorted(glob.glob(os.path.join(mdir, "cards", "*.md"))):
                    text = _read(p, 40_000)
                    head = re.search(r"^#\s+(.+)$", text, re.M)
                    aid = os.path.basename(p)[:-3]
                    add(f"card:{mid}/{aid}", "card", f"Paper card {aid}: {head.group(1).strip() if head else aid}", text, _mtime(p))

        for c in list(store.councils.values()):
            paper = c.get("paper") or {}
            parts = [f"Paper: {paper.get('title') or c.get('title')} {paper.get('url') or c.get('source') or ''}"]
            for msg in c.get("messages") or []:
                who = name_of(msg.get("agent_id")) if msg.get("agent_id") else ("The owner" if msg.get("role") == "user" else "Sabha")
                if msg.get("content"):
                    parts.append(f"[{msg.get('kind') or msg.get('role')}] {who}: {msg['content']}")
            if c.get("verdict"):
                parts.append("VERDICT: " + c["verdict"])
            add(f"council:{c['id']}", "council", f"Council on '{c.get('title')}'", "\n\n".join(parts), c.get("created_ts") or 0)

        data = Path(self.hub.cfg.data_dir)
        for ms in list(store.missions.values()):
            text = (f"Task given to {name_of(ms.get('agent_id'))}: {ms.get('prompt') or ''}\nStatus: {ms.get('status')}\n\n"
                    + (ms.get("result_text") or ms.get("error") or ""))
            ws = data / ms["workspace"] if ms.get("workspace") else None
            if ws and ws.is_dir():
                n = 0
                for f in sorted(ws.rglob("*")):
                    if n >= 20 or not f.is_file() or f.suffix.lower() not in TEXT_FILES or f.stat().st_size > 400_000:
                        continue
                    text += f"\n\n=== file {f.relative_to(ws).as_posix()}\n" + _read(str(f), 20_000)
                    n += 1
            add(f"mission:{ms['id']}", "mission", f"Work mission: {ms.get('title')}", text, ms.get("created_ts") or 0)

        for t in list(store.threads.values()):
            who = name_of(t.get("agent_id"))
            lines = [f"{'The owner' if m['role'] == 'user' else who if m['role'] == 'assistant' else 'Sabha'}: {m['content']}"
                     for m in t.get("messages") or [] if m.get("content")]
            add(f"chat:{t['id']}", "chat", f"Chat with {who}: {t.get('title')}", "\n\n".join(lines), t.get("updated_ts") or 0)

        taste = self.hub.taste.load() if getattr(self.hub, "taste", None) else {}
        if taste.get("taste") or taste.get("loves") or taste.get("topic"):
            add("taste", "taste", "The owner's research taste",
                f"Research taste: {taste.get('taste') or ''}\nPapers they love: {', '.join(taste.get('loves') or [])}\n"
                f"Question of the last research run: {taste.get('topic') or ''}", taste.get("updated") or 0)
        return docs

    def _build(self) -> None:
        docs = self._collect()
        df: Counter = Counter()
        total = 0
        for d in docs:
            toks = words(d["text"])
            d["tf"] = Counter(toks)
            d["tt"] = set(words(d["title"]))
            d["len"] = len(toks) or 1
            total += d["len"]
            df.update(d["tf"].keys())
        self.docs, self.df = docs, df
        self.by_id = {d["id"]: d for d in docs}
        self.avg_len = total / max(len(docs), 1)
        self.built = time.time()

    def _signature(self) -> tuple:
        """Cheap: changes whenever something new lands (store counts, research folder mtimes, the taste file)."""
        st = self.hub.store
        sig: list = [len(st.councils), len(st.missions), len(st.threads),
                     sum(len(t.get("messages") or []) for t in st.threads.values()),
                     sum(len(c.get("messages") or []) for c in st.councils.values()),
                     sum(1 for m in st.missions.values() if m.get("result_text"))]
        for root in self.research_roots():
            for pat in ("*", "*/cards", "*/tracks/*"):
                sig += [_mtime(d) for d in glob.glob(os.path.join(root, "missions", pat))]
        taste = getattr(self.hub, "taste", None)
        sig.append(_mtime(str(taste.path)) if taste is not None else 0)
        return tuple(sig)

    def fresh(self) -> None:
        with self.lock:
            sig = self._signature()
            if sig != getattr(self, "sig", None) or time.time() - self.built > 300:
                self._build()
                self.sig = sig

    # ------------------------------------------------------------ use

    def search(self, query: str, kind: str = "", k: int = 8, exclude: str = "") -> list[dict]:
        self.fresh()
        q = list(dict.fromkeys(words(query)))
        if not q:
            return []
        kinds = {x.strip() for x in (kind or "").split(",") if x.strip() in KINDS}
        n = len(self.docs) or 1
        scored = []
        for d in self.docs:
            if (kinds and d["kind"] not in kinds) or d["id"] == exclude:
                continue
            s = 0.0
            for w in q:
                f = d["tf"].get(w, 0)
                if f:
                    idf = math.log(1 + (n - self.df[w] + 0.5) / (self.df[w] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * d["len"] / self.avg_len))
                if w in d["tt"]:
                    s += 1.5
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: (-x[0], -x[1]["ts"]))
        return [{"id": d["id"], "kind": d["kind"], "title": d["title"], "score": round(s, 2),
                 "snippet": self._snippet(d["text"], q), "chars": len(d["text"])} for s, d in scored[:max(1, min(k, 20))]]

    def _snippet(self, text: str, q: list[str], size: int = 360) -> str:
        low = text.lower()
        rare = sorted(q, key=lambda w: self.df.get(w, 0))
        pos = next((low.find(w) for w in rare if low.find(w) >= 0), 0)
        start = max(0, pos - size // 3)
        cut = " ".join(text[start:start + size].split())
        return ("…" if start else "") + cut + ("…" if start + size < len(text) else "")

    def read(self, did: str, part: int = 1) -> dict | None:
        self.fresh()
        d = self.by_id.get((did or "").strip())
        if d is None:
            return None
        parts = max(1, math.ceil(len(d["text"]) / PART_CHARS))
        part = min(max(int(part or 1), 1), parts)
        return {"id": d["id"], "kind": d["kind"], "title": d["title"], "part": part, "parts": parts,
                "text": d["text"][(part - 1) * PART_CHARS: part * PART_CHARS]}

    def overview(self, exclude: str = "", limit: int = 1800) -> str:
        """A short catalogue for the Talk system prompt: what exists, newest first."""
        self.fresh()
        L: list[str] = []
        runs = [d for d in self.docs if d["kind"] == "research"]
        cards = Counter(d["id"].split(":", 1)[1].split("/", 1)[0] for d in self.docs if d["kind"] == "card")
        syn = {d["id"].split(":", 1)[1] for d in self.docs if d["kind"] == "synthesis"}
        for d in sorted(runs, key=lambda d: -d["ts"])[:8]:
            mid = d["id"].split(":", 1)[1]
            L.append(f"- {d['title']} [{d['id']}]: {cards.get(mid, 0)} paper cards" + (", has a synthesis" if mid in syn else ""))
        for kind, label in (("council", "Councils"), ("mission", "Work missions"), ("chat", "Earlier chats")):
            items = [d for d in self.docs if d["kind"] == kind and d["id"] != exclude]
            if items:
                L.append(f"{label} ({len(items)}): " + "; ".join(f"{d['title']} [{d['id']}]" for d in sorted(items, key=lambda d: -d["ts"])[:6]))
        if "taste" in self.by_id:
            L.append("The owner's research taste [taste]")
        out = "\n".join(L) or "(nothing yet: no research runs, councils, missions or earlier chats on this box)"
        return out if len(out) <= limit else out[:limit].rsplit("\n", 1)[0] + "\n- …"
