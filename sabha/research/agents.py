"""The research pipeline steps (docs/RESEARCH_PLAN.md sections 4 and 5).

Every step starts from files only (the memory packet) and ends by writing files.
Models: s1 triage, reader gate + section map, deep reduce / gaps / planner / writing.
"""
from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import llm, sources as src
from .memory import MEMORY_LESSON, Mission, append, base_id, now, packet, read_json, write, write_json

# ---------------------------------------------------------------- schema helpers

STR = {"type": "string"}
BOOL = {"type": "boolean"}


def OBJ(**props):
    return {"type": "object", "properties": props, "required": list(props)}


def ARR(x):
    return {"type": "array", "items": x}


def ENUM(*v):
    return {"type": "string", "enum": list(v)}


EV = OBJ(sec=STR, quote=STR)
MODEL_TYPES = ("representation_learner", "world_model", "policy", "value_model", "generative_simulator",
               "planner", "benchmark", "dataset", "survey", "theory", "other")
FIELDS = ("training_method", "architecture", "loss", "data", "result", "baseline", "hypothesis",
          "limitation", "compute", "repo", "other")


def _sys(task: str) -> str:
    return MEMORY_LESSON + "\n" + task.strip()


# ---------------------------------------------------------------- mission planner


def _sections(md: str, level: str) -> list[tuple[str, str]]:
    out, cur, buf = [], None, []
    for line in md.split("\n"):
        m = re.match(rf"^{level} +(.+)$", line)
        if m:
            if cur is not None:
                out.append((cur, "\n".join(buf).strip()))
            cur, buf = m.group(1).strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out.append((cur, "\n".join(buf).strip()))
    return out


def _numbered(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", x).strip() for x in re.findall(r"^\s*\d+\.\s+(.+)$", text, re.M)]


def _bullets(text: str) -> list[str]:
    return [x.strip().rstrip(".") for x in re.findall(r"^\s*[-*]\s+(.+)$", text, re.M)]


TRACK_SCHEMA = OBJ(name=STR, one_line=STR, questions=ARR(STR), seeds=ARR(STR), starter_queries=ARR(STR), deliverable=STR)


def plan_mission(m: Mission) -> dict:
    brief = open(m.p("brief.md"), encoding="utf-8").read()
    front = {}
    fm = re.match(r"^---\n(.*?)\n---\n", brief, re.S)
    if fm:
        for line in fm.group(1).split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                front[k.strip()] = v.strip()
        brief = brief[fm.end():]
    h2 = _sections(brief, "##")
    agents = [(t, b) for t, b in h2 if re.match(r"agent\s*\d+\s*:", t, re.I)]
    std = next((b for t, b in h2 if "standard" in t.lower()), "")
    dels = next((b for t, b in h2 if "deliverable" in t.lower()), "")
    synth = next((b for t, b in _sections(brief, "#") if "synthesis" in t.lower()), "")
    if std:
        write(m.p("standards.md"), std)
    head = brief[:5000]
    mission = llm.chat_json("deep", _sys("You set up a literature research mission. Answer in JSON."),
                            "BRIEF (start):\n" + head + "\n\nGive: one_line (one sentence, the research goal), "
                            "cats (3-8 arXiv categories that matter, like cs.LG, cs.RO, cs.CV, cs.AI, stat.ML).",
                            OBJ(one_line=STR, cats=ARR(STR)), max_tokens=300)
    tracks = []
    if agents:
        for i, (title, body) in enumerate(agents, 1):
            name = re.sub(r"^agent\s*\d+\s*:\s*", "", title, flags=re.I)
            t = llm.chat_json("deep", _sys("You turn one research assignment into a track plan. Answer in JSON."),
                              f"ASSIGNMENT '{name}':\n{body[:9000]}\n\nGive: name (short), one_line (what this track must find out), "
                              "questions (every question and 'explore' item in the assignment, rewritten as one clear question each, 6-16), "
                              "seeds (every named paper, model, benchmark or method the assignment says to read, exactly as named), "
                              "starter_queries (8 arXiv search queries of 2-4 keywords each), deliverable (one sentence).",
                              TRACK_SCHEMA, max_tokens=1500)
            write(m.p("tracks", f"t{i}", "assignment.md"), f"# {title}\n\n{body}\n")
            tracks.append({"id": f"t{i}", **t, "weight": 1.5 if "most important" in body.lower() else 1.0})
    else:
        props = llm.chat_json("deep", _sys("You split a research brief into parallel literature tracks. Answer in JSON."),
                              "BRIEF:\n" + brief[:12000] + "\n\nSplit it into 3 to 6 non-overlapping tracks that together cover it. "
                              "For each: name, one_line, questions (6-12), seeds (named papers or methods to read first), "
                              "starter_queries (8 arXiv queries, 2-4 keywords each), deliverable.",
                              OBJ(tracks=ARR(TRACK_SCHEMA)), max_tokens=4000)
        for i, t in enumerate(props.get("tracks", [])[:6], 1):
            write(m.p("tracks", f"t{i}", "assignment.md"), f"# {t['name']}\n\n{t['one_line']}\n")
            tracks.append({"id": f"t{i}", **t, "weight": 1.0})
    for t in tracks:
        t["questions"] = [{"id": f"{t['id']}.q{j}", "text": q} for j, q in enumerate(t["questions"], 1)]
    deliverables = _numbered(dels) or [
        "Executive summary", "Ranked list of 10-15 important papers", "Detailed analysis of the five most relevant papers",
        "Architectures", "Training procedures and loss functions", "Open-source repositories and datasets",
        "Benchmark recommendations", "Limitations and open questions", "Three promising research directions",
        "A minimum viable experiment", "Comparison of approaches", "Questions for the other tracks"]
    must = []
    mm = re.search(r"must present:?(.*?)(?:\n\n[A-Z*]|\Z)", synth, re.S | re.I)
    if mm:
        must = _bullets(mm.group(1))
    plan = {"mission": m.id, "one_line": mission.get("one_line", ""), "cats": mission.get("cats", []),
            "hours": float(front.get("hours", 12)), "mode": front.get("mode", "queue"),
            "tracks": tracks, "deliverables": deliverables,
            "synthesis_questions": _numbered(synth), "synthesis_must_present": must, "created": now()}
    write_json(m.p("plan.json"), plan)
    m.log("planned", tracks=len(tracks))
    return plan


# ---------------------------------------------------------------- seeds


def resolve_seeds(m: Mission, idx, budget: src.Budget) -> list[dict]:
    out = []
    for t in m.plan["tracks"]:
        for name in t.get("seeds", [])[:12]:
            cands = {}
            for c in idx.query(f"{name}. {t['name']}", k=6):
                cands[c["id"]] = c["title"]
            try:
                for c in src.arxiv_search(re.sub(r"[^\w\s.-]", " ", name)[:80], 8):
                    cands.setdefault(c["id"], c["title"])
            except Exception:
                pass
            tried = ["index", "arxiv"]
            pick = _pick_seed(name, t, cands)
            if not pick and budget.take("seeds"):
                tried.append("tavily")
                try:
                    hits = src.tavily(f"{name} arxiv paper", 5, domains=["arxiv.org"])
                    ids = [mm.group(1) for h in hits for mm in [src.ARXIV_ID.search(h["url"])] if mm]
                    for aid in ids[:3]:
                        if aid not in cands:
                            meta = idx.get(aid)
                            cands[aid] = meta["title"] if meta else next((h["title"] for h in hits if aid in h["url"]), aid)
                    pick = _pick_seed(name, t, cands)
                except Exception:
                    pass
            rec = {"name": name, "track": t["id"], "id": pick, "status": "resolved" if pick else "unresolved", "tried": tried}
            out.append(rec)
            if pick and not m.seen(pick):
                m.mark(pick, state="queued", track=t["id"], rel=3, prio=2.0, via="seed", why=f"seed: {name}", seed=name)
    write_json(m.p("memory/seeds.json"), out)
    m.log("seeds", resolved=sum(1 for s in out if s["id"]), total=len(out))
    return out


def _pick_seed(name: str, t: dict, cands: dict) -> str | None:
    if not cands:
        return None
    ids = list(cands)[:14]
    lines = "\n".join(f"{i}: {cands[i]}" for i in ids)
    r = llm.chat_json("deep", _sys("You match a named paper or method to the arXiv paper that introduced it. Answer in JSON."),
                      f"NAME: {name}\nCONTEXT: {t['name']}\nCANDIDATES (arXiv id: title):\n{lines}\n\n"
                      "Which candidate is the original paper that introduced this name? match = its id, or \"none\" if no candidate is that paper.",
                      OBJ(match=ENUM(*ids, "none"), why=STR), max_tokens=120)
    return None if r.get("match") in (None, "none") else r["match"]


# ---------------------------------------------------------------- scouting


def _jaccard(a: str, b: str) -> float:
    x, y = set(a.lower().split()), set(b.lower().split())
    return len(x & y) / max(len(x | y), 1)


def triage(m: Mission, cands: list[dict], steer: dict) -> list[dict]:
    tracks = m.plan["tracks"]
    tlist = "\n".join(f"{t['id']}: {t['name']}. {t['one_line']}" for t in tracks)
    schema = OBJ(track=ENUM(*[t["id"] for t in tracks], "none"), rel={"type": "integer", "enum": [0, 1, 2, 3]},
                 kind=ENUM("method", "benchmark", "dataset", "survey", "theory", "other"))
    sysmsg = ("You sort arXiv papers for a research team. Read the title and abstract and answer in JSON.\n"
              "track: the one track this paper serves best, or none.\n"
              "rel 3: it directly answers a track question with a method or a measurement.\n"
              "rel 2: a related method, benchmark or dataset the track should know.\n"
              "rel 1: same broad area, not useful. rel 0: off topic.")

    def one(c):
        try:
            r = llm.chat_json("s1", sysmsg, f"MISSION: {m.plan.get('one_line', '')}\nTRACKS:\n{tlist}\n\n"
                              f"TITLE: {c['title']}\nABSTRACT: {c.get('abstract', '')[:1500]}", schema, max_tokens=40)
            return {**c, **r}
        except llm.LLMError:
            return {**c, "track": "none", "rel": 0, "kind": "other", "err": True}

    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, cands))


