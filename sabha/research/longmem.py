"""Long-term memory: what the agents keep after a run ends. One markdown file per memory, an index, a gardener.

    <home>/MEMORY.md     the index, one line per memory (what every planner reads first)
    <home>/<slug>.md     one memory: front matter (name, description, type, created, updated, source, uses) + body

<home> is <data>/memory next to the research home (env SABHA_LONGMEM overrides). The research daemon
reads it when a run starts and at every wave, and the gardener (agents.garden_memory) rewrites it at
every wave turnover: add, update or delete, a few at a time. The hub lists, reads and deletes memories
for the app (GET/DELETE /api/longmem) and Talk searches them through sabha/recall.py.

Types (the same four kinds a person keeps about a collaborator):
    owner   what the owner wants, likes and distrusts: from their taste, their commands and their feedback
    lesson  how to work: searches, sources and reading habits that worked or failed (procedural)
    fact    a finding from a run, with the arXiv ids that back it (semantic; re-check before relying on it)
    source  a pointer worth keeping: a key paper, a repo, a benchmark, a dataset, a lab

Standard library only. Every write is atomic (tmp file + rename); reads never raise.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import Counter

TYPES = ("owner", "lesson", "fact", "source")
MAX_MEMORIES = 160         # the gardener prunes the least used, oldest non-owner memories above this
MAX_BODY = 1200
MAX_DESC = 160
INDEX_NAME = "MEMORY.md"
_LOCK = threading.RLock()
_WORD = re.compile(r"[a-z0-9][a-z0-9\-]{1,}")
_STOP = set("the a an and or of to in on for with by from is are was were be been this that these those it its as at "
            "not no but if then than so such into over under about which what when where who how why can could should "
            "would will may might do does did done has have had their there they them we our you your".split())


def home_for(repo: str) -> str:
    """Where long-term memory lives for a research home: env SABHA_LONGMEM, else <repo>/../memory."""
    return os.environ.get("SABHA_LONGMEM") or os.path.join(os.path.dirname(os.path.abspath(repo)), "memory")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (s[:60].rstrip("-") or "memory")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _one_line(s, limit: int) -> str:
    return " ".join(str(s or "").split())[:limit]


def parse(text: str) -> dict:
    """A memory file -> {name, description, type, created, updated, source, uses, body}. Missing front matter is fine."""
    meta, body = {}, text
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if m:
        for line in m.group(1).split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
        body = text[m.end():]
    try:
        uses = int(meta.get("uses") or 0)
    except ValueError:
        uses = 0
    t = meta.get("type", "fact")
    return {"name": meta.get("name", ""), "description": meta.get("description", ""), "type": t if t in TYPES else "fact",
            "created": meta.get("created", ""), "updated": meta.get("updated", ""), "source": meta.get("source", ""),
            "uses": uses, "body": body.strip()}


def render(mem: dict) -> str:
    desc = _one_line(mem.get("description"), MAX_DESC).replace('"', "'")
    return (f"---\nname: {slug(mem['name'])}\ndescription: \"{desc}\"\ntype: {mem.get('type', 'fact')}\n"
            f"created: {mem.get('created') or _now()}\nupdated: {mem.get('updated') or _now()}\n"
            f"source: {_one_line(mem.get('source'), 120)}\nuses: {int(mem.get('uses') or 0)}\n---\n\n"
            f"{str(mem.get('body') or '').strip()[:MAX_BODY]}\n")


def load_all(home: str) -> list[dict]:
    out = []
    try:
        names = sorted(os.listdir(home))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".md") or n == INDEX_NAME:
            continue
        try:
            with open(os.path.join(home, n), encoding="utf-8") as f:
                mem = parse(f.read())
        except (OSError, UnicodeDecodeError):
            continue
        mem["name"] = mem["name"] or n[:-3]
        mem["file"] = n
        out.append(mem)
    return out


def get(home: str, name: str) -> dict | None:
    p = os.path.join(home, slug(name) + ".md")
    try:
        with open(p, encoding="utf-8") as f:
            mem = parse(f.read())
    except OSError:
        return None
    mem["name"] = mem["name"] or slug(name)
    mem["file"] = slug(name) + ".md"
    return mem


def write_index(home: str, mems: list[dict] | None = None) -> str:
    mems = load_all(home) if mems is None else mems
    order = {t: i for i, t in enumerate(TYPES)}
    lines = ["# Long-term memory", "",
             "One line per memory: - [name](file) (type) description. Written by the research agents' gardener.", ""]
    for mem in sorted(mems, key=lambda x: (order.get(x["type"], 9), x["name"])):
        lines.append(f"- [{mem['name']}]({mem['file']}) ({mem['type']}) {mem['description']}")
    text = "\n".join(lines) + "\n"
    _write(os.path.join(home, INDEX_NAME), text)
    return text


def index_text(home: str, max_chars: int = 3000, types: tuple[str, ...] = TYPES) -> str:
    """The index as a prompt block: owner memories first (they never get cut), then lessons, facts, sources."""
    mems = [x for x in load_all(home) if x["type"] in types]
    order = {t: i for i, t in enumerate(TYPES)}
    mems.sort(key=lambda x: (order.get(x["type"], 9), -x["uses"], x["name"]))
    out, n = [], 0
    for mem in mems:
        line = f"- {mem['name']} ({mem['type']}): {mem['description']}"
        if n + len(line) + 1 > max_chars:
            break
        out.append(line)
        n += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------- retrieval (BM25 over name + description + body)


def _toks(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def recall(home: str, query: str, k: int = 5, types: tuple[str, ...] = TYPES) -> list[dict]:
    """The k memories that best match query. Name and description count three times (they say what the memory is for)."""
    mems = [x for x in load_all(home) if x["type"] in types]
    q = set(_toks(query))
    if not mems or not q:
        return []
    docs = [Counter(_toks(f"{x['name'].replace('-', ' ')} {x['description']} ") * 3 + _toks(x["body"])) for x in mems]
    lens = [sum(d.values()) for d in docs]
    avg = sum(lens) / len(lens) or 1.0
    df = Counter(w for d in docs for w in d)
    N = len(docs)
    scored = []
    for mem, d, ln in zip(mems, docs, lens):
        s = 0.0
        for w in q:
            if w in d:
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * d[w] * 2.2 / (d[w] + 1.2 * (0.25 + 0.75 * ln / avg))
        if s > 0:
            scored.append((s, mem))
    scored.sort(key=lambda x: -x[0])
    return [dict(mem, score=round(s, 3)) for s, mem in scored[:k]]


def block(mems: list[dict], max_chars: int = 1800, body_chars: int = 420) -> str:
    """Memories as a prompt block: name, type, description, then the start of the body."""
    out, n = [], 0
    for mem in mems:
        s = f"- {mem['name']} ({mem['type']}): {mem['description']}\n  {_one_line(mem['body'], body_chars)}"
        if n + len(s) > max_chars:
            break
        out.append(s)
        n += len(s) + 1
    return "\n".join(out)


# ---------------------------------------------------------------- writes (the gardener's ops)


def apply_ops(home: str, ops: list, source: str = "", max_ops: int = 8) -> list[str]:
    """Apply the gardener's ops: {op: add|update|delete, name, type, description, body}. Returns one line per op done.
    add on an existing name updates it; update on a missing name adds it; owner memories are never deleted by the
    gardener (only the owner deletes them, from the app)."""
    done: list[str] = []
    with _LOCK:
        os.makedirs(home, exist_ok=True)
        for op in (ops or [])[:max_ops]:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op") or "").lower()
            name = slug(str(op.get("name") or ""))
            if name == "memory" or kind not in ("add", "update", "delete"):
                continue
            cur = get(home, name)
            if cur is None and kind in ("add", "update"):  # a near-duplicate name updates the memory it duplicates
                twin = _twin(home, name, str(op.get("type") or ""))
                if twin:
                    name, cur = twin["name"], twin
            path = os.path.join(home, name + ".md")
            if kind == "delete":
                if cur and cur["type"] != "owner":
                    try:
                        os.remove(path)
                        done.append(f"deleted {name}")
                    except OSError:
                        pass
                continue
            body = str(op.get("body") or "").strip()
            desc = _one_line(op.get("description"), MAX_DESC)
            if not body or not desc:
                continue
            if cur and source and cur.get("source") and cur["source"] != source and cur["type"] in ("fact", "source") \
                    and cur["body"].strip() not in body:
                # another run's knowledge is kept under the new text (a real gardener overwrote run 002's fact with run 003's)
                body = f"{body}\n\nEarlier ({cur['source']}): {cur['body'][:600]}"
            elif cur and "\n\nEarlier (" in cur["body"] and "Earlier (" not in body:  # the same run updates again: keep the past
                body = body + cur["body"][cur["body"].index("\n\nEarlier ("):]
            t = str(op.get("type") or (cur or {}).get("type") or "fact").lower()
            mem = {"name": name, "type": t if t in TYPES else "fact", "description": desc, "body": body,
                   "created": (cur or {}).get("created") or _now(), "updated": _now(),
                   "source": source or (cur or {}).get("source", ""), "uses": (cur or {}).get("uses", 0)}
            _write(path, render(mem))
            done.append(f"{'updated' if cur else 'added'} {name} ({mem['type']})")
        prune(home)
        write_index(home)
    return done


_KIND_WORDS = {"fact", "facts", "lesson", "lessons", "source", "sources", "owner", "memory", "note", "notes"}


def _twin(home: str, name: str, mtype: str) -> dict | None:
    """An existing memory of the same type whose name says the same thing (word overlap >= 0.6, the type words left out).
    A real run's gardener added no-real-jepa-planning-facts next to fact-no-real-jepa-planning."""
    words = set(name.split("-")) - _KIND_WORDS
    if len(words) < 2:
        return None
    best, best_j = None, 0.0
    for mem in load_all(home):
        if mtype in TYPES and mem["type"] != mtype:
            continue
        other = set(mem["name"].split("-")) - _KIND_WORDS
        j = len(words & other) / max(len(words | other), 1)
        if j > best_j:
            best, best_j = mem, j
    return best if best_j >= 0.6 else None


