"""Shared memory of a mission, all plain files in the data repo (docs/RESEARCH_PLAN.md section 3).

working    scratch/<id>/            one paper's text and notes, deleted after the card is written
episodic   cards/<id>.json (+ .md)  one card per paper, written once, read by every agent
semantic   tracks/<t>/claims.jsonl  what a track believes (+ digest.md rendered from it)
procedural tracks/<t>/queries.jsonl which searches paid off (+ memory/playbook.md)
board      memory/board.jsonl       questions between tracks (and to the validator)
registry   memory/registry.jsonl    every paper ever seen: global dedupe and the reading queue

Append-only JSONL, last line per id wins. Each append opens the file, writes one
line and closes it (a git pull can truncate a handle kept open). Only the box
writes these; only humans write brief.md and only the validator writes steering.md.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

_LOCK = threading.RLock()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def base_id(aid: str) -> str:
    return re.sub(r"v\d+$", "", (aid or "").strip())


def append(path: str, rec: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {**rec, "ts": rec.get("ts") or now()}
    with _LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_jsonl(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def latest(path: str, key: str = "id") -> dict[str, dict]:
    d: dict[str, dict] = {}
    for r in read_jsonl(path):
        if key in r:
            d[r[key]] = {**d.get(r[key], {}), **r}
    return d


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path: str, obj) -> None:
    write(path, json.dumps(obj, indent=1, ensure_ascii=False))


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Mission:
    """Paths and in-memory views over one mission directory."""

    def __init__(self, root: str):
        self.root = root
        self.id = os.path.basename(root.rstrip("/"))
        for d in ("cards", "deep", "rejected", "memory", "tracks", "state"):
            os.makedirs(self.p(d), exist_ok=True)
        self.lock = threading.RLock()
        self.reg = latest(self.p("memory/registry.jsonl"))

    def p(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    @property
    def plan(self) -> dict:
        return read_json(self.p("plan.json"), {}) or {}

    @property
    def taste(self) -> str:
        """The owner's research taste (taste.md, written when the run starts from the app), cut to prompt size. "" = none."""
        try:
            return open(self.p("taste.md"), encoding="utf-8").read().strip()[:1500]
        except OSError:
            return ""

    def track_ids(self) -> list[str]:
        return [t["id"] for t in self.plan.get("tracks", [])]

    def track(self, tid: str) -> dict:
        return next((t for t in self.plan.get("tracks", []) if t["id"] == tid), {})

    # ---- registry (dedupe + queue)
    def seen(self, aid: str) -> bool:
        return base_id(aid) in self.reg

    def mark(self, aid: str, **fields) -> dict:
        aid = base_id(aid)
        with self.lock:
            rec = {**self.reg.get(aid, {}), **fields, "id": aid, "ts": now()}
            self.reg[aid] = rec
            append(self.p("memory/registry.jsonl"), {"id": aid, **fields})
        return rec

    def queued(self, track: str | None = None) -> list[dict]:
        q = [r for r in self.reg.values() if r.get("state") == "queued" and (track is None or r.get("track") == track)]
        return sorted(q, key=lambda r: -float(r.get("prio", 0)))

    def count(self, state: str, track: str | None = None) -> int:
        return sum(1 for r in self.reg.values() if r.get("state") == state and (track is None or r.get("track") == track))

    def requeue_stale(self, minutes: float = 30) -> int:
        n, cut = 0, time.time() - minutes * 60
        for r in list(self.reg.values()):
            if r.get("state") == "reading":
                try:
                    t = time.mktime(time.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                except (KeyError, ValueError):
                    t = 0
                if t < cut:
                    self.mark(r["id"], state="queued", why=(r.get("why") or "") + " | requeued")
                    n += 1
        return n

    # ---- episodic
    def card(self, aid: str) -> dict | None:
        return read_json(self.p("cards", base_id(aid) + ".json"))

    def cards(self) -> list[dict]:
        out = []
        for f in sorted(os.listdir(self.p("cards"))):
            if f.endswith(".json"):
                c = read_json(self.p("cards", f))
                if c:
                    out.append(c)
        return out

    # ---- semantic
    def claims(self, tid: str) -> dict[str, dict]:
        return latest(self.p("tracks", tid, "claims.jsonl"))

    def next_claim_id(self, tid: str) -> str:
        n = len(self.claims(tid)) + 1
        return f"{tid}.c{n}"

    # ---- procedural
    def queries(self, tid: str) -> list[dict]:
        return read_jsonl(self.p("tracks", tid, "queries.jsonl"))

    # ---- board
    def board(self) -> dict[str, dict]:
        return latest(self.p("memory/board.jsonl"))

    def post(self, frm: str, to: str, q: str) -> str:
        with self.lock:
            bid = f"b{len(self.board()) + 1}"
            append(self.p("memory/board.jsonl"), {"id": bid, "from": frm, "to": to, "q": q[:400], "status": "open"})
        return bid

    def log(self, kind: str, **fields) -> None:
        append(self.p("state/events.jsonl"), {"kind": kind, **fields})


MEMORY_LESSON = """MEMORY LESSON (read this first, every time)
You are one research agent in a team. You have four memories:
- WORKING memory is this prompt. It is erased the moment you answer.
- EPISODIC memory is one card per paper, written once and read by every agent.
- SEMANTIC memory is your track's claims ledger and digest: what the team believes so far, with evidence.
- PROCEDURAL memory is the playbook: which searches and reading habits worked.
You remember nothing from earlier papers except the MEMORY PACKET below.
Rules:
1. Only your JSON answer survives. Write it for a stranger who has not read the paper.
2. Every fact carries a section name and a quote of at most 25 words, copied exactly from the text you were given.
3. If the packet already holds a claim, cite its id ("supports t1.c12", "contradicts t1.c12"). Do not restate it.
4. Unknown is "not_stated". Never fill a field from what you remember about a famous paper.
5. A question you cannot answer goes to the board, addressed to a track.
6. Predicting future observations is not evidence of causal understanding. Say "prediction only" unless the paper intervenes or tests counterfactuals.
"""


def packet(m: Mission, tid: str, near_claims: list[dict] | None = None, steering_lines: list[str] | None = None,
           max_chars: int = 7000) -> str:
    """The MEMORY PACKET: everything an agent knows when it starts a step."""
    t = m.track(tid)
    parts = [f"MISSION: {m.plan.get('one_line', '')}",
             f"YOUR TRACK {tid}: {t.get('name', '')}. {t.get('one_line', '')}",
             "TRACK QUESTIONS:\n" + "\n".join(f"- {q['id']}: {q['text']}" for q in t.get("questions", []))]
    if m.taste:  # early in the packet, so the cut at max_chars never drops it
        parts.append("THE OWNER'S RESEARCH TASTE (choose, judge and write by it):\n" + m.taste)
    # standing instructions sent from the phone (research/inbox.py); early in the packet, so the cut at max_chars never drops them
    dj = read_json(m.p("state", "directives.json"), {})
    human = [x.get("text", "") for x in ((dj.get("instructions") or []) if isinstance(dj, dict) else [])
             if isinstance(x, dict) and x.get("track") in (tid, "all") and x.get("text")][-8:]
    if human:
        parts.append("STANDING INSTRUCTIONS FROM THE HUMAN (they apply to everything you do in this step):\n" + "\n".join(f"- {s}" for s in human))
    dig = ""
    try:
        dig = open(m.p("tracks", tid, "digest.md"), encoding="utf-8").read()[:2500]
    except OSError:
        pass
    if dig:
        parts.append("TRACK DIGEST (semantic memory):\n" + dig)
    if near_claims:
        parts.append("NEAREST LEDGER CLAIMS:\n" + "\n".join(
            f"- {c['id']} [{c.get('status')}] {c.get('text', '')[:220]}" for c in near_claims[:8]))
    opened = [b for b in m.board().values() if b.get("status") == "open" and b.get("to") in (tid, "all")][:5]
    if opened:
        parts.append("OPEN BOARD QUESTIONS FOR YOU:\n" + "\n".join(f"- {b['id']} from {b['from']}: {b['q']}" for b in opened))
    try:
        parts.append("PLAYBOOK (procedural memory):\n" + open(m.p("memory/playbook.md"), encoding="utf-8").read()[:1500])
    except OSError:
        pass
    if steering_lines:
        parts.append("VALIDATOR STEERING:\n" + "\n".join(f"- {s}" for s in steering_lines[:8]))
    out = "\n\n".join(parts)
    return out[:max_chars]