def priority(c: dict, boost: float = 0.0) -> float:
    year = 2015
    mm = re.search(r"(20\d\d)", c.get("created") or c.get("submitted") or "")
    if mm:
        year = int(mm.group(1))
    elif re.match(r"\d{4}\.", c.get("id", "")):
        year = 2000 + int(c["id"][:2])
    rec = min(max((year - 2015) / 11, 0), 1)
    return round(0.45 * c.get("rel", 0) / 3 + 0.2 * max(c.get("cos", 0), 0) + 0.15 * rec
                 + 0.2 * min(c.get("cited_by", 0), 5) / 5 + boost, 4)


GAP_SCHEMA = OBJ(gaps=ARR(OBJ(q=STR, why=STR)), queries=ARR(OBJ(text=STR, via=ENUM("index", "arxiv", "tavily"), q=STR)))


def scout(m: Mission, tid: str, idx, budget: src.Budget, steer: dict) -> dict:
    t = m.track(tid)
    claims = m.claims(tid)
    cover = {q["id"]: 0 for q in t.get("questions", [])}
    for c in claims.values():
        if c.get("status") in ("supported", "contested") and c.get("q") in cover:
            cover[c["q"]] += 1
    for card in m.cards():
        if tid in card.get("tracks", []):
            for cl in card.get("claims", []):
                if cl.get("q") in cover:
                    cover[cl["q"]] += 1
    past = m.queries(tid)
    hist = "\n".join(f"- \"{q['text']}\" via {q['via']}: {q.get('n_new', 0)} new, {q.get('n_rel2', 0)} useful" for q in past[-12:])
    unused = [s for s in t.get("starter_queries", []) if not any(_jaccard(s, q["text"]) >= 0.7 for q in past)]
    pri = [p for p in steer.get("priority_topics", []) if p.get("track") in (tid, "all")]
    user = (packet(m, tid, steering_lines=steer.get("lessons", [])) +
            "\n\nCOVERAGE (claims found per question, zeros are gaps):\n" + "\n".join(f"- {k}: {v}" for k, v in cover.items()) +
            "\n\nPAST QUERIES AND YIELDS:\n" + (hist or "(none yet)") +
            ("\n\nUNUSED STARTER QUERIES:\n" + "\n".join(f"- {s}" for s in unused[:8]) if unused else "") +
            ("\n\nVALIDATOR PRIORITY TOPICS:\n" + "\n".join(f"- {p['topic']}" for p in pri) if pri else "") +
            "\n\nFind the biggest gaps and write 6 new search queries. Each query: 2-4 keywords (arXiv search needs every word to match), "
            "different from past queries, aimed at one question id. via: index (semantic search over all arXiv abstracts, best for concepts), "
            "arxiv (keyword search, best for names and exact terms), tavily (web search, at most one, only for very recent work or names arXiv misses).")
    try:
        g = llm.chat_json("deep", _sys("You plan the next literature searches for your track. Answer in JSON."), user, GAP_SCHEMA, max_tokens=700)
    except llm.LLMError as e:
        m.log("scout_fail", track=tid, err=str(e)[:200])
        return {"new": 0}
    banned_q = [b.lower() for b in steer.get("banned", {}).get("queries", []) + steer.get("banned", {}).get("patterns", [])]
    banned_cats = set(steer.get("banned", {}).get("cats", []))
    banned_ids = set(steer.get("banned", {}).get("ids", []))
    total_new, total_rel2, used_tavily = 0, 0, False
    for q in g.get("queries", [])[:6]:
        text = q.get("text", "").strip()
        if not text or any(_jaccard(text, p["text"]) >= 0.7 for p in past) or any(b in text.lower() for b in banned_q):
            m.log("query_refused", track=tid, text=text)
            continue
        via = q.get("via", "index")
        hits = []
        try:
            hits = idx.query(text, k=30)
            if via == "arxiv":
                for h in src.arxiv_search(text, 25):
                    meta = idx.get(h["id"])
                    hits.append({**h, **(meta or {}), "cos": 0.0})
            elif via == "tavily" and not used_tavily and budget.take("scout"):
                used_tavily = True
                for h in src.tavily(text + " arxiv", 5, domains=["arxiv.org"]):
                    mm = src.ARXIV_ID.search(h["url"])
                    if mm:
                        meta = idx.get(mm.group(1)) or {"id": mm.group(1), "title": h["title"], "abstract": h["content"]}
                        hits.append({**meta, "cos": 0.0})
        except Exception as e:
            m.log("search_fail", track=tid, text=text, err=str(e)[:200])
        fresh, seen_here = [], set()
        for h in hits:
            aid = base_id(h["id"])
            if aid in seen_here or m.seen(aid) or aid in banned_ids:
                continue
            if banned_cats and banned_cats & set(h.get("cats", [])):
                continue
            seen_here.add(aid)
            fresh.append({**h, "id": aid})
        fresh = fresh[:20]
        judged = triage(m, fresh, steer) if fresh else []
        n_rel2 = 0
        for c in judged:
            boost = sum(p.get("boost", 0.2) for p in pri if p.get("topic", "").lower() in (c["title"] + " " + c.get("abstract", "")).lower())
            ok = c.get("rel", 0) >= 2 and c.get("track") != "none"
            n_rel2 += ok
            m.mark(c["id"], state="queued" if ok else "rejected", track=c.get("track") if ok else tid,
                   rel=c.get("rel", 0), cos=round(c.get("cos", 0), 3), prio=priority(c, boost), via=via,
                   qid=q.get("q", ""), title=c["title"][:200], kind=c.get("kind"),
                   created=c.get("created") or c.get("submitted", ""), why="triage")
        append(m.p("tracks", tid, "queries.jsonl"), {"qid": f"{tid}.s{len(past) + 1}", "track": tid, "q": q.get("q", ""),
                                                    "text": text, "via": via, "n_hits": len(hits), "n_new": len(fresh), "n_rel2": n_rel2})
        past.append({"text": text, "via": via})
        total_new += len(fresh)
        total_rel2 += n_rel2
    m.log("scouted", track=tid, new=total_new, rel2=total_rel2)
    return {"new": total_new, "rel2": total_rel2}


