"""Continuous research: a long run is cut into waves, and between waves the team writes, forgets and re-plans.

A wave is one stretch of ordinary work (scout, read, consolidate). When it ends, the deep model, in order:

1. writes the wave's chapter (waves/wNN.md): what it found, what changed in what the team believes, dead ends;
2. compacts the state of knowledge (knowledge.md, under ~900 words): answer so far, established, contested,
   open questions, dead ends, next directions. This is the only history the next wave carries;
3. tends long-term memory (longmem.py): lessons, owner preferences, facts and sources worth keeping after the run;
4. re-plans: keeps, refocuses or retires each track, may open new ones, gives fresh questions and searches.

Then the living report (REPORT.md: where we stand on top, the wave chapters below, the final synthesis at the
end) is rebuilt, the tracks lose their "saturated" flags, and every agent starts the next wave from files only:
the compact knowledge, its track digest, the new questions. Nothing else from the last wave reaches a prompt.
That is the context reset: the run can go on for 12 hours while each prompt stays the same size.

Instant runs (one short pass) have no waves; they still get a REPORT.md and one gardening pass at the end.
"""
from __future__ import annotations

import calendar
import glob
import os
import re
import time

from . import live, llm, longmem
from .agents import ARR, ENUM, OBJ, STR, _card_brief, _strip_unknown_cites, _sys, rank_cards, render_digest
from .memory import Mission, append, now, read_json, read_jsonl, write, write_json

MAX_ACTIVE_TRACKS = 6
MAX_QUESTIONS = 16
KNOWLEDGE_MAX = 7000


def today() -> str:
    """A date line for prompts: without it the model dates findings by its training cut-off ("as of late 2025")."""
    return "TODAY: " + time.strftime("%Y-%m-%d", time.gmtime()) + ". Papers dated after your training are real: trust the cards, not memory.\n"


