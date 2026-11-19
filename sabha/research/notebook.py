"""The lab notebook of a run, and the refined report it becomes at the end.

While the run goes, the team keeps writing:

- every paper card lands in the notebook as an entry (state/notebook.jsonl, kind "paper"), with its method, numbers
  and quotes: nothing a card holds is lost to a summary;
- after every consolidation the deep model writes a short lab note (kind "note"): what the new papers add to the
  question, how they connect, which number surprised it, one idea to try;
- every wave closes with its headline (kind "wave").

NOTEBOOK.md is rebuilt from those entries after each one, and the living REPORT.md shows the newest notes and every
paper read so far. When the run ends, refine() turns it all into the finished report (final.md): the answer, the key
findings with their numbers, a comparison table, the experiments worth running, the open questions, and a dossier of
every paper. Each part is its own short call, so a small model writes it well; the tables and the dossier are built
from the cards themselves and never pass through a model.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import live, llm
from .memory import Mission, append, now, read_jsonl, write

NOTE_WORDS = 220
RENDER_LOCK = threading.Lock()  # six readers card papers at once: one NOTEBOOK.md / REPORT.md writer at a time


def entries(m: Mission) -> list[dict]:
    return read_jsonl(m.p("state", "notebook.jsonl"))


def _wave_of(m: Mission) -> int:
    try:
        with open(m.p("state", "waves.json"), encoding="utf-8") as f:
            return int(json.load(f).get("n") or 1)
    except (OSError, ValueError, AttributeError):
        return 1


# ---------------------------------------------------------------- unwrapping model output


def _md_of(v, level: int = 2) -> str:
    """A JSON value a model answered with, as markdown: keys become headings, lists bullets, markdown strings stay."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        rows, headed = [], False
        for x in v:
            if isinstance(x, dict) and x:
                keys = list(x)
                tk = next((k for k in keys if k.lower() in TITLE_KEYS), None)
                if tk is None and isinstance(x[keys[0]], str) and len(x[keys[0]]) < 120 and len(keys) > 1:
                    tk = keys[0]
                if tk:  # an experiment, a finding: its own heading, the other fields as labelled bullets
                    headed = True
                    rest = [f"- **{k.replace('_', ' ').strip().capitalize()}**: {_inline(y, level)}"
                            for k, y in x.items() if k != tk and _md_of(y, level + 1)]
                    rows.append(f"{'#' * min(level, 6)} {' '.join(str(x[tk]).split())}\n\n" + "\n".join(rest))
                else:
                    bits = [str(y).strip() for y in x.values() if isinstance(y, (str, int, float)) and str(y).strip()]
                    rows.append("- " + " · ".join(bits))
            else:
                rows.append("- " + " ".join(_md_of(x, level + 1).split()))
        return ("\n\n" if headed else "\n").join(rows)
    if isinstance(v, dict):
        if len(v) == 1 and isinstance(next(iter(v.values())), (list, dict)):
            return _md_of(next(iter(v.values())), level)  # {"experiments": [...]}: the list is the content
        parts = []
        for k, x in v.items():
            body = _md_of(x, level + 1)
            if not body:
                continue
            if isinstance(x, str) and re.match(r"\s*#{1,6} ", x):
                parts.append(body)  # it brings its own headings
            elif k.lower() in ("answer", "text", "body", "markdown", "content", "report"):
                parts.append(body)
            else:
                parts.append(f"{'#' * min(level, 6)} {k.replace('_', ' ').strip().capitalize()}\n\n{body}")
        return "\n\n".join(parts)
    return "" if v is None else str(v)


TITLE_KEYS = ("name", "title", "experiment", "heading", "finding", "headline")


def _inline(y, level: int) -> str:
    """One field's value on one line: a list of ids becomes [id], [id]."""
    if isinstance(y, list) and all(isinstance(z, (str, int, float)) for z in y):
        return ", ".join(f"[{z}]" if re.fullmatch(r"\d{4}\.\d{4,5}", str(z).strip()) else str(z).strip() for z in y)
    return " ".join(_md_of(y, level + 1).split())
