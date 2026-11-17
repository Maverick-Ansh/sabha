"""A fake research daemon for demo mode and the smoke test: no models, no network, same files as the real one.

    python -m sabha.research.mock --repo DIR --mission ID [--hours H] [--every 3] [--cards 8]

It plans two tracks from the brief, then writes one card every few seconds (registry, events, cards/<id>.json
and .md, live.json heartbeat, status_line.txt), a report per track every 4 cards, and on a `finish` command in
state/inbox.jsonl (or at the deadline, or after --cards cards plus a short idle) the final synthesis. Then it marks
the mission done and exits, exactly like `sabha.research.run --once`.

With --wave-s N (a continuous run) it also turns a wave over every N seconds the way research/waves.py does:
a chapter in waves/wNN.md, knowledge.md, one long-term memory, a new track in wave 2, REPORT.md, the wave event
and a line in state/notify.jsonl.
"""
from __future__ import annotations

import argparse
import os
import time

from . import live, longmem, waves as W
from .memory import Mission, append, now, read_json, read_jsonl, write, write_json

PAPERS = [("2301.08243", "Self-supervised learning from images with a joint-embedding predictive architecture"),
          ("2506.09985", "Video world models that plan in latent space"),
          ("2403.00504", "Learning to act from offline experience with a learned simulator"),
          ("2410.11081", "A benchmark for long-horizon planning with learned models"),
          ("2502.02710", "Contrastive pretraining for control: a careful ablation"),
          ("2311.17024", "When does a world model help a policy? Controlled experiments"),
          ("2408.01234", "Scaling laws for predictive representation learning"),
          ("2509.04321", "A survey of latent dynamics models for robotics")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--mission", required=True)
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--every", type=float, default=3.0, help="seconds between cards")
    ap.add_argument("--cards", type=int, default=len(PAPERS))
    ap.add_argument("--wave-s", type=float, default=0, help="seconds per wave (0 = one pass)")
    a = ap.parse_args()
    m = Mission(os.path.join(a.repo, "missions", a.mission))
    until = time.time() + a.hours * 3600
    brief = open(m.p("brief.md"), encoding="utf-8").read()
    question = next((ln.strip() for ln in brief.split("## The question", 1)[-1].splitlines() if ln.strip()), "the question")
    taste = m.taste
    tracks = [{"id": "t1", "name": "Methods", "one_line": "Which methods answer the question, and how well.", "weight": 1.0,
               "questions": [{"id": "t1.q1", "text": question[:200]}], "seeds": []},
              {"id": "t2", "name": "Evidence", "one_line": "Benchmarks, ablations and what did not hold up.", "weight": 1.0,
               "questions": [{"id": "t2.q1", "text": "Which claims survive a controlled test?"}], "seeds": []}]
    write_json(m.p("plan.json"), {"mission": m.id, "one_line": question[:200], "hours": a.hours, "tracks": tracks,
                                  "synthesis_questions": ["What is the answer?"], "mock": True})
    write_json(m.p("state", "mission.json"), {"status": "running", "started": now(), "ts": now()})
    m.log("planned", tracks=len(tracks))
    live.bind(m.p("state", "live.json"), m.id)
    live.slot("w0", "worker")
    done_cards: list[str] = []
    finished_by = ""
    t_last_card = time.time()
    home = longmem.home_for(a.repo)
    longmem.seed_owner(home, taste, [], source=m.id)
    wv = W.start(m, a.wave_s, until, 0) if a.wave_s else {}

    def turnover(final: bool = False) -> None:
        nonlocal wv
        n, t0 = int(wv["n"]), float(wv["started"])
        got = [c for c in done_cards if (m.card(c) or {}).get("wave") == n]
        headline = f"Demo wave {n}: {len(got)} papers, " + ("the strongest is " + PAPERS[[p[0] for p in PAPERS].index(got[0])][1]
                                                           if got else "nothing new")
        write(m.p("waves", f"w{n:02d}.md"), f"## Wave {n} · {W.hhmm(t0)}–{W.hhmm(time.time())} UTC · {len(got)} papers\n\n"
              f"**{headline}**\n\n### What we found\n\n" + "".join(f"- [{c}] {(m.card(c) or {}).get('title', '')}\n" for c in got)
              + "\n### What the next wave should do\n\n- Read the ablations (demo).\n")
        write(m.p("knowledge.md"), f"# State of knowledge after wave {n} ({now()})\n\n## Answer so far\n\nDemo: {len(done_cards)} papers "
              f"read; the answer leans on [{done_cards[0] if done_cards else '2301.08243'}].\n\n## Established\n\n- demo claim\n\n"
              "## Contested\n\n- none\n\n## Open questions\n\n1. Does it hold on real hardware?\n\n## Dead ends\n\n- \"world model survey\" via arxiv\n\n"
              "## Next directions\n\n- ablations\n")
        mem = longmem.apply_ops(home, [{"op": "update", "name": "demo-search-lesson", "type": "lesson",
                                        "description": "Demo lesson: short keyword searches find more than long questions",
                                        "body": f"Seen in demo wave {n}: 2-4 keyword arXiv searches worked; whole questions found nothing."}],
                                source=m.id)
        hist = list(wv.get("history") or []) + [{"n": n, "started": t0, "ended": time.time(), "cards": len(got), "headline": headline,
                                                  "memory": mem}]
        wv = dict(wv, history=hist)
        if final:
            wv["closed"] = True
        else:
            if n == 1:
                plan = m.plan
                plan["tracks"].append({"id": "t3", "name": "Open questions", "one_line": "What the first wave left open.",
                                       "weight": 1.0, "since_wave": 2, "seeds": [],
                                       "questions": [{"id": "t3.q1", "text": "Does it hold on real hardware?", "wave": 2}]})
                plan["wave"] = 2
                write_json(m.p("plan.json"), plan)
                m.log("replanned", wave=2, why="demo", changes=["t3 new: Open questions"])
            wv.update(n=n + 1, started=time.time(), ends=time.time() + a.wave_s, plan_changes=["t3 new: Open questions"] if n == 1 else [])
        W.save(m, wv)
        W.render_report(m)
        m.log("wave", n=n, cards=len(got), headline=headline, final=final, next=None if final else n + 1)
        append(m.p("state", "notify.jsonl"), {"kind": "wave", "n": n, "cards": len(got), "headline": headline, "final": final})
    print(f"[mock] mission {m.id}: {len(tracks)} tracks, until {time.strftime('%H:%M', time.gmtime(until))} UTC", flush=True)
    while not finished_by:
        if any(c.get("cmd") == "finish" for c in read_jsonl(m.p("state", "inbox.jsonl"))):
            finished_by = "finish command"
            break
        if time.time() > until:
            finished_by = "deadline"
            break
        i = len(done_cards)
        if i < min(a.cards, len(PAPERS)) and time.time() - t_last_card >= a.every:
            aid, title = PAPERS[i]
            tids = [t["id"] for t in m.plan.get("tracks", [])]
            tid = tids[i % len(tids)]
            live.begin(step="map", paper=aid, title=title, track=tid, i=1, n=3)
            m.mark(aid, state="queued", track=tid, title=title, rel=3, via="search")
            card = {"id": aid, "title": title, "version": "v1", "tracks": [tid], "relevance": 3, "ts": now(),
                    "first_submitted": "2025-01-01", "last_revised": "2025-02-01",
                    "one_line": f"(demo card) {title}.",
                    "why_it_matters": ("Matches your taste: " + taste.splitlines()[0][:120]) if taste else "",
                    "quality": {"grade": "B", "quote_pass": 0.9}, "causal_evidence": "n/a", "model_type": ["other"],
                    "wave": int(wv.get("n") or 1) if wv else 1}
            write_json(m.p("cards", aid + ".json"), card)
            write(m.p("cards", aid + ".md"), f"# {title}\n\n**One line.** {card['one_line']}\n\n"
                  + (f"**Why it matters to you.** {card['why_it_matters']}\n" if card["why_it_matters"] else ""))
            m.mark(aid, state="carded", track=tid, title=title)
            m.log("carded", id=aid, track=tid, grade="B", quote_pass=0.9, secs=int(a.every))
            done_cards.append(aid)
            t_last_card = time.time()
            live.idle()
            if len(done_cards) % 4 == 0:
                for t in tracks:
                    mine = [c for j, c in enumerate(done_cards) if j % 2 == int(t["id"][1:]) - 1]
                    write(m.p("tracks", t["id"], "report.md"), f"# {t['id']} {t['name']}: report (demo)\n\n## Summary\n\n"
                          + "".join(f"- [{c}]\n" for c in mine))
                    m.log("report", track=t["id"], cards=len(mine))
        elif i >= min(a.cards, len(PAPERS)) and time.time() - t_last_card > 4 * a.every:
            finished_by = "all demo cards written"
        if wv and not wv.get("closed") and time.time() >= float(wv["ends"]) and not finished_by:
            turnover()
        left = int((until - time.time()) // 60)
        write(m.p("state", "status_line.txt"), f"{time.strftime('%H:%M')} up 0h00 left {left}m cards {len(done_cards)} err 0 (demo)\n")
        time.sleep(0.5)
    if wv and not wv.get("closed"):
        turnover(final=True)
    live.begin(step="synthesis", final=True)
    write(m.p("synthesis.md"), f"# Synthesis (final, {now()}, demo)\n\n## The question\n\n{question}\n\n"
          f"## Answer\n\nA demo run: {len(done_cards)} cards, " + ", ".join(f"[{c}]" for c in done_cards) + ".\n"
          + (f"\n## By your taste\n\n{taste[:400]}\n" if taste else ""))
    m.log("synthesis", final=True)
    W.render_report(m, final=True)
    append(m.p("state", "notify.jsonl"), {"kind": "done", "headline": W.headline_of_synthesis(m), "cards": len(done_cards),
                                          "waves": len((wv or {}).get("history") or [])})
    st = read_json(m.p("state", "mission.json"), {}) or {}
    st.update(status="done", ts=now(), note=finished_by)
    write_json(m.p("state", "mission.json"), st)
    live.idle()
    time.sleep(2.5)  # one last live.json flush
    print(f"[mock] done ({finished_by}): {len(done_cards)} cards", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