def triage_refs(m: Mission, idx, steer: dict, limit: int = 30) -> int:
    """Snowballing: references cited by carded papers, most-cited first."""
    cands = [r for r in m.reg.values() if r.get("state") == "cand"]
    cands.sort(key=lambda r: -r.get("cited_by", 0))
    batch = []
    for r in cands[:limit]:
        meta = idx.get(r["id"])
        if meta:
            batch.append({**meta, "cited_by": r.get("cited_by", 0)})
        else:
            m.mark(r["id"], state="cand_unindexed")
    judged = triage(m, batch, steer) if batch else []
    for c in judged:
        ok = c.get("rel", 0) >= 2 and c.get("track") != "none"
        m.mark(c["id"], state="queued" if ok else "rejected", track=c.get("track") if ok else None, rel=c.get("rel", 0),
               prio=priority(c), via="refs", title=c["title"][:200], kind=c.get("kind"), created=c.get("created", ""), why="cited")
    return len(judged)


# ---------------------------------------------------------------- reading one paper

GATE_SCHEMA = OBJ(keep=BOOL, track=STR, model_type=ARR(ENUM(*MODEL_TYPES)), why=STR)
MAP_SCHEMA = OBJ(facts=ARR(OBJ(field=ENUM(*FIELDS), text=STR, sec=STR, quote=STR, status=ENUM("measured", "stated", "hypothesis"))),
                 repo_urls=ARR(STR), datasets=ARR(STR))