FENCE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\n(.*?)(?:```|\Z)", re.S)
KEYVAL = re.compile(r'"(\w+)"\s*:\s*(\[\s*"|")')


def _loads(b: str):
    """json.loads, or for JSON a model cut off at its token limit (an unclosed string, seen on a real run): the string
    and string-list fields it did write, in order. None when it is not JSON at all."""
    b = b.strip()
    if not b.startswith(("{", "[")):
        return None
    try:
        return json.loads(b, strict=False)
    except ValueError:
        pass
    out, pos = {}, 0
    while True:
        mm = KEYVAL.search(b, pos)
        if not mm:
            break
        vals, i = [], mm.end()
        while True:
            j, esc = i, False
            while j < len(b) and (esc or b[j] != '"'):
                esc = (b[j] == "\\") and not esc
                j += 1
            try:
                vals.append(json.loads('"' + b[i:j].rstrip("\\") + '"', strict=False))
            except ValueError:
                vals.append(b[i:j])
            nxt = re.match(r'"\s*,\s*"', b[j:j + 40]) if mm.group(2) != '"' else None
            if not nxt:
                break
            i = j + nxt.end()
        out[mm.group(1)] = vals[0] if mm.group(2) == '"' else vals
        pos = max(j, mm.end())
    return out or None


def unjson(txt: str, level: int = 2) -> str:
    """Small models sometimes answer a markdown request with JSON, fenced or bare (seen on a real run: the whole
    synthesis came back as two ```json blocks). Turn every JSON object or list in the text into markdown; code
    fences that are not JSON stay as they are."""
    s = (txt or "").strip()
    if s.startswith(("{", "[")):
        v = _loads(s)
        if isinstance(v, (dict, list)):
            return _md_of(v, level)

    def sub(mm):
        v = _loads(mm.group(1))
        return "\n" + _md_of(v, level) + "\n" if isinstance(v, (dict, list)) else mm.group(0)
    return FENCE.sub(sub, s)


# ---------------------------------------------------------------- writing as the run goes


def add_paper(m: Mission, aid: str) -> None:
    c = m.card(aid) or {}
    append(m.p("state", "notebook.jsonl"), {"kind": "paper", "id": aid, "track": (c.get("tracks") or [""])[0],
                                            "wave": _wave_of(m), "title": c.get("title", "")[:200]})
    render(m)


def add_wave(m: Mission, n: int, headline: str, cards: int) -> None:
    append(m.p("state", "notebook.jsonl"), {"kind": "wave", "n": n, "wave": n, "headline": headline[:300], "cards": cards})
    render(m)


def _brief(c: dict) -> str:
    res = "; ".join(f"{r.get('metric')} = {r.get('value')} ({str(r.get('setting', ''))[:50]})" for r in c.get("results", [])[:4]
                    if str(r.get("value") or "").strip())
    lim = "; ".join(str(x.get("text", ""))[:110] for x in c.get("limitations", [])[:2])
    return (f"[{c['id']}] {c.get('title', '')[:110]} | {c.get('one_line', '')[:200]} | method: "
            f"{c.get('training_method', {}).get('text', '')[:180]} | architecture: {c.get('architecture', {}).get('text', '')[:140]}"
            f" | results: {res or 'none measured'} | limits: {lim or 'none stated'} | code: {_repo(c) or 'none'}")


def think(m: Mission, tid: str, ids: list[str], question: str) -> str:
    """The lab note after a consolidation: the model writes down what it makes of the new papers. Returns the note."""
    cards = [c for c in (m.card(a) for a in ids) if c][:10]
    if not cards:
        return ""
    t = m.track(tid) or {}
    live.begin(step="notebook", track=tid, n=len(cards))
    before = [e for e in entries(m) if e.get("kind") == "note"][-3:]
    user = (f"TODAY: {time.strftime('%Y-%m-%d', time.gmtime())}\nTHE QUESTION OF THIS RUN:\n{question[:1200]}\n\n"
            f"YOUR TRACK: {tid} {t.get('name', '')}: {t.get('one_line', '')[:240]}\n\n"
            + ("YOUR LAST NOTES (do not repeat them):\n" + "\n".join(f"- {e.get('text', '')[:300]}" for e in before) + "\n\n" if before else "")
            + "PAPERS YOU JUST READ (cite as [id]):\n" + "\n".join(_brief(c) for c in cards)
            + f"\n\nWrite one lab-notebook entry, under {NOTE_WORDS} words, plain markdown, no headings. Cover: what these papers "
              "add to the question (with their numbers), how they connect to each other or contradict, what surprised you, and "
              "end with one line that starts 'Idea:' naming a concrete thing to try or check next. Cite [id] for every fact. "
              "Write like a researcher's notebook: direct, specific, first person plural.")
    try:
        txt = llm.chat("deep", "You keep the research team's lab notebook. You write down what the papers just read mean for "
                               "the question, in plain words, with the numbers. Markdown, no headings.",
                       user, max_tokens=600, temperature=0.5, timeout=300)
    except llm.LLMError as e:
        m.log("notebook_fail", track=tid, err=str(e)[:200])
        return ""
    from .agents import _strip_unknown_cites
    txt = _strip_unknown_cites(m, unjson(txt, 4)).strip()
    txt = re.sub(r"^#{1,6} .*\n", "", txt).strip()
    if len(txt) < 40:
        return ""
    idea = re.search(r"^\**Idea\**\s*:\s*(.+)$", txt, re.M | re.I)
    append(m.p("state", "notebook.jsonl"), {"kind": "note", "track": tid, "wave": _wave_of(m), "papers": [c["id"] for c in cards],
                                            "text": txt[:2400], "idea": idea.group(1).strip()[:300] if idea else ""})
    render(m)
    return txt


# ---------------------------------------------------------------- markdown


def _repo(c: dict) -> str:
    r = c.get("official_repo") or {}
    return (r.get("url") or "") if r.get("status") == "verified" else ""


def _hhmm(ts) -> str:
    try:
        return time.strftime("%H:%M", time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return ""


def best_result(c: dict) -> str:
    for r in c.get("results", []):
        v = str(r.get("value") or "").strip().strip(",;")
        if v and re.search(r"\d", v):
            metric = str(r.get("metric") or "").replace("_", " ").strip()
            head = metric if re.sub(r"[^\d.]", "", v) and re.sub(r"[^\d.]", "", v) in re.sub(r"[^\d.]", " ", metric).split() else f"{metric} {v}"
            setting = str(r.get("setting") or "").replace("_", " ").strip()
            return head.strip() + (f" ({setting[:40]})" if setting else "")
    return ""


def paper_md(c: dict, level: int = 3, full: bool = True) -> str:
    """One paper as a dossier entry: everything the card knows, in reading order."""
    aid = c["id"]
    h = "#" * level
    L = [f"{h} {c.get('title', aid)}", "",
         f"[{aid}] · first submitted {str(c.get('first_submitted') or '?')[:16]} · tracks {', '.join(c.get('tracks', []))}"
         + (f" · [code]({_repo(c)})" if _repo(c) else "") + f" · [arXiv](https://arxiv.org/abs/{aid})", ""]
    if c.get("one_line"):
        L += [f"**In one line.** {c['one_line']}", ""]
    if c.get("why_it_matters"):
        L += [f"**Why it matters here.** {c['why_it_matters']}", ""]
    tm, ar = (c.get("training_method") or {}).get("text"), (c.get("architecture") or {}).get("text")
    if ar:
        L += [f"**Architecture.** {ar}", ""]
    if tm:
        L += [f"**How it is trained.** {tm}", ""]
    if c.get("losses") and full:
        L += ["**Losses.**", ""] + [f"- {x.get('name')}: {x.get('form') or ''}".rstrip(": ") for x in c["losses"][:5]] + [""]
    res = [r for r in c.get("results", []) if str(r.get("value") or "").strip()]
    if res:
        L += ["| Result | Metric | Value | Setting |", "|---|---|---|---|"]
        for r in res[:6]:
            L.append("| " + " | ".join(" ".join(str(x).replace("|", "/").split())[:140] for x in
                                       (r.get("claim", ""), str(r.get("metric", "")).replace("_", " "), r.get("value", ""), r.get("setting", ""))) + " |")
        L.append("")
    quotes = [e for r in res[:3] for e in (r.get("ev") or [])[:1] if e.get("quote")]
    for e in quotes[:2]:
        L += [f"> \"{' '.join(str(e['quote']).split())[:400]}\" ({e.get('sec', '')})", ""]
    if c.get("hypotheses") and full:
        L += ["**The authors believe (not measured).** " + " ".join(f"{x.get('text', '')}" for x in c["hypotheses"][:2]), ""]
    if c.get("limitations"):
        L += ["**Limitations.** " + " ".join(f"{x.get('text', '')}" + (f" ({x.get('by')})" if x.get("by") else "")
                                             for x in c["limitations"][:3]), ""]
    cp = (c.get("compute") or {}).get("text")
    if cp and full:
        L += [f"**Compute.** {cp}" + (f" (to reproduce: {c['compute'].get('repro_cost')})" if c["compute"].get("repro_cost") else ""), ""]
    return "\n".join(L).rstrip() + "\n"


def render(m: Mission) -> str:
    """NOTEBOOK.md: everything the team wrote, in the order it was written, one section per wave."""
    with RENDER_LOCK:
        return _render(m)


def _render(m: Mission) -> str:
    ev = entries(m)
    L = ["# Lab notebook", "", "_Everything the team wrote down during the run, oldest first: each paper as it was read, the "
         "notes the model wrote after each batch, and the close of every wave._", ""]
    wave = None
    for e in ev:
        w = int(e.get("wave") or 1)
        if w != wave and e.get("kind") != "wave":
            wave = w
            L += [f"## Wave {w}", ""]
        k = e.get("kind")
        when = _hhmm(e.get("ts"))
        if k == "paper":
            c = m.card(e.get("id", "")) or {}
            if not c:
                continue
            br = best_result(c)
            L += [f"### {when} · read [{c['id']}] {c.get('title', '')[:140]}", "",
                  c.get("one_line", ""), ""]
            if br:
                L += [f"- **Best number:** {br}"]
            tm = (c.get("training_method") or {}).get("text")
            if tm:
                L += [f"- **Method:** {tm[:300]}"]
            if c.get("limitations"):
                L += [f"- **Limitation:** {c['limitations'][0].get('text', '')[:240]}"]
            L.append("")
        elif k == "note":
            L += [f"### {when} · note · {e.get('track', '')}", "", e.get("text", ""), ""]
        elif k == "wave":
            L += [f"### {when} · wave {e.get('n')} closed ({e.get('cards', 0)} papers)", "", f"**{e.get('headline', '')}**", ""]
            wave = None
    out = "\n".join(L).rstrip() + "\n"
    write(m.p("NOTEBOOK.md"), out)
    return out


def backfill(m: Mission) -> bool:
    """A run from before the notebook existed: build its entries from the event log (each paper when it was carded,
    each wave when it closed). The notes the model never wrote stay missing. True when entries were written."""
    if os.path.exists(m.p("state", "notebook.jsonl")):
        return False
    from .waves import epoch
    hist = []
    try:
        with open(m.p("state", "waves.json"), encoding="utf-8") as f:
            hist = json.load(f).get("history") or []
    except (OSError, ValueError, AttributeError):
        pass

    def wave_at(t: float) -> int:
        for h in hist:
            if t <= float(h.get("ended") or 0):
                return int(h.get("n") or 1)
        return int(hist[-1]["n"]) + 1 if hist else 1
    seen, rows = set(), []
    for e in read_jsonl(m.p("state", "events.jsonl")):
        k = e.get("kind")
        if k == "carded" and e.get("id") not in seen and m.card(e.get("id", "")):
            seen.add(e["id"])
            rows.append({"kind": "paper", "id": e["id"], "track": e.get("track"), "wave": wave_at(epoch(e.get("ts"))), "ts": e.get("ts")})
        elif k == "wave":
            rows.append({"kind": "wave", "n": e.get("n"), "wave": e.get("n"), "headline": str(e.get("headline", ""))[:300],
                         "cards": e.get("cards", 0), "ts": e.get("ts")})
    if not rows:
        return False
    for r in rows:
        append(m.p("state", "notebook.jsonl"), r)
    render(m)
    return True


def latest_notes(m: Mission, k: int = 4) -> list[dict]:
    return [e for e in entries(m) if e.get("kind") == "note"][-k:]


# ---------------------------------------------------------------- the refined report


def _ask(m: Mission, system: str, user: str, max_tokens: int = 1400) -> str:
    from .agents import _prose, _strip_unknown_cites
    try:
        txt = llm.chat("deep", _prose(system + ("\nTHE OWNER'S RESEARCH TASTE (write for this person):\n" + m.taste if m.taste else "")),
                       user, max_tokens=max_tokens, temperature=0.35, timeout=480)
    except llm.LLMError as e:
        return f"_(this part could not be written: {str(e)[:100]})_"
    txt = unjson(txt, 3)
    txt = re.sub(r"^# .*\n", "", txt.strip())
    txt = re.sub(r"^#{1,2} (?!#)", "### ", txt, flags=re.M)  # the part's own headings sit under its '## ' heading
    return _strip_unknown_cites(m, txt).strip()


def _confidence_line(txt: str) -> str:
    """The answer's last line as 'Confidence: medium, because ...' in its own paragraph: a small model runs it into
    the prose, or copies the template's 'high|medium' (both seen on a real run)."""
    mm = re.search(r"\**Confidence\**\s*:\s*\**\s*([A-Za-z|/ ,-]*?)\s*\**\s*(?:,|\.|;|\s-|\bbecause\b)\s*(.*)$", txt, re.S | re.I)
    if not mm:
        return txt
    words = re.findall(r"high|medium|low", mm.group(1).lower())
    level = words[0] if len(words) == 1 else "medium" if words else "low"
    reason = " ".join(mm.group(2).split()).strip().rstrip(".")
    reason = re.sub(r"^because\s+", "", reason, flags=re.I)
    return txt[:mm.start()].rstrip() + f"\n\nConfidence: {level}" + (f", because {reason}." if reason else ".")