def epoch(ts) -> float:
    try:
        return float(calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return 0.0


def hhmm(t: float) -> str:
    return time.strftime("%H:%M", time.gmtime(t))


def auto_wave_minutes(hours: float) -> float:
    """Wave length for a run of this many hours: long enough to read ~15-30 papers, short enough to re-plan often."""
    if hours <= 0.75:
        return 0.0
    return 20.0 if hours <= 1.5 else 30.0 if hours <= 3 else 45.0 if hours <= 6 else 60.0


# ---------------------------------------------------------------- state (state/waves.json)


def load(m: Mission) -> dict:
    w = read_json(m.p("state", "waves.json"), None)
    return w if isinstance(w, dict) and w.get("n") else {}


def start(m: Mission, wave_s: float, until: float, end_margin: float) -> dict:
    """The wave state of a mission: the one on disk (a restarted daemon goes on where it was) or wave 1."""
    w = load(m)
    if not w:
        t = time.time()
        w = {"n": 1, "wave_s": wave_s, "started": t, "ends": min(t + wave_s, until - end_margin), "history": []}
        save(m, w)
    w["wave_s"] = wave_s
    return w


def save(m: Mission, w: dict) -> None:
    write_json(m.p("state", "waves.json"), w)


def cards_since(m: Mission, since: float) -> list[dict]:
    seen, out = set(), []
    for e in read_jsonl(m.p("state", "events.jsonl")):
        if e.get("kind") == "carded" and epoch(e.get("ts")) >= since and e.get("id") not in seen:
            seen.add(e.get("id"))
            c = m.card(e["id"])
            if c:
                out.append(c)
    return out


def claim_changes(m: Mission, since: float, limit: int = 40) -> list[str]:
    out = []
    for tid in m.track_ids():
        for c in read_jsonl(m.p("tracks", tid, "claims.jsonl")):
            if epoch(c.get("ts")) >= since:
                out.append(f"- {c.get('id')} [{c.get('status')}] {str(c.get('text', ''))[:200]} "
                           f"(for {', '.join(c.get('for', [])[:4])}{'; against ' + ', '.join(c.get('against', [])[:4]) if c.get('against') else ''})")
    return out[-limit:]


def dead_searches(m: Mission, since: float, limit: int = 12) -> list[str]:
    out = []
    for tid in m.track_ids():
        for q in read_jsonl(m.p("tracks", tid, "queries.jsonl")):
            if epoch(q.get("ts")) >= since and not q.get("n_rel2"):
                out.append(f"\"{q.get('text')}\" via {q.get('via')} ({tid})")
    return out[-limit:]


def _read(path: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()[:limit]
    except OSError:
        return ""


def question_of(m: Mission, limit: int = 1500) -> str:
    brief = _read(m.p("brief.md"), 20000)
    mm = re.search(r"^## The question\s*\n(.*?)(?=^## |\Z)", brief, re.S | re.M)
    return (mm.group(1).strip() if mm else m.plan.get("one_line", ""))[:limit]


def knowledge(m: Mission, limit: int = KNOWLEDGE_MAX) -> str:
    return _read(m.p("knowledge.md"), limit)


def knowledge_excerpt(m: Mission, max_chars: int = 1400) -> str:
    """The parts of knowledge.md every agent prompt carries: the answer so far, the open questions, the dead ends."""
    k = knowledge(m)
    if not k:
        return ""
    keep = []
    for title, body in re.findall(r"^## +(.+?)\s*\n(.*?)(?=^## |\Z)", k, re.S | re.M):
        if re.match(r"(answer|open|dead)", title.strip(), re.I):
            keep.append(f"{title.strip().upper()}:\n{body.strip()}")
    return "\n".join(keep)[:max_chars]


# ---------------------------------------------------------------- 1. the wave's chapter

HEADLINE = re.compile(r"^\s*\**HEADLINE\**\s*:\s*(.+)$", re.M | re.I)


def chapter(m: Mission, w: dict, ended: float) -> tuple[str, str, int]:
    """(headline, chapter markdown, cards in the wave). Written to waves/wNN.md."""
    n, started = int(w["n"]), float(w["started"])
    cards = cards_since(m, started)
    changes = claim_changes(m, started)
    dead = dead_searches(m, started)
    head = f"## Wave {n} · {hhmm(started)}–{hhmm(ended)} UTC · {len(cards)} papers"
    if not cards and not changes:
        headline = "No new papers this wave" + (": the searches came back empty" if dead else "")
        md = (f"{head}\n\n**{headline}.**\n\n### Dead ends\n\n" + ("\n".join(f"- {d}" for d in dead) or "- none recorded") +
              "\n\n### What the next wave should do\n\n- Change direction: new questions and searches (see the plan).\n")
    else:
        live.begin(step="wave chapter", n=n)
        ranked = {c["id"]: i for i, c in enumerate(sum((rank_cards(m, t) for t in m.track_ids()), []))}
        cards.sort(key=lambda c: ranked.get(c["id"], 999))
        user = (today() + f"MISSION: {m.plan.get('one_line', '')}\nTHE QUESTION OF THIS RUN:\n{question_of(m)}\n\n"
                f"WHAT THE TEAM KNEW BEFORE THIS WAVE:\n{knowledge(m, 3500) or '(nothing yet: this is the first wave)'}\n\n"
                f"PAPERS CARDED IN WAVE {n} (cite as [id]):\n" + "\n".join(_card_brief(c, 2) for c in cards[:24]) +
                "\n\nCHANGES TO THE CLAIMS LEDGERS IN THIS WAVE:\n" + ("\n".join(changes) or "(none)") +
                "\n\nSEARCHES THIS WAVE THAT FOUND NOTHING USEFUL:\n" + ("\n".join(f"- {d}" for d in dead) or "(none)") +
                "\n\nWrite the chapter of the living report for this wave. First line exactly:\n"
                "HEADLINE: <one sentence: the most important thing this wave found, plain words, it goes to the owner's phone>\n"
                "Then these sections, a '### ' heading each: What we found; What changed in what we believe; Dead ends; "
                "What the next wave should do.\nRules: cite [arxiv_id] for every finding, only papers listed above; numbers only "
                "from the cards; separate measured results from author hypotheses; if the wave found little, say so plainly. "
                "Under 600 words.")
        try:
            txt = llm.chat("deep", _sys("You write one chapter of a living research report: what the team found in the "
                                        "last wave of work. Markdown." + (("\nTHE OWNER'S RESEARCH TASTE (write for this person):\n"
                                                                            + m.taste) if m.taste else "")),
                           user, max_tokens=1400, temperature=0.4, timeout=420)
        except llm.LLMError as e:
            txt = (f"HEADLINE: {len(cards)} papers read this wave (the chapter could not be written: {str(e)[:80]})\n\n"
                   "### What we found\n\n" + "\n".join(f"- [{c['id']}] {c.get('title', '')[:100]}: {c.get('one_line', '')[:200]}" for c in cards[:15]))
        hm = HEADLINE.search(txt)
        headline = (hm.group(1).strip().strip("*") if hm else f"{len(cards)} papers read in wave {n}")[:240]
        body = HEADLINE.sub("", txt, count=1).strip()
        body = re.sub(r"^#{1,2} (?!#)", "### ", body, flags=re.M)  # the chapter's own headings stay below its '## Wave' line
        md = f"{head}\n\n**{headline}**\n\n{_strip_unknown_cites(m, body)}\n"
    write(m.p("waves", f"w{n:02d}.md"), md)
    return headline, md, len(cards)


# ---------------------------------------------------------------- 2. compaction: the state of knowledge

KNOWLEDGE_SECTIONS = ("Answer so far", "Established", "Contested", "Open questions", "Dead ends", "Next directions")


def compact(m: Mission, chapter_md: str, n: int) -> str:
    live.begin(step="compacting knowledge", n=n)
    digs = []
    for t in m.plan.get("tracks", []):
        if t.get("retired"):
            continue
        d = _read(m.p("tracks", t["id"], "digest.md"), 900)
        if d:
            digs.append(f"=== {t['id']} {t['name']}\n{d}")
    contested = []
    for tid in m.track_ids():
        contested += [f"- {c['id']}: {str(c.get('text', ''))[:180]}" for c in m.claims(tid).values() if c.get("status") == "contested"]
    user = (today() + f"MISSION: {m.plan.get('one_line', '')}\nTHE QUESTION OF THIS RUN:\n{question_of(m, 1000)}\n\n"
            f"OLD STATE OF KNOWLEDGE:\n{knowledge(m, 5000) or '(none yet)'}\n\nNEW CHAPTER (wave {n}):\n{chapter_md[:5000]}\n\n"
            "TRACK DIGESTS (the claims ledgers):\n" + "\n\n".join(digs)[:6000] +
            ("\n\nCONTESTED CLAIMS:\n" + "\n".join(contested[:15]) if contested else "") +
            "\n\nRewrite the state of knowledge with exactly these sections, a '## ' heading each: " + "; ".join(KNOWLEDGE_SECTIONS) +
            ". Answer so far: 2-5 sentences, the current best answer to the question and how strong the evidence is. "
            "Established: bullets, each cites [arxiv_id] or claim ids. Open questions: numbered, the most important first. "
            "Dead ends: searches and directions that gave nothing, so nobody repeats them. Keep what still holds from the old state, "
            "merge the new chapter in, drop what was retired or disproved. Under 900 words.")
    try:
        txt = llm.chat("deep", _sys("You keep the team's state of knowledge: the short document every agent reads at the start "
                                    "of every step. You rewrite it after each wave of work. Markdown, under 900 words."),
                       user, max_tokens=1600, temperature=0.3, timeout=420)
        txt = re.sub(r"^# .*\n", "", txt.strip())
        if "## " not in txt:
            raise llm.LLMError("no sections")
    except llm.LLMError:
        old = knowledge(m)
        txt = (re.sub(r"^# .*\n", "", old).strip() + "\n\n" if old else "") + f"## Latest wave\n\n{chapter_md[:2500]}"
    out = f"# State of knowledge after wave {n} ({now()})\n\n{_strip_unknown_cites(m, txt.strip())}\n"
    write(m.p("knowledge.md"), out[:KNOWLEDGE_MAX])
    return out


# ---------------------------------------------------------------- 3. long-term memory

MEMORY_OPS = OBJ(ops=ARR(OBJ(op=ENUM("add", "update", "delete"), name=STR, type=ENUM(*longmem.TYPES), description=STR, body=STR)))


def garden(m: Mission, chapter_md: str, home: str) -> list[str]:
    """Add, update or delete a few long-term memories from what this wave taught. Returns what was done."""
    live.begin(step="memory", n=0)
    idx = longmem.index_text(home, 3500)
    rel = longmem.recall(home, chapter_md[:3000] + " " + m.plan.get("one_line", ""), k=6)
    cmds = [str(c.get("text") or c.get("topic") or "") for c in read_jsonl(m.p("state", "inbox.jsonl"))
            if c.get("cmd") in ("instruct", "focus")][-6:]
    playbook = _read(m.p("memory", "playbook.md"), 1800)
    user = (today() + "CURRENT LONG-TERM MEMORY (index):\n" + (idx or "(empty)") +
            ("\n\nMEMORIES CLOSEST TO THIS WAVE (bodies):\n" + longmem.block(rel, 2200, 380) if rel else "") +
            (f"\n\nTHE OWNER'S RESEARCH TASTE:\n{m.taste[:800]}" if m.taste else "") +
            ("\n\nWHAT THE OWNER TOLD THE AGENTS DURING THIS RUN:\n" + "\n".join(f"- {c}" for c in cmds if c) if cmds else "") +
            f"\n\nTHE RUN: {m.plan.get('one_line', '')}\n\nWHAT THIS WAVE FOUND:\n{chapter_md[:4500]}\n\nSEARCH PLAYBOOK:\n{playbook}" +
            "\n\nGive at most 5 ops. Each op: op (add, update or delete), name (kebab-case, 2-6 words), type (owner = what the owner "
            "wants or distrusts; lesson = how to work: a search, source or reading habit that worked or failed, and why; fact = a "
            "durable finding, citing arXiv ids and dated with today's date (\"as of YYYY-MM\"); source = a key paper, repo, benchmark or dataset worth coming back to), description "
            "(one line that says when a later agent should read it), body (the memory itself, under 120 words).\n"
            "Keep only what will still help a later run, maybe on another question. Update an existing memory instead of adding a "
            "near duplicate. Delete one this wave proved wrong. Never save counts, times or the status of this run. "
            "An empty list is a fine answer.")
    try:
        r = llm.chat_json("deep", "You tend the team's LONG-TERM memory. It outlives this run: later runs, on other questions too, "
                                  "start from it. You save little and only what lasts. Answer in JSON.",
                          user, MEMORY_OPS, max_tokens=1400, temperature=0.3, timeout=300)
    except llm.LLMError as e:
        return [f"memory not updated: {str(e)[:100]}"]
    done = longmem.apply_ops(home, r.get("ops") or [], source=m.id, max_ops=5)
    m.log("memory", ops=done)
    return done


def refresh_memory_block(m: Mission, home: str) -> str:
    """state/longmem.txt: the long-term memories this mission's prompts carry (fixed for a whole wave, so the prompt
    prefix stays the same and the model server's prefix cache keeps hitting)."""
    q = " ".join([m.plan.get("one_line", ""), question_of(m, 800), knowledge_excerpt(m, 800)])
    owner = [x for x in longmem.load_all(home) if x["type"] == "owner"][:2]
    rest = [x for x in longmem.recall(home, q, k=8, types=("lesson", "source", "fact")) if x["name"] not in {o["name"] for o in owner}]
    picked = owner + rest[:5]
    text = longmem.block(picked, 1800, 360)
    write(m.p("state", "longmem.txt"), text + ("\n" if text else ""))
    if picked:
        longmem.bump(home, [x["name"] for x in picked])
    return text


# ---------------------------------------------------------------- 4. the next wave's plan

REPLAN_SCHEMA = OBJ(why=STR, tracks=ARR(OBJ(id=STR, action=ENUM("keep", "refocus", "retire", "new"), name=STR, one_line=STR,
                                             questions=ARR(STR), starter_queries=ARR(STR))))


def replan(m: Mission, n: int, wave_min: float, track_state: dict) -> list[str]:
    """Rewrite plan.json for wave n+1. Returns one line per change. On failure the plan stays and only the saturation resets."""
    live.begin(step="planning next wave", n=n + 1)
    plan = m.plan
    rows = []
    for t in plan.get("tracks", []):
        if t.get("retired"):
            continue
        tid = t["id"]
        led = m.claims(tid)
        cover = {q["id"]: 0 for q in t.get("questions", [])}
        for c in led.values():
            if c.get("q") in cover and c.get("status") != "retired":
                cover[c["q"]] += 1
        zero = [q["text"][:90] for q in t.get("questions", []) if not cover.get(q["id"])][:4]
        rows.append(f"- {tid} {t['name']}: {t.get('one_line', '')[:160]} | cards {m.count('carded', tid)}, queued {m.count('queued', tid)}"
                    f"{', SATURATED (searches stopped finding papers)' if track_state.get(tid, {}).get('saturated') else ''}"
                    f" | unanswered: {'; '.join(zero) or 'none'} | recent questions: "
                    f"{'; '.join(q['text'][:70] for q in t.get('questions', [])[-3:])}")
    user = (today() + f"MISSION: {plan.get('one_line', '')}\nTHE QUESTION OF THIS RUN:\n{question_of(m, 1000)}\n\n"
            f"STATE OF KNOWLEDGE AFTER WAVE {n}:\n{knowledge(m, 5000)}\n\nTRACKS NOW:\n" + "\n".join(rows) +
            (f"\n\nLONG-TERM MEMORY:\n{_read(m.p('state', 'longmem.txt'), 1200)}" if os.path.exists(m.p("state", "longmem.txt")) else "") +
            f"\n\nThe next wave lasts about {int(wave_min)} minutes. For each track above choose: keep (it still finds useful papers), "
            "refocus (same area, new questions taken from the open questions), or retire (its questions are answered, or it is "
            f"saturated and the area is done). You may open up to 2 new tracks (id \"new\") for open questions no track covers; at most "
            f"{MAX_ACTIVE_TRACKS} active tracks in total. For each track give id, action, name, one_line, questions (2-6 NEW questions "
            "for the next wave, none for retire) and starter_queries (4-8 arXiv searches of 2-4 keywords each, different from the "
            "dead ends; none for retire). why: one sentence on the plan.")
    try:
        r = llm.chat_json("deep", _sys("You plan the next wave of a continuous literature research run. Go where the open "
                                       "questions are, not where the team has already been. Answer in JSON."),
                          user, REPLAN_SCHEMA, max_tokens=2200, temperature=0.4, timeout=420)
    except llm.LLMError as e:
        return [f"plan kept ({str(e)[:80]})"]
    tracks = plan.get("tracks", [])
    by_id = {t["id"]: t for t in tracks}
    nums = [int(t["id"][1:]) for t in tracks if re.fullmatch(r"t\d+", t["id"])]
    changes = []
    active = [t for t in tracks if not t.get("retired")]
    wanted_retire = [x for x in r.get("tracks") or [] if x.get("action") == "retire" and x.get("id") in by_id]
    if len(wanted_retire) >= len(active):  # never retire everything: the busiest track stays
        keep = max(active, key=lambda t: m.count("queued", t["id"]) + m.count("carded", t["id"]))["id"] if active else ""
        r["tracks"] = [x if x.get("id") != keep else {**x, "action": "refocus"} for x in r.get("tracks") or []]
    for x in r.get("tracks") or []:
        act, tid = x.get("action"), str(x.get("id") or "")
        qs = [" ".join(str(q).split())[:300] for q in x.get("questions") or [] if str(q).strip()][:6]
        sq = [" ".join(str(q).split())[:80] for q in x.get("starter_queries") or [] if str(q).strip()][:8]
        if tid in by_id and not by_id[tid].get("retired"):
            t = by_id[tid]
            if act == "retire":
                young = n - int(t.get("since_wave") or 1) < 1  # it has had one wave or less
                if young and not track_state.get(tid, {}).get("saturated"):
                    act = "refocus"  # a young track that found little gets new questions, not the axe (seen on a real run)
                else:
                    t["retired"] = n
                    changes.append(f"{tid} retired")
                    continue
            if act == "refocus" and x.get("one_line"):
                t["one_line"] = " ".join(str(x["one_line"]).split())[:300]
            base = len(t.get("questions", []))
            t["questions"] = (t.get("questions", []) + [{"id": f"{tid}.q{base + j}", "text": q, "wave": n + 1}
                                                        for j, q in enumerate(qs, 1)])[-MAX_QUESTIONS:]
            if sq:
                t["starter_queries"] = sq
            changes.append(f"{tid} {act}: +{len(qs)} questions")
        elif act == "new" and qs:
            if sum(1 for t in tracks if not t.get("retired")) >= MAX_ACTIVE_TRACKS:
                continue
            k = max(nums, default=0) + 1
            nums.append(k)
            tid = f"t{k}"
            t = {"id": tid, "name": " ".join(str(x.get("name") or tid).split())[:80],
                 "one_line": " ".join(str(x.get("one_line") or "").split())[:300], "weight": 1.0, "seeds": [], "since_wave": n + 1,
                 "questions": [{"id": f"{tid}.q{j}", "text": q, "wave": n + 1} for j, q in enumerate(qs, 1)],
                 "starter_queries": sq, "deliverable": "Answer its questions from the papers, for the living report."}
            tracks.append(t)
            write(m.p("tracks", tid, "assignment.md"), f"# {t['name']}\n\nOpened in wave {n + 1}. {t['one_line']}\n")
            render_digest(m, tid)
            changes.append(f"{tid} new: {t['name']}")
    plan["tracks"] = tracks
    plan["wave"] = n + 1
    plan["replanned"] = now()
    write_json(m.p("plan.json"), plan)
    m.log("replanned", wave=n + 1, why=str(r.get("why") or "")[:300], changes=changes)
    return changes


# ---------------------------------------------------------------- the living report (REPORT.md)


def _demote(md: str, by: int = 1) -> str:
    return re.sub(r"^(#{1,5}) ", lambda mm: "#" * min(len(mm.group(1)) + by, 6) + " ", md, flags=re.M)


def render_report(m: Mission, final: bool = False) -> str:
    """REPORT.md: the one document the owner reads. Rebuilt from its parts, so every wave appends a chapter."""
    brief = _read(m.p("brief.md"), 3000)
    tm = re.search(r"^title:\s*(.+)$", brief, re.M) or re.search(r"^#\s+(.+)$", brief, re.M)
    title = tm.group(1).strip() if tm else m.id
    w = load(m)
    chapters = sorted(glob.glob(m.p("waves", "w*.md")))
    cards = sum(1 for r in m.reg.values() if r.get("state") == "carded")
    L = [f"# {title}", "",
         f"_Living report · {'continuous research, ' + str(len(chapters)) + (' wave' if len(chapters) == 1 else ' waves') if chapters else 'one pass'}"
         f" · {cards} papers read · "
         f"updated {now()}_", "", f"**The question.** {question_of(m, 1200)}", ""]
    syn = _read(m.p("synthesis.md"), 60000)
    if final and syn:
        L += ["## The answer", "", _demote(re.sub(r"^# .*\n", "", syn.strip()), 1).strip(), ""]
    k = knowledge(m)
    if k:
        L += ["## Where we stand" + (f" (after wave {len(chapters)})" if chapters else ""), "",
              _demote(re.sub(r"^# .*\n", "", k.strip()), 1).strip(), ""]
    if chapters:
        L += ["## Wave log", "", "_One chapter per wave, oldest first. Each wave started from the state of knowledge above it._", ""]
        for p in chapters:
            L += [_demote(_read(p, 20000).strip(), 1), ""]
    if syn and not final:
        L += ["## Synthesis draft", "", _demote(re.sub(r"^# .*\n", "", syn.strip()), 1).strip(), ""]
    reps = []
    for t in m.plan.get("tracks", []):
        if os.path.exists(m.p("tracks", t["id"], "report.md")):
            reps.append(f"- {t['id']} {t['name']}{' (retired in wave ' + str(t['retired']) + ')' if t.get('retired') else ''}: "
                        f"tracks/{t['id']}/report.md")
    if reps:
        L += ["## Track reports", ""] + reps + [""]
    out = "\n".join(L).rstrip() + "\n"
    write(m.p("REPORT.md"), out)
    return out


def headline_of_synthesis(m: Mission, limit: int = 300) -> str:
    """The first real sentence of the synthesis's first section: what the notification of a finished run says."""
    syn = _read(m.p("synthesis.md"), 20000)
    for para in re.split(r"\n\s*\n", re.sub(r"^#.*$|^_.*_$", "", syn, flags=re.M)):
        para = " ".join(para.split()).lstrip("-* ")
        if len(para) > 40:
            first = re.split(r"(?<=[.!?])\s", para, maxsplit=1)[0]
            return first[:limit]
    return "The run is finished: the report is ready."


def context_for_synthesis(m: Mission, limit: int = 9000) -> str:
    """What a continuous run hands the final synthesis on top of the track reports: the compact knowledge and the
    headline of every wave (the chapters themselves stay in REPORT.md)."""
    w = load(m)
    k = knowledge(m, 6000)
    heads = [f"- wave {h['n']} ({h.get('cards', 0)} papers): {h.get('headline', '')}" for h in (w.get("history") or [])]
    out = (f"STATE OF KNOWLEDGE AFTER {len(heads)} WAVES:\n{k}\n" if k else "") + ("\nWAVE HEADLINES:\n" + "\n".join(heads) if heads else "")
    return out[:limit]


def turnover(m: Mission, w: dict, until: float, end_margin: float, home: str, track_state: dict, final: bool = False) -> dict:
    """Close wave w (chapter, knowledge, memory) and, unless final, open the next one (re-plan, reset). Returns the new state."""
    ended = time.time()
    headline, md, n_cards = chapter(m, w, ended)
    compact(m, md, int(w["n"]))
    mem = garden(m, md, home)
    hist = list(w.get("history") or []) + [{"n": int(w["n"]), "started": w["started"], "ended": ended, "cards": n_cards,
                                            "headline": headline, "memory": mem[:5]}]
    nxt = dict(w, history=hist)
    if not final:
        changes = replan(m, int(w["n"]), float(w["wave_s"]) / 60, track_state)
        refresh_memory_block(m, home)
        t = time.time()
        nxt.update(n=int(w["n"]) + 1, started=t, ends=min(t + float(w["wave_s"]), until - end_margin), plan_changes=changes)
    else:
        nxt.update(closed=True)
    save(m, nxt)
    render_report(m, final=False)
    m.log("wave", n=int(w["n"]), cards=n_cards, headline=headline, final=final, next=None if final else nxt["n"])
    append(m.p("state", "notify.jsonl"), {"kind": "wave", "n": int(w["n"]), "cards": n_cards, "headline": headline, "final": final})
    return nxt