CARD_SCHEMA = OBJ(
    one_line=STR, model_type=ARR(ENUM(*MODEL_TYPES)),
    training_method=OBJ(text=STR, ev=ARR(EV)), architecture=OBJ(text=STR, ev=ARR(EV)),
    losses=ARR(OBJ(name=STR, form=STR, ev=ARR(EV))),
    data=ARR(OBJ(name=STR, size=STR, license=STR, ev=ARR(EV))),
    results=ARR(OBJ(claim=STR, metric=STR, value=STR, setting=STR, baselines=ARR(STR), ev=ARR(EV))),
    hypotheses=ARR(OBJ(text=STR, ev=ARR(EV))),
    limitations=ARR(OBJ(text=STR, by=ENUM("authors", "reader"), ev=ARR(EV))),
    compute=OBJ(text=STR, repro_cost=ENUM("low", "medium", "high", "unknown"), ev=ARR(EV)),
    baselines=ARR(STR),
    causal_evidence=ENUM("intervention", "counterfactual_eval", "none_prediction_only", "n/a"),
    claims=ARR(OBJ(text=STR, q=STR, status=ENUM("measured", "hypothesis"), relation=ENUM("new", "supports", "contradicts"),
                   ref=STR, ev=ARR(EV))),
    questions_for_others=ARR(OBJ(to=STR, q=STR)))

_ORDER = [("method|approach|model|architecture|framework|preliminar", 0), ("train|objective|loss|learning|pretrain", 1),
          ("experiment|result|evaluat|benchmark|ablation", 2), ("limitation|discussion|conclusion|future", 3),
          ("implementation|hyper|compute|detail|appendix|setup", 4), ("introduction|background|related", 5)]


def _rank_sections(sections: list[dict]) -> list[dict]:
    def key(s):
        t = s["title"].lower()
        for pat, r in _ORDER:
            if re.search(pat, t):
                return r
        return 3
    return sorted(sections, key=key)


def _numbers_ok(value: str, full_raw: str) -> bool:
    nums = re.findall(r"\d+(?:\.\d+)?", value or "")
    return all(n in full_raw for n in nums)


def _ev_ok(ev: list, fullnorm: str) -> list:
    keep = []
    for e in ev or []:
        q = src.norm(e.get("quote", ""))
        if len(q) >= 12 and q in fullnorm:
            keep.append(e)
    return keep