def pick_title(txt: str) -> str:
    """The title out of whatever the model wrote: 'Title: X', '### Title' then X, a JSON {"title": X}, or just X."""
    v = _loads(txt or "")
    if isinstance(v, dict):
        for k, x in v.items():
            if k.lower() in TITLE_KEYS and isinstance(x, str):
                return " ".join(x.split()).strip(" .'\"")
    for line in (txt or "").splitlines():
        t = re.sub(r"^\s*(?:#+|[-*>]|\d+[.)])\s*", "", line)
        t = re.sub(r"[*_`\"]", "", t).strip()
        t = re.sub(r"^(?:report\s+)?title\s*:\s*", "", t, flags=re.I).strip(" .'")
        if t and t.lower() not in ("title", "report title"):
            return " ".join(t.split())[:120]
    return ""


def comparison_table(cards: list[dict], k: int = 14) -> str:
    rows = ["| Paper | The idea | Best number | Code |", "|---|---|---|---|"]
    for c in cards[:k]:
        idea = " ".join(str(c.get("one_line", "")).replace("|", "/").split())[:150]
        rows.append(f"| [{c['id']}] {' '.join(c.get('title', '').replace('|', '/').split())[:60]} | {idea} | "
                    f"{best_result(c).replace('|', '/')[:70] or '–'} | {'[repo](' + _repo(c) + ')' if _repo(c) else '–'} |")
    return "\n".join(rows)