def bump(home: str, names: list[str]) -> None:
    """Count a use of each memory (a memory that keeps getting recalled survives pruning)."""
    with _LOCK:
        for n in names:
            cur = get(home, n)
            if cur:
                cur["uses"] = int(cur.get("uses") or 0) + 1
                try:
                    _write(os.path.join(home, cur["file"]), render(cur))
                except OSError:
                    pass


def delete(home: str, name: str) -> bool:
    with _LOCK:
        try:
            os.remove(os.path.join(home, slug(name) + ".md"))
        except OSError:
            return False
        write_index(home)
        return True


def prune(home: str, cap: int = MAX_MEMORIES) -> int:
    mems = load_all(home)
    extra = len(mems) - cap
    if extra <= 0:
        return 0
    victims = sorted((x for x in mems if x["type"] != "owner"), key=lambda x: (x["uses"], x["updated"]))[:extra]
    for v in victims:
        try:
            os.remove(os.path.join(home, v["file"]))
        except OSError:
            pass
    return len(victims)


def seed_owner(home: str, taste: str, loves: list[str], source: str = "") -> bool:
    """The owner's taste as an owner memory, so it survives into runs started without the taste form."""
    taste = (taste or "").strip()
    if len(taste) < 20:
        return False
    body = taste + (("\n\nPapers they love: " + ", ".join(loves[:12])) if loves else "")
    cur = get(home, "owner-research-taste")
    if cur and cur["body"].strip() == body.strip()[:MAX_BODY].strip():
        return False
    apply_ops(home, [{"op": "update", "name": "owner-research-taste", "type": "owner",
                      "description": "What the owner cares about in research and what they distrust (their own words)",
                      "body": body}], source=source)
    return True