def read_paper(m: Mission, aid: str, idx, budget: src.Budget, steer: dict, max_chunks: int = 10, tier_b: bool = False) -> str:
    """Read one paper and write its card. Returns carded|rejected|failed."""
    reg = m.reg.get(aid, {})
    tid = reg.get("track") or m.track_ids()[0]
    m.mark(aid, state="reading")
    t0 = time.time()
    try:
        meta = src.abs_meta(aid)
        paper = src.paper_text(aid)
    except Exception as e:
        m.mark(aid, state="failed", why=f"fetch: {str(e)[:160]}")
        return "failed"
    full = paper["full"]
    fullnorm = src.norm(full)
    tlist = "\n".join(f"{t['id']}: {t['name']}. {t['one_line']}" for t in m.plan["tracks"])
    intro = paper["sections"][0]["text"][:4000] if paper["sections"] else ""
    try:
        gate = llm.chat_json("reader", _sys("You decide if a paper deserves a full read for the team. Answer in JSON."),
                             f"TRACKS:\n{tlist}\nSUGGESTED TRACK: {tid}\n\nTITLE: {meta['title']}\nABSTRACT: {meta['abstract'][:2000]}\n\n"
                             f"INTRODUCTION (start):\n{intro}\n\nkeep: true if it gives a method, measurement, benchmark or dataset that a track needs. "
                             "track: the best track id. model_type: what the paper's main system is (a representation learner, world model, policy, "
                             "value model and generative simulator are different things).", GATE_SCHEMA, max_tokens=150)
    except llm.LLMError as e:
        m.mark(aid, state="queued", why=f"gate llm: {str(e)[:120]}")
        return "failed"
    if not gate.get("keep") and reg.get("via") != "seed" and not tier_b:
        write_json(m.p("rejected", aid + ".json"), {"id": aid, "title": meta["title"], "gate": gate, "ts": now()})
        m.mark(aid, state="rejected", why="gate: " + gate.get("why", "")[:200])
        return "rejected"
    if gate.get("track") in m.track_ids():
        tid = gate["track"]
    t = m.track(tid)
    qs = "\n".join(f"- {q['id']}: {q['text']}" for q in t.get("questions", []))
    chunks = _rank_sections(paper["sections"])[:(24 if tier_b else max_chunks)]
    map_sys = _sys("""You extract facts from ONE part of a paper for your track. Answer in JSON.
FIELDS: training_method (data, stages, schedule), architecture (modules, encoders, predictor, sizes), loss (objective and its form),
data (dataset, size, license), result (a measured number with metric and setting), baseline (what it is compared against),
hypothesis (what the authors believe but did not measure), limitation, compute (GPUs, hours, parameters), repo (code link), other.
status: measured = an experiment in this paper shows it; stated = a description of what they did; hypothesis = belief or speculation.
quote: copy 8 to 25 words EXACTLY from the text below (it is checked by a program; a quote that is not in the text is thrown away).
At most 10 facts, the ones that matter most for the track questions first. Skip anything not in this part.""")

    def mapper(sec, role="reader"):
        try:
            return llm.chat_json(role, map_sys, f"TRACK {tid} QUESTIONS:\n{qs}\n\nPAPER: {meta['title']}\nSECTION: {sec['title']}\n\n{sec['text']}",
                                 MAP_SCHEMA, max_tokens=900)
        except llm.LLMError:
            return {"facts": [], "repo_urls": [], "datasets": []}

    with ThreadPoolExecutor(6) as ex:
        maps = list(ex.map(mapper, chunks))
    facts, total = [], 0
    for sec, mp in zip(chunks, maps):
        for f in mp.get("facts", []):
            total += 1
            q = src.norm(f.get("quote", ""))
            if len(q) >= 12 and q in fullnorm:
                facts.append({**f, "sec": f.get("sec") or sec["title"]})
    quote_pass = round(len(facts) / total, 2) if total else 0.0
    if len(facts) < 3:
        write_json(m.p("rejected", aid + ".json"), {"id": aid, "title": meta["title"], "why": "too few verified facts",
                                                    "quote_pass": quote_pass, "facts_total": total, "ts": now()})
        m.mark(aid, state="failed", why=f"quote check: {len(facts)}/{total} facts verified")
        return "failed"
    near = _near_claims(m, tid, meta["abstract"], idx)
    flines = "\n".join(f"[{f['field']}|{f['status']}] {f['text']} -- \"{f['quote']}\" ({f['sec']})" for f in facts[:60])
    try:
        card = llm.chat_json("deep", _sys("""You write the paper card: the team's permanent memory of this paper. Answer in JSON.
Use ONLY the verified facts and metadata below. Copy evidence quotes exactly from the facts (they were checked against the paper).
Anything the facts do not cover is "not_stated". results: only numbers that appear in the facts.
claims: 2 to 5 claims that answer your track questions (q = the question id). relation: supports / contradicts a ledger claim
in the packet (ref = its id) or new (ref = ""). causal_evidence: intervention or counterfactual_eval only if the paper actually
intervenes or tests counterfactuals; predicting future observations is none_prediction_only.
questions_for_others: questions this paper raises that another track should answer (to = track id)."""),
                            packet(m, tid, near) + f"\n\nPAPER {aid} {meta.get('version', '')}: {meta['title']}\nAUTHORS: {', '.join(meta['authors'][:6])}\n"
                            f"FIRST SUBMITTED: {meta['first_submitted']}  LAST REVISED: {meta['last_revised']}  COMMENTS: {meta['comments'][:200]}\n"
                            f"ABSTRACT: {meta['abstract'][:1800]}\n\nVERIFIED FACTS:\n{flines}", CARD_SCHEMA, max_tokens=2200)
    except llm.LLMError as e:
        m.mark(aid, state="queued", why=f"reduce llm: {str(e)[:120]}")
        return "failed"
    lint = []
    # evidence must be real quotes
    for k in ("training_method", "architecture", "compute"):
        if isinstance(card.get(k), dict):
            card[k]["ev"] = _ev_ok(card[k].get("ev"), fullnorm)
            if not card[k]["ev"] and card[k].get("text", "not_stated") != "not_stated":
                lint.append(f"{k}: no verified evidence")
    for k in ("losses", "data", "hypotheses", "limitations", "claims"):
        for it in card.get(k, []) or []:
            it["ev"] = _ev_ok(it.get("ev"), fullnorm)
    good = []
    for r in card.get("results", []) or []:
        r["ev"] = _ev_ok(r.get("ev"), fullnorm)
        if _numbers_ok(r.get("value", ""), full):
            good.append(r)
        else:
            lint.append(f"result dropped, number not in paper: {r.get('value', '')[:40]}")
    card["results"] = good
    if card.get("causal_evidence") not in ("intervention", "counterfactual_eval"):
        for cl in card.get("claims", []):
            if re.search(r"\bcausal", cl.get("text", ""), re.I):
                cl["text"] = "(prediction only, no causal test) " + cl["text"]
                lint.append("causal wording without causal evidence")
    # official repo: must be in the paper text or a Tavily hit, and answer on the GitHub API
    repo = {"url": None, "status": "not_found", "license": None, "last_push": None, "src": None}
    in_text = src.repo_urls(full)
    for mp in maps:
        for u in mp.get("repo_urls", []):
            for x in src.repo_urls(u):
                if x.lower() in full.lower() and x not in in_text:
                    in_text.append(x)
    for u in in_text[:4]:
        g = src.github_repo(u)
        if g.get("exists"):
            repo = {"url": g["url"], "status": "verified", "license": g.get("license"), "last_push": g.get("last_push"),
                    "stars": g.get("stars"), "src": "paper"}
            break
        repo = {**repo, "url": u, "status": "in_paper_unverified", "src": "paper"}
    if repo["status"] == "not_found" and (reg.get("rel", 0) >= 3 or reg.get("via") == "seed") and budget.take("repo"):
        try:
            for h in src.tavily(f"\"{meta['title'][:120]}\" official code github", 5):
                for u in src.repo_urls(h["url"] + " " + h["content"]):
                    g = src.github_repo(u)
                    if g.get("exists"):
                        repo = {"url": g["url"], "status": "verified", "license": g.get("license"),
                                "last_push": g.get("last_push"), "stars": g.get("stars"), "src": "tavily"}
                        break
                if repo["status"] == "verified":
                    break
        except Exception:
            pass
    n_ev = sum(len(card.get(k, {}).get("ev", [])) for k in ("training_method", "architecture", "compute") if isinstance(card.get(k), dict))
    n_ev += sum(len(it.get("ev", [])) for k in ("losses", "data", "results", "hypotheses", "limitations", "claims") for it in card.get(k, []) or [])
    if n_ev < 3:
        lint.append("fewer than 3 verified evidence quotes")
    tracks = [tid] + [x for x in [reg.get("track")] if x and x != tid and x in m.track_ids()]
    full_card = {"id": aid, "version": meta.get("version"), "title": meta["title"], "authors": meta["authors"],
                 "first_submitted": meta["first_submitted"], "last_revised": meta["last_revised"],
                 "venue": meta.get("journal_ref") or meta.get("comments", "")[:160],
                 "source": {"kind": paper["kind"], "chunks_read": len(chunks), "chunks_total": len(paper["sections"])},
                 "tracks": tracks, "relevance": reg.get("rel", 2), "via": reg.get("via"), "seed": reg.get("seed"),
                 **card, "official_repo": repo, "refs_arxiv": paper["refs"][:80],
                 "independent_eval": {"as_baseline_in": []},
                 "quality": {"quote_pass": quote_pass, "facts_verified": len(facts), "facts_total": total, "evidence": n_ev,
                             "lint": lint, "grade": "ok" if quote_pass >= 0.7 and n_ev >= 3 else "weak", "deep": tier_b},
                 "read_seconds": round(time.time() - t0), "ts": now()}
    write_json(m.p("cards", aid + ".json"), full_card)
    write(m.p("cards", aid + ".md"), card_md(full_card))
    m.mark(aid, state="carded", track=tid, title=meta["title"][:200], grade=full_card["quality"]["grade"])
    for qo in card.get("questions_for_others", [])[:3]:
        if qo.get("to") in m.track_ids() and qo.get("q"):
            m.post(tid, qo["to"], f"{qo['q']} (from card {aid})")
    for r in paper["refs"]:
        r = base_id(r)
        if r == aid:
            continue
        prev = m.reg.get(r)
        if prev is None:
            m.mark(r, state="cand", cited_by=1, via="refs")
        else:
            m.reg[r]["cited_by"] = prev.get("cited_by", 0) + 1
            if prev.get("state") == "cand":
                m.mark(r, cited_by=m.reg[r]["cited_by"])
    m.log("carded", id=aid, track=tid, secs=full_card["read_seconds"], quote_pass=quote_pass, grade=full_card["quality"]["grade"])
    return "carded"