def refine(m: Mission, ask=None) -> str:
    """final.md: the finished report, refined from the notebook, the knowledge, the synthesis and the cards. ask(m, system,
    user, max_tokens) writes one part (the model by default; the demo daemon passes its own)."""
    ask = ask or _ask
    from .agents import rank_cards
    from .waves import knowledge, load as load_waves, question_of
    live.begin(step="refining the report", final=True)
    q = question_of(m, 1500)
    k = knowledge(m, 5000)
    syn = re.sub(r"^# .*\n|^_.*_\n", "", unjson(_read(m.p("synthesis.md"), 9000)), flags=re.M).strip()
    notes = [e for e in entries(m) if e.get("kind") == "note"]
    notes_txt = "\n\n".join(f"({e.get('track')}, wave {e.get('wave')}) {e.get('text', '')}" for e in notes[-12:])[:7000]
    ideas = [e.get("idea") for e in notes if e.get("idea")]
    ranked, seen = [], set()
    for t in m.plan.get("tracks", []):
        for c in rank_cards(m, t["id"]):
            if c["id"] not in seen:
                seen.add(c["id"])
                ranked.append(c)
    ranked.sort(key=lambda c: -(c.get("relevance", 2) + (1 if best_result(c) else 0) + (0.5 if _repo(c) else 0)))
    briefs = "\n".join(_brief(c) for c in ranked[:20])
    base = (f"TODAY: {time.strftime('%Y-%m-%d', time.gmtime())}\nTHE QUESTION:\n{q}\n\n"
            f"THE PAPERS THE TEAM READ, BEST FIRST (the ground truth: their titles, methods and numbers; cite as [id]):\n{briefs}\n\n"
            f"LAB NOTEBOOK (the team's notes while reading):\n{notes_txt or '(none)'}\n\n"
            f"EARLIER DRAFTS (may name papers wrongly or repeat themselves; where they disagree with THE PAPERS, the papers win):\n"
            f"STATE OF KNOWLEDGE:\n{k[:3000] or '(none)'}\n\nSYNTHESIS DRAFT:\n{syn[:3000] or '(none)'}\n")
    rules = ("Rules: cite [arxiv_id] for every fact, only papers in THE PAPERS list, and call a paper only by its title there; "
             "numbers only from the papers' results; do not repeat a point you already made; say plainly where evidence is thin "
             "or missing; separate what was measured from what the authors believe. No preamble.")
    short = ask(m, "You write the opening of a finished research report: the answer, for someone who reads only this part.",
                 base + "\nWrite the answer to the question in 5-8 sentences of plain prose (no list, no heading): what the evidence "
                        "says, the two or three papers it rests on, and how sure we can be. Then one last line exactly like "
                        "'Confidence: <one word: high, medium or low>, because <reason>'.\n" + rules, 700)
    short = _confidence_line(short)
    findings = ask(m, "You write the key findings of a research report. Each finding is specific, carries its number and its source.",
                    base + "\nWrite 5 to 8 key findings as a numbered list. Each item: a bold one-sentence finding, then 1-3 "
                           "sentences of evidence with the numbers and [id] citations, and what it means for the question.\n" + rules, 1500)
    experiments = ask(m, "You design experiments a small team can run next, from the evidence in a research report.",
                       base + ("\nIDEAS THE TEAM WROTE DOWN:\n" + "\n".join(f"- {x}" for x in ideas[-10:]) + "\n" if ideas else "")
                       + "\nDesign 3 to 5 concrete experiments that would move the question forward. For each, a '### ' heading "
                         "with a short name, then bullets: **Hypothesis**, **Setup** (model size, data, what changes between "
                         "arms, what it costs on one or two consumer or cloud GPUs), **Measure**, **What would change our mind**, "
                         "**Builds on** [ids]. Prefer cheap, decisive experiments over big ones.\n" + rules, 1700)
    title = ask(m, "You name finished research reports. Answer with the title only.",
                f"THE QUESTION:\n{q[:1500]}\n\nTHE ANSWER:\n{short[:1500]}\n\nGive this report a title: 4 to 10 words, plain and "
                "specific, like a good paper or essay title. No quotes, no markdown, no colon-separated subtitle unless it helps.", 40)
    title = pick_title(title)
    if 8 <= len(title) <= 120 and not title.startswith("(") and "could not be written" not in title:
        write(m.p("state", "report_title.txt"), title + "\n")
    from .waves import knowledge_from_ledgers
    sections = {}
    for src_md in (knowledge_from_ledgers(m, ""), re.sub(r"^# .*\n", "", k)):  # the model's state wins; the ledgers fill gaps
        for title, body in re.findall(r"^## +(.+?)\s*\n(.*?)(?=^## |\Z)", src_md, re.S | re.M):
            if body.strip() and not re.fullmatch(r"(- none( recorded)?|1\. none recorded|- nothing yet)", body.strip()):
                sections[title.strip().lower()] = body.strip()
    open_md = "\n\n".join(f"### {name}\n\n{sections[key]}" for key, name in
                          (("contested", "Contested"), ("open questions", "Still open"), ("dead ends", "Dead ends")) if sections.get(key))
    w = load_waves(m)
    hist = w.get("history") or []
    cards_n = len(ranked)
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    L = [f"_Finished report · {cards_n} papers read · {len(hist) or 1} {'wave' if len(hist) <= 1 else 'waves'} · "
         f"{len([t for t in m.plan.get('tracks', [])])} tracks · refined {when}_", "",
         "## The answer", "", short, "",
         "## Key findings", "", findings, "",
         "## How the papers compare", "", "_Built from the paper cards: every number is quoted in the paper (see the dossier)._", "",
         comparison_table(ranked), "",
         "## Experiments worth running", "", experiments, ""]
    if open_md:
        L += ["## What is still open", "", open_md, ""]
    if hist:
        L += ["## How the run went", ""] + [f"- **Wave {h.get('n')}** ({h.get('cards', 0)} papers): {h.get('headline', '')}" for h in hist] + [""]
    L += ["## The papers", "", "_Every paper the team read, best first, with its method, numbers and the quotes behind them._", ""]
    by_track = {}
    for c in ranked:
        by_track.setdefault((c.get("tracks") or ["other"])[0], []).append(c)
    for t in m.plan.get("tracks", []):
        cs = by_track.pop(t["id"], [])
        if cs:
            L += [f"### {t['name']}", "", f"_{t.get('one_line', '')}_", ""] + [paper_md(c, 4) for c in cs]
    for tid, cs in by_track.items():
        L += [f"### {tid}", ""] + [paper_md(c, 4) for c in cs]
    out = "\n".join(L).rstrip() + "\n"
    write(m.p("final.md"), out)
    m.log("refined", cards=cards_n, notes=len(notes))
    return out


def _read(path: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()[:limit]
    except OSError:
        return ""