def _near_claims(m: Mission, tid: str, text: str, idx, k: int = 8) -> list[dict]:
    claims = [c for c in m.claims(tid).values() if c.get("status") != "retired"]
    if not claims:
        return []
    words = set(re.findall(r"[a-z]{4,}", text.lower()))
    claims.sort(key=lambda c: -len(words & set(re.findall(r"[a-z]{4,}", c.get("text", "").lower()))))
    return claims[:k]


def card_md(c: dict) -> str:
    def ev(lst):
        return "".join(f"\n  > \"{e['quote']}\" ({e['sec']})" for e in (lst or [])[:2])
    L = [f"# {c['title']}", f"`{c['id']}{c.get('version') or ''}` | first submitted {c.get('first_submitted')} | revised {c.get('last_revised')} "
         f"| tracks {', '.join(c.get('tracks', []))} | relevance {c.get('relevance')} | grade {c['quality']['grade']}",
         "", f"**One line.** {c.get('one_line', '')}", f"**Model type.** {', '.join(c.get('model_type', []))}",
         f"**Causal evidence.** {c.get('causal_evidence')}", "",
         f"**Training method.** {c.get('training_method', {}).get('text', '')}{ev(c.get('training_method', {}).get('ev'))}",
         f"**Architecture.** {c.get('architecture', {}).get('text', '')}{ev(c.get('architecture', {}).get('ev'))}", "", "**Losses.**"]
    L += [f"- {x.get('name')}: {x.get('form')}{ev(x.get('ev'))}" for x in c.get("losses", [])]
    L += ["", "**Data.**"] + [f"- {x.get('name')} ({x.get('size')}, license {x.get('license')}){ev(x.get('ev'))}" for x in c.get("data", [])]
    L += ["", "**Results (measured).**"] + [f"- {x.get('claim')}: {x.get('metric')} = {x.get('value')} ({x.get('setting')}); baselines {', '.join(x.get('baselines', []))}{ev(x.get('ev'))}" for x in c.get("results", [])]
    L += ["", "**Author hypotheses (not measured).**"] + [f"- {x.get('text')}{ev(x.get('ev'))}" for x in c.get("hypotheses", [])]
    L += ["", "**Limitations.**"] + [f"- ({x.get('by')}) {x.get('text')}{ev(x.get('ev'))}" for x in c.get("limitations", [])]
    cp = c.get("compute", {})
    r = c.get("official_repo", {})
    L += ["", f"**Compute.** {cp.get('text', '')} (repro cost: {cp.get('repro_cost')}){ev(cp.get('ev'))}",
          f"**Official repo.** {r.get('url')} [{r.get('status')}, license {r.get('license')}, last push {r.get('last_push')}]",
          f"**Baselines.** {', '.join(c.get('baselines', []))}", "", "**Claims for the ledger.**"]
    L += [f"- ({x.get('q')}, {x.get('status')}, {x.get('relation')} {x.get('ref', '')}) {x.get('text')}{ev(x.get('ev'))}" for x in c.get("claims", [])]
    q = c["quality"]
    L += ["", f"_Read {c['source']['chunks_read']}/{c['source']['chunks_total']} parts from {c['source']['kind']}; "
          f"quote check {q['quote_pass']} ({q['facts_verified']}/{q['facts_total']}); lint: {'; '.join(q['lint']) or 'clean'}_"]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- consolidation (episodic -> semantic)

OPS_SCHEMA = OBJ(ops=ARR(OBJ(op=ENUM("add", "support", "contradict", "refine", "retire"), claim=STR, text=STR, q=STR,
                             cards=ARR(STR), why=STR)),
                 answers=ARR(OBJ(board=STR, text=STR, cards=ARR(STR))), board=ARR(OBJ(to=STR, q=STR)))


def consolidate(m: Mission, tid: str, since_ids: list[str]) -> int:
    t = m.track(tid)
    ledger = m.claims(tid)
    new = []
    for aid in since_ids:
        c = m.card(aid)
        if c:
            for cl in c.get("claims", []):
                new.append(f"[{aid}] ({cl.get('q')}, {cl.get('status')}, {cl.get('relation')} {cl.get('ref', '')}) {cl.get('text', '')}")
    if not new:
        return 0
    led = "\n".join(f"- {c['id']} [{c.get('status')}] ({c.get('q')}) {c.get('text', '')[:240]} | for {c.get('for', [])} against {c.get('against', [])}"
                    for c in list(ledger.values())[-60:] if c.get("status") != "retired")
    opened = [b for b in m.board().values() if b.get("status") == "open" and b.get("to") in (tid, "all")][:6]
    user = (packet(m, tid) + "\n\nCURRENT LEDGER:\n" + (led or "(empty)") + "\n\nNEW CLAIMS FROM NEW CARDS:\n" + "\n".join(new[:50]) +
            ("\n\nOPEN BOARD QUESTIONS:\n" + "\n".join(f"- {b['id']}: {b['q']}" for b in opened) if opened else "") +
            "\n\nTurn the new claims into ledger operations. add: a claim the ledger lacks (claim = \"\"). support / contradict: "
            "cards that back or break an existing claim (claim = its id). refine: sharpen an existing claim's text. retire: a claim "
            "shown wrong. Merge duplicates. Every op lists its card ids. answers: board questions the cards now answer.")
    try:
        r = llm.chat_json("deep", _sys("You consolidate your track's memory: episodic cards into the semantic ledger. Answer in JSON."),
                          user, OPS_SCHEMA, max_tokens=1400)
    except llm.LLMError as e:
        m.log("consolidate_fail", track=tid, err=str(e)[:200])
        return 0
    path = m.p("tracks", tid, "claims.jsonl")
    n = 0
    for op in r.get("ops", [])[:30]:
        cards = [base_id(x) for x in op.get("cards", []) if m.card(x)]
        cid = op.get("claim", "")
        if op["op"] == "add" or cid not in ledger:
            if not op.get("text") or not cards:
                continue
            cid = m.next_claim_id(tid)
            rec = {"id": cid, "q": op.get("q", ""), "text": op["text"][:500], "status": "supported" if len(cards) else "hypothesis",
                   "for": cards, "against": []}
        else:
            old = ledger[cid]
            rec = {**old}
            if op["op"] == "support":
                rec["for"] = sorted(set(old.get("for", []) + cards))
            elif op["op"] == "contradict":
                rec["against"] = sorted(set(old.get("against", []) + cards))
                rec["status"] = "contested"
                m.post(tid, "all", f"Contested claim {cid}: {old.get('text', '')[:200]} -- against: {', '.join(cards)}")
            elif op["op"] == "refine" and op.get("text"):
                rec["text"] = op["text"][:500]
            elif op["op"] == "retire":
                rec["status"] = "retired"
            rec.pop("ts", None)
        rec["why"] = op.get("why", "")[:200]
        append(path, rec)
        ledger[cid] = rec
        n += 1
    for a in r.get("answers", [])[:6]:
        b = m.board().get(a.get("board", ""))
        if b and a.get("text"):
            append(m.p("memory/board.jsonl"), {**b, "status": "answered", "answer": a["text"][:600], "cards": a.get("cards", []), "ts": now()})
    for b in r.get("board", [])[:3]:
        if b.get("to") in m.track_ids() + ["all", "validator"] and b.get("q"):
            m.post(tid, b["to"], b["q"])
    render_digest(m, tid)
    m.log("consolidated", track=tid, ops=n)
    return n


def render_digest(m: Mission, tid: str, max_chars: int = 4500) -> None:
    t = m.track(tid)
    ledger = [c for c in m.claims(tid).values() if c.get("status") != "retired"]
    L = [f"# {tid} {t.get('name', '')}: digest ({now()})"]
    for q in t.get("questions", []):
        cs = [c for c in ledger if c.get("q") == q["id"]]
        if not cs:
            continue
        L.append(f"\n## {q['id']} {q['text'][:120]}")
        for c in sorted(cs, key=lambda c: -len(c.get("for", []))):
            L.append(f"- {c['id']} [{c['status']}] {c['text'][:260]} (for {', '.join(c.get('for', [])[:4])}"
                     + (f"; against {', '.join(c.get('against', [])[:4])}" if c.get("against") else "") + ")")
    other = [c for c in ledger if c.get("q") not in {q["id"] for q in t.get("questions", [])}]
    if other:
        L.append("\n## other")
        L += [f"- {c['id']} [{c['status']}] {c['text'][:200]}" for c in other[:10]]
    write(m.p("tracks", tid, "digest.md"), "\n".join(L)[:max_chars] + "\n")


def rebuild_playbook(m: Mission, lessons: list[str]) -> None:
    rows = []
    for tid in m.track_ids():
        rows += [q for q in m.queries(tid)]
    good = sorted([q for q in rows if q.get("n_rel2", 0) > 0], key=lambda q: -q["n_rel2"])[:10]
    dead = [q for q in rows if q.get("n_new", 0) == 0][-8:]
    L = ["Searches that found useful papers (copy their style):"] + [f"- \"{q['text']}\" via {q['via']}: {q['n_rel2']} useful" for q in good]
    L += ["Searches that found nothing new (avoid these patterns):"] + [f"- \"{q['text']}\" via {q['via']}" for q in dead]
    L += ["Reading habits: prefer method, training and experiment sections; quote exactly; mark author beliefs as hypothesis."]
    if lessons:
        L += ["Validator lessons:"] + [f"- {x}" for x in lessons[-12:]]
    write(m.p("memory/playbook.md"), "\n".join(L) + "\n")


# ---------------------------------------------------------------- reports and synthesis

REPORT_GROUPS = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11)]


def rank_cards(m: Mission, tid: str) -> list[dict]:
    cards = [c for c in m.cards() if tid in c.get("tracks", [])]
    cited = {}
    for c in m.cards():
        for r in c.get("refs_arxiv", []):
            cited[r] = cited.get(r, 0) + 1

    def score(c):
        q = c.get("quality", {})
        year = int(c["id"][:2]) if re.match(r"\d{4}\.", c["id"]) else 15
        return (0.4 * c.get("relevance", 2) / 3 + 0.25 * min(cited.get(c["id"], 0), 5) / 5 + 0.2 * q.get("quote_pass", 0)
                + 0.15 * min(max((year - 15) / 11, 0), 1) + (0.2 if c.get("seed") else 0))
    return sorted(cards, key=lambda c: -score(c))


def _card_brief(c: dict, n_res: int = 3) -> str:
    res = "; ".join(f"{r.get('metric')} {r.get('value')} ({r.get('setting', '')[:60]})" for r in c.get("results", [])[:n_res])
    return (f"[{c['id']}] {c['title'][:120]} ({c.get('first_submitted', '')}) | {c.get('one_line', '')[:220]} | type "
            f"{','.join(c.get('model_type', []))} | train: {c.get('training_method', {}).get('text', '')[:200]} | losses: "
            f"{'; '.join((l.get('name') or '') + ' ' + (l.get('form') or '')[:80] for l in c.get('losses', [])[:3])} | results: {res} | "
            f"repo: {c.get('official_repo', {}).get('url')} [{c.get('official_repo', {}).get('status')}] | compute: "
            f"{c.get('compute', {}).get('text', '')[:120]} | causal: {c.get('causal_evidence')} | limits: "
            f"{'; '.join(l.get('text', '')[:100] for l in c.get('limitations', [])[:2])}")


def write_report(m: Mission, tid: str) -> str:
    t = m.track(tid)
    ranked = rank_cards(m, tid)
    if len(ranked) < 3:
        return ""
    dig = ""
    try:
        dig = open(m.p("tracks", tid, "digest.md"), encoding="utf-8").read()[:4000]
    except OSError:
        pass
    ctx = (f"TRACK {tid}: {t['name']}. {t['one_line']}\nDELIVERABLE: {t.get('deliverable', '')}\n\nASSIGNMENT:\n" +
           open(m.p("tracks", tid, "assignment.md"), encoding="utf-8").read()[:3000] + "\n\nSEMANTIC DIGEST:\n" + dig +
           "\n\nCARDS, RANKED (cite as [id]):\n" + "\n".join(_card_brief(c) for c in ranked[:18]))
    seeds = [s for s in (read_json(m.p("memory/seeds.json"), []) or []) if s.get("track") == tid]
    unresolved = [s["name"] for s in seeds if not s.get("id")]
    if unresolved:
        ctx += "\n\nPRIMARY SOURCE NOT LOCATED (say so, do not describe them from memory): " + ", ".join(unresolved)
    dels = m.plan.get("deliverables", [])
    std = ""
    try:
        std = open(m.p("standards.md"), encoding="utf-8").read()[:2500]
    except OSError:
        pass
    parts = [f"# {tid} {t['name']}: report\n\n_Rewritten {now()} from {len(ranked)} cards. Every claim cites card ids; "
             "numbers come from the cards' verified quotes._\n"]
    for grp in REPORT_GROUPS:
        names = [dels[i] for i in grp if i < len(dels)]
        if not names:
            continue
        try:
            txt = llm.chat("deep", _sys("You write part of a technical, evidence-based research report in markdown. "
                                        "Cite cards as [arxiv_id]. Use only the cards and digest given. No generic overview prose. "
                                        "Separate measured results from author hypotheses. Never call prediction causal understanding.\n"
                                        "RESEARCH STANDARDS:\n" + std),
                           ctx + "\n\nWRITE THESE SECTIONS (a '## ' heading each):\n" + "\n".join(f"- {n}" for n in names),
                           max_tokens=1600, temperature=0.4, timeout=420)
        except llm.LLMError as e:
            txt = f"\n_(section group failed: {str(e)[:120]})_\n"
        parts.append(_strip_unknown_cites(m, txt))
    out = "\n\n".join(parts)
    write(m.p("tracks", tid, "report.md"), out)
    m.log("report", track=tid, cards=len(ranked))
    return out


def _strip_unknown_cites(m: Mission, txt: str) -> str:
    keep = []
    for para in txt.split("\n"):
        ids = re.findall(r"\[(\d{4}\.\d{4,5})(?:v\d+)?\]", para)
        if ids and not all(m.card(i) for i in ids):
            bad = [i for i in ids if not m.card(i)]
            para = re.sub(r"\[(" + "|".join(map(re.escape, bad)) + r")(?:v\d+)?\]", "[unverified citation removed]", para)
        keep.append(para)
    return "\n".join(keep)


def write_synthesis(m: Mission, final: bool = False) -> str:
    plan = m.plan
    ctx = []
    for t in plan["tracks"]:
        try:
            rep = open(m.p("tracks", t["id"], "report.md"), encoding="utf-8").read()
            summ = rep.split("##", 2)[1][:2200] if "##" in rep else rep[:2200]
        except OSError:
            summ = "(no report yet)"
        dig = ""
        try:
            dig = open(m.p("tracks", t["id"], "digest.md"), encoding="utf-8").read()[:1800]
        except OSError:
            pass
        ctx.append(f"=== {t['id']} {t['name']}\nREPORT SUMMARY:\n{summ}\nDIGEST:\n{dig}")
    contested = []
    for t in plan["tracks"]:
        contested += [f"{c['id']}: {c['text'][:200]} (for {c.get('for')}, against {c.get('against')})"
                      for c in m.claims(t["id"]).values() if c.get("status") == "contested"]
    base = "\n\n".join(ctx)[:30000] + ("\n\nCONTESTED CLAIMS:\n" + "\n".join(contested[:20]) if contested else "")
    qs = plan.get("synthesis_questions", [])
    must = plan.get("synthesis_must_present", [])
    groups = [qs[i:i + 3] for i in range(0, len(qs), 3)] + ([must[:4], must[4:]] if must else [])
    out = [f"# Synthesis ({'final' if final else 'draft'}, {now()})\n\n_Built from the track reports and ledgers. "
           "Cite cards as [id]; claims as t#.c#._\n"]
    for g in [g for g in groups if g]:
        try:
            txt = llm.chat("deep", _sys("You are the synthesis agent. Answer from the evidence in the track reports and ledgers only, "
                                        "cite [arxiv_id] and claim ids, say where evidence is missing or contested. "
                                        "Do not force a JEPA answer if the evidence favours another design."),
                           base + "\n\nANSWER (a '## ' heading each):\n" + "\n".join(f"- {x}" for x in g),
                           max_tokens=1600, temperature=0.4, timeout=420)
        except llm.LLMError as e:
            txt = f"_(failed: {str(e)[:100]})_"
        out.append(_strip_unknown_cites(m, txt))
    s = "\n\n".join(out)
    write(m.p("synthesis.md"), s)
    m.log("synthesis", final=final)
    return s
