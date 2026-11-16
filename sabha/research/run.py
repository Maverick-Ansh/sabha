"""The Research mode daemon: runs missions continuously until the session deadline.

    setsid python -m sabha.research.run --repo /kaggle/working/sabha-research --until <epoch>
    python -m sabha.research.run --repo ... --test-paper 2506.09985      (first slice: one card, one push)

Threads: N paper workers (read one paper each, context gone after it), one maintenance
thread (seeds, scouting, snowballing, consolidation, reports, synthesis: the deep-model
jobs), one housekeeping thread (steering every 60 s, STATUS every 2 min, git push every
10 min, DIGEST every 30 min, index refresh). Restart-safe: everything lives in the repo.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import threading
import time
import traceback

from . import agents as A, llm, sources as src
from .index import Index
from .memory import Mission, append, now, read_json, read_jsonl, write, write_json


def sh(cmd: str, cwd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        tok = open("/root/.gh_token").read().strip() if os.path.exists("/root/.gh_token") else "@@"
        return r.returncode, (r.stdout + r.stderr).replace(tok, "***")[-800:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"


class Daemon:
    def __init__(self, repo: str, until: float, slots: int = 6):
        self.repo, self.until, self.slots = repo, until, slots
        self.t_start = time.time()
        self.stop = False
        self.m: Mission | None = None
        self.idx = Index("cpu")
        self.steer: dict = {"lessons": [], "priority_topics": [], "banned": {}}
        self.params = {"max_chunks": 10, "report_every_cards": 20, "report_every_s": 7200, "tavily_cap": 300}
        self.deficit: dict[str, float] = {}
        self.sel_lock = threading.Lock()
        self.git_lock = threading.Lock()
        self.last = {"status": 0, "push": 0, "digest": 0, "steer": 0, "index": 0, "refs": 0}
        self.track_state: dict[str, dict] = {}
        self.last_push_ok = 0.0
        self.errors: list[str] = []
        self.synth_draft_done = False
        self.synth_final_done = False
        self.final_reports_done = False
        self.restart = False
        if os.environ.get("SABHA_S1"):  # e.g. SABHA_S1=reader: System-1 triage served by the 4B (no room for a 0.8B server)
            llm.OVERRIDE["s1"] = os.environ["SABHA_S1"]

    # ------------------------------------------------------------ mission selection
    def pick_mission(self) -> Mission | None:
        best = None
        for b in sorted(glob.glob(os.path.join(self.repo, "missions", "*", "brief.md"))):
            root = os.path.dirname(b)
            st = read_json(os.path.join(root, "state", "mission.json"), {}) or {}
            if st.get("status") == "done":
                continue
            head = open(b, encoding="utf-8").read(400)
            pre = "mode: preempt" in head
            if best is None or (pre and not best[1]):
                best = (root, pre)
        return Mission(best[0]) if best else None

    def set_status(self, status: str) -> None:
        p = self.m.p("state", "mission.json")
        st = read_json(p, {}) or {}
        st.update(status=status, ts=now(), started=st.get("started") or now())
        write_json(p, st)

    def weights(self) -> dict[str, float]:
        w = {t["id"]: float(t.get("weight", 1.0)) for t in self.m.plan["tracks"]}
        w.update({k: float(v) for k, v in (read_json(self.m.p("state", "weights.json"), {}) or {}).items() if k in w})
        for tid, s in self.track_state.items():
            if s.get("saturated") and not self.m.queued(tid):
                w[tid] = 0.0
        return w

    # ------------------------------------------------------------ workers
    def next_paper(self) -> str | None:
        with self.sel_lock:
            w = self.weights()
            tids = [t for t in w if w[t] > 0]
            for t in tids:
                self.deficit[t] = self.deficit.get(t, 0.0) + w[t]
            for t in sorted(tids, key=lambda t: -self.deficit[t]):
                q = self.m.queued(t)
                if q:
                    self.deficit[t] -= sum(w.values())
                    aid = q[0]["id"]
                    self.m.mark(aid, state="reading")
                    return aid
            q = self.m.queued()
            if q:
                self.m.mark(q[0]["id"], state="reading")
                return q[0]["id"]
        return None

    def worker(self, k: int) -> None:
        while not self.stop:
            if time.time() > self.until - 45 * 60:
                return
            aid = self.next_paper()
            if not aid:
                time.sleep(20)
                continue
            try:
                reg = self.m.reg.get(aid, {})
                res = A.read_paper(self.m, aid, self.idx, self.budget, self.steer, max_chunks=int(self.params["max_chunks"]),
                                   tier_b=reg.get("via") == "seed")
                if res == "carded":
                    tid = self.m.reg[aid].get("track")
                    ts = self.track_state.setdefault(tid, {})
                    ts.setdefault("new_cards", []).append(aid)
                    ts["cards_since_report"] = ts.get("cards_since_report", 0) + 1
            except Exception as e:
                self.err(f"worker {k} {aid}: {type(e).__name__}: {str(e)[:200]}")
                self.m.mark(aid, state="failed", why=f"crash: {str(e)[:150]}")

    def err(self, msg: str) -> None:
        self.errors = (self.errors + [f"{time.strftime('%H:%M')} {msg}"])[-20:]
        if self.m:
            self.m.log("error", msg=msg[:400])

    # ------------------------------------------------------------ maintenance (deep-model jobs, one at a time)
    def maintenance(self) -> None:
        m = self.m
        if not os.path.exists(m.p("memory/seeds.json")):
            def seeds():
                try:
                    A.resolve_seeds(m, self.idx, self.budget)
                except Exception as e:
                    self.err(f"seeds: {e}")
                    traceback.print_exc()
            threading.Thread(target=seeds, daemon=True).start()  # scouting starts at once, seeds resolve beside it
        while not self.stop:
            left = self.until - time.time()
            try:
                did = False
                if left > 75 * 60:
                    for tid in m.track_ids():
                        ts = self.track_state.setdefault(tid, {})
                        if ts.get("saturated") or self.weights().get(tid, 1) <= 0:
                            continue
                        if len(m.queued(tid)) < 8 and time.time() - ts.get("last_scout", 0) > 600:
                            r = A.scout(m, tid, self.idx, self.budget, self.steer)
                            ts["last_scout"] = time.time()
                            ts["low_rounds"] = ts.get("low_rounds", 0) + 1 if r.get("rel2", 0) < 2 else 0
                            if ts["low_rounds"] >= 3:
                                ts["saturated"] = True
                                m.log("saturated", track=tid)
                            did = True
                    if time.time() - self.last["refs"] > 900:
                        A.triage_refs(m, self.idx, self.steer)
                        self.last["refs"] = time.time()
                for tid in m.track_ids():
                    ts = self.track_state.setdefault(tid, {})
                    new = ts.get("new_cards", [])
                    if len(new) >= 8 or (new and time.time() - ts.get("last_cons", self.t_start) > 45 * 60):
                        ts["new_cards"] = []
                        A.consolidate(m, tid, new)
                        A.rebuild_playbook(m, self.steer.get("lessons", []))
                        ts["last_cons"] = time.time()
                        did = True
                if left > 60 * 60:
                    for tid in m.track_ids():
                        ts = self.track_state.setdefault(tid, {})
                        due = ts.get("cards_since_report", 0) >= self.params["report_every_cards"] or (
                            time.time() - ts.get("last_report", self.t_start) > self.params["report_every_s"] and ts.get("cards_since_report", 0) >= 3)
                        if due:
                            A.write_report(m, tid)
                            ts["cards_since_report"] = 0
                            ts["last_report"] = time.time()
                            did = True
                half = self.t_start + (self.until - self.t_start) / 2
                if not self.synth_draft_done and time.time() > half:
                    A.write_synthesis(m, final=False)
                    self.synth_draft_done = True
                if not self.final_reports_done and left < 60 * 60:
                    for tid in m.track_ids():
                        A.write_report(m, tid)
                    self.final_reports_done = True
                if not self.synth_final_done and left < 40 * 60:
                    A.write_synthesis(m, final=True)
                    self.synth_final_done = True
                    self.set_status("done")
                    self.sync("mission done")
                    self.stop = True
                if not did:
                    time.sleep(30)
            except Exception as e:
                self.err(f"maintenance: {type(e).__name__}: {str(e)[:200]}")
                traceback.print_exc()
                time.sleep(60)

    # ------------------------------------------------------------ git, steering, status, digest
    def sync(self, why: str = "sync") -> bool:
        with self.git_lock:
            cwd = self.repo
            sh("git add -A", cwd)
            rc, out = sh(f'git commit -qm "box: {why} {now()}"', cwd)
            rc, out = sh("git fetch -q origin && git merge -q --no-edit origin/main", cwd)
            if rc != 0:
                self.err(f"git merge: {out[-200:]}")
                sh("git merge --abort", cwd)
            rc, out = sh("git push -q origin HEAD:main", cwd, timeout=180)
            if rc == 0:
                self.last_push_ok = time.time()
                return True
            self.err(f"git push: {out[-200:]}")
            return False

    def pull_steering(self) -> None:
        with self.git_lock:
            sh("git fetch -q origin && git merge -q --no-edit origin/main", self.repo)
        p = self.m.p("steering.md")
        if not os.path.exists(p):
            return
        text = open(p, encoding="utf-8").read()
        mm = re.search(r"```ya?ml\n(.*?)```", text, re.S)
        try:
            import yaml
            d = yaml.safe_load(mm.group(1) if mm else text) or {}
        except Exception as e:
            self.err(f"steering parse: {e}")
            return
        applied = read_json(self.m.p("state", "steering_applied.json"), {"seq": 0, "items": []}) or {"seq": 0}
        seq = int(d.get("seq", 0) or 0)
        if seq <= int(applied.get("seq", 0)):
            return
        res = []
        m = self.m
        if d.get("track_weights"):
            w = read_json(m.p("state", "weights.json"), {}) or {}
            w.update({k: float(v) for k, v in d["track_weights"].items()})
            write_json(m.p("state", "weights.json"), w)
            res.append(f"weights {w}")
        for k in ("priority_topics", "lessons"):
            if d.get(k):
                self.steer[k] = (self.steer.get(k, []) + list(d[k]))[-30:]
                res.append(f"{k} +{len(d[k])}")
        if d.get("banned"):
            b = self.steer.setdefault("banned", {})
            for k, v in d["banned"].items():
                b[k] = sorted(set(b.get(k, []) + list(v or [])))
            res.append("banned updated")
        for s in d.get("seed_ids", []) or []:
            aid = str(s.get("id", "")).strip()
            meta = self.idx.get(aid)
            if aid and s.get("title_has"):  # the validator names a word the real title or abstract must contain
                try:
                    meta = src.abs_meta(aid)
                except Exception:
                    meta = None
                if not meta or str(s["title_has"]).lower() not in (meta.get("title", "") + " " + meta.get("abstract", "")[:800]).lower():
                    res.append(f"seed {aid} REFUSED: '{s['title_has']}' not in title/abstract")
                    continue
            if aid and not (m.card(aid)):
                m.mark(aid, state="queued", track=s.get("track"), rel=3, prio=2.5, via="seed", seed=s.get("name"),
                       title=(meta or {}).get("title", ""), why="validator seed")
                res.append(f"seed {aid} queued")
        for f in d.get("card_fixes", []) or []:
            aid = str(f.get("id", ""))
            c = m.card(aid)
            if not c:
                res.append(f"card_fix {aid}: no card")
                continue
            act = f.get("action")
            if act == "reread":
                os.replace(m.p("cards", aid + ".json"), m.p("rejected", aid + ".prev.json"))
                m.mark(aid, state="queued", prio=2.0, why="validator reread: " + str(f.get("why", ""))[:150])
            elif act == "reject":
                os.replace(m.p("cards", aid + ".json"), m.p("rejected", aid + ".json"))
                m.mark(aid, state="rejected", why="validator: " + str(f.get("why", ""))[:150])
            elif act == "patch" and f.get("field"):
                c[f["field"]] = f.get("value")
                c.setdefault("quality", {}).setdefault("lint", []).append(f"validator patched {f['field']}")
                write_json(m.p("cards", aid + ".json"), c)
                write(m.p("cards", aid + ".md"), A.card_md(c))
            res.append(f"card_fix {aid} {act}")
        for f in d.get("claim_fixes", []) or []:
            led = m.claims(f.get("track", ""))
            c = led.get(f.get("claim", ""))
            if c:
                c = {**c, "status": "retired" if f.get("action") == "retire" else "contested", "why": "validator: " + str(f.get("why", ""))[:200]}
                c.pop("ts", None)
                append(m.p("tracks", f["track"], "claims.jsonl"), c)
                A.render_digest(m, f["track"])
                res.append(f"claim {f['claim']} {f.get('action')}")
        for a in d.get("board_answers", []) or []:
            b = m.board().get(a.get("id", ""))
            if b:
                append(m.p("memory/board.jsonl"), {**b, "status": "answered", "answer": str(a.get("answer", ""))[:800], "by": "validator", "ts": now()})
                res.append(f"board {a['id']} answered")
        if d.get("params"):
            self.params.update(d["params"])
            if "tavily_cap" in d["params"]:
                self.budget.d["cap"] = int(d["params"]["tavily_cap"])
            res.append(f"params {d['params']}")
        if d.get("lessons"):
            A.rebuild_playbook(m, self.steer["lessons"])
        if d.get("stop"):
            self.stop = True
            res.append("stop")
        if d.get("restart"):  # pull new daemon code and exit 3; the bash loop on the box relaunches it
            code = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            rc, out = sh("git pull -q --autostash && git log --oneline -1", code)
            res.append(f"restart: code {out.strip()[-80:]}")
            self.restart = self.stop = True
        write_json(m.p("state", "steering_applied.json"), {"seq": seq, "items": res, "ts": now()})
        m.log("steering", seq=seq, items=res)

    def status(self) -> None:
        m = self.m
        up = time.time() - self.t_start
        cards = [r for r in m.reg.values() if r.get("state") == "carded"]
        ev = read_jsonl(m.p("state", "events.jsonl"))
        hour = [e for e in ev if e.get("kind") == "carded" and time.time() - _ts(e.get("ts")) < 3600]
        sp = llm.speed()
        health = " ".join(f"{r}=>{llm.OVERRIDE[r]}" if r in llm.OVERRIDE else f"{r}:{'ok' if llm.up(r) else 'DOWN'}" for r in llm.ROLES)
        applied = (read_json(m.p("state", "steering_applied.json"), {}) or {}).get("seq", 0)
        line = (f"{time.strftime('%H:%M')} up {int(up // 3600)}h{int(up % 3600 // 60):02d} left {int((self.until - time.time()) // 60)}m "
                f"cards {len(cards)} queued {m.count('queued')} pph {len(hour)} {health} push {int((time.time() - self.last_push_ok) // 60) if self.last_push_ok else -1}m "
                f"tav {self.budget.d['tavily_used']}/{self.budget.d['cap']} idx {len(self.idx)} steer {applied} err {len(self.errors)}")
        write(m.p("state", "status_line.txt"), line + "\n")
        L = [f"# STATUS {m.id}", "", f"`{line}`", "", f"Mission: {m.plan.get('one_line', '')}", "",
             "| track | name | cards | queued | rejected | failed | weight | state |", "|---|---|---|---|---|---|---|---|"]
        w = self.weights()
        for t in m.plan.get("tracks", []):
            ts = self.track_state.get(t["id"], {})
            L.append(f"| {t['id']} | {t['name'][:40]} | {m.count('carded', t['id'])} | {m.count('queued', t['id'])} | "
                     f"{m.count('rejected', t['id'])} | {m.count('failed', t['id'])} | {w.get(t['id'], 1)} | "
                     f"{'saturated' if ts.get('saturated') else 'active'} |")
        L += ["", f"Models: {health}; tokens/s per stream {sp}", "", "## Last 10 cards"]
        for e in [e for e in ev if e.get("kind") == "carded"][-10:][::-1]:
            c = m.card(e["id"]) or {}
            L.append(f"- [{e['id']}](cards/{e['id']}.md) {c.get('title', '')[:90]} ({e.get('track')}, quote {e.get('quote_pass')}, {e.get('grade')}, {e.get('secs')} s)")
        L += ["", "## Recent errors"] + [f"- {x}" for x in self.errors[-8:]]
        sa = read_json(m.p("state", "steering_applied.json"), {}) or {}
        L += ["", f"## Steering applied: seq {sa.get('seq', 0)}"] + [f"- {x}" for x in sa.get("items", [])]
        write(m.p("STATUS.md"), "\n".join(L) + "\n")

    def digest(self) -> None:
        m = self.m
        ev = read_jsonl(m.p("state", "events.jsonl"))
        line = open(m.p("state", "status_line.txt"), encoding="utf-8").read().strip() if os.path.exists(m.p("state", "status_line.txt")) else ""
        seq = len([e for e in ev if e.get("kind") == "digest"]) + 1
        L = [f"# DIGEST {m.id} seq {seq} {now()}", "", "## 1 Health", line, f"tokens/s {llm.speed()}  llm stats {llm.STATS}",
             f"failures by reason: {_fail_reasons(m)}", f"tavily {self.budget.d}", "errors:"] + [f"- {x}" for x in self.errors[-10:]]
        L += ["", "## 2 Tracks"]
        for t in m.plan["tracks"]:
            cs = [c for c in m.cards() if t["id"] in c.get("tracks", [])]
            qp = sum(c["quality"]["quote_pass"] for c in cs) / len(cs) if cs else 0
            r3 = sum(1 for c in cs if c.get("relevance", 0) >= 3) / len(cs) if cs else 0
            L.append(f"- {t['id']} {t['name']}: cards {len(cs)}, queued {m.count('queued', t['id'])}, rel3 share {r3:.2f}, "
                     f"mean quote_pass {qp:.2f}, weight {self.weights().get(t['id'])}, {'saturated' if self.track_state.get(t['id'], {}).get('saturated') else 'active'}")
        L += ["", "## 3 Coverage (supported claims per question, zeros first)"]
        for t in m.plan["tracks"]:
            led = m.claims(t["id"])
            cov = sorted(((q["id"], sum(1 for c in led.values() if c.get("q") == q["id"] and c.get("status") != "retired")) for q in t["questions"]), key=lambda x: x[1])
            L.append(f"- {t['id']}: " + ", ".join(f"{q}={n}" for q, n in cov))
        L += ["", "## 4 Top papers"]
        for t in m.plan["tracks"]:
            L.append(f"### {t['id']}")
            L += [f"- {c['id']} | {c['title'][:80]} | {c.get('one_line', '')[:140]}" for c in A.rank_cards(m, t["id"])[:10]]
        L += ["", "## 5 Card audit"]
        import random
        for t in m.plan["tracks"]:
            cs = [c for c in m.cards() if t["id"] in c.get("tracks", [])]
            pick = cs[-2:] + ([random.choice(cs[:-2])] if len(cs) > 2 else [])
            for c in pick:
                res = "; ".join(f"{r.get('metric')}={r.get('value')} \"{(r.get('ev') or [{}])[0].get('quote', '')[:120]}\"" for r in c.get("results", [])[:2])
                L.append(f"- {c['id']} ({t['id']}) {c.get('one_line', '')[:200]}\n  train: {c.get('training_method', {}).get('text', '')[:220]}\n"
                         f"  results: {res}\n  repo: {c.get('official_repo', {}).get('url')} [{c.get('official_repo', {}).get('status')}] lint: {c['quality']['lint'][:3]}")
        L += ["", "## 6 Claims (new and contested)"]
        for t in m.plan["tracks"]:
            led = list(m.claims(t["id"]).values())
            L += [f"- {c['id']} [{c.get('status')}] {c.get('text', '')[:200]} for {c.get('for', [])[:4]} against {c.get('against', [])[:4]}"
                  for c in led[-10:] + [c for c in led[:-10] if c.get("status") == "contested"]]
        L += ["", "## 7 Queries (last 5 per track)"]
        for t in m.plan["tracks"]:
            L += [f"- {t['id']} \"{q['text']}\" {q['via']}: new {q.get('n_new')}, useful {q.get('n_rel2')}" for q in m.queries(t["id"])[-5:]]
        refused = [e.get("text") for e in ev if e.get("kind") == "query_refused"][-8:]
        L.append(f"refused loops: {refused}")
        L += ["", "## 8 Board (to validator / open) and unresolved seeds"]
        L += [f"- {b['id']} {b['from']}->{b['to']} [{b['status']}] {b['q'][:200]}" for b in m.board().values() if b.get("status") == "open"][:20]
        seeds = read_json(m.p("memory/seeds.json"), []) or []
        L.append("unresolved seeds: " + ", ".join(f"{s['name']} ({s['track']})" for s in seeds if not s.get("id")))
        L.append("resolved seeds: " + ", ".join(f"{s['name']}={s['id']}" for s in seeds if s.get("id")))
        L += ["", "## 9 Anomalies"]
        weak = [c["id"] for c in m.cards() if c["quality"]["grade"] == "weak"]
        ol = {}
        for c in m.cards():
            ol.setdefault(c.get("one_line", "")[:80], []).append(c["id"])
        L += [f"weak cards: {weak[-20:]}", f"duplicate one_lines: {[v for v in ol.values() if len(v) > 1][:5]}",
              f"rejected: {m.count('rejected')}, failed: {m.count('failed')}"]
        L += ["", "## 10 Reports (start of each)"]
        for t in m.plan["tracks"]:
            try:
                rep = open(m.p("tracks", t["id"], "report.md"), encoding="utf-8").read()
                L.append(f"### {t['id']}\n" + rep[rep.find("##"):][:900] if "##" in rep else rep[:900])
            except OSError:
                L.append(f"### {t['id']}: no report yet")
        write(m.p("DIGEST.md"), "\n".join(L)[:60000] + "\n")
        m.log("digest", seq=seq)

    def housekeeping(self) -> None:
        while not self.stop:
            t = time.time()
            try:
                if t - self.last["index"] > 600:
                    n = self.idx.refresh()
                    self.last["index"] = t
                    if n:
                        print(f"[run] index +{n} = {len(self.idx)}", flush=True)
                if t - self.last["steer"] > 60:
                    self.pull_steering()
                    self.last["steer"] = t
                if t - self.last["status"] > 120:
                    self.status()
                    self.last["status"] = t
                if t - self.last["digest"] > 1800:
                    self.digest()
                    self.last["digest"] = t
                if t - self.last["push"] > 600:
                    self.sync()
                    self.last["push"] = t
            except Exception as e:
                self.err(f"housekeeping: {type(e).__name__}: {str(e)[:200]}")
                traceback.print_exc()
            time.sleep(15)

    # ------------------------------------------------------------ main
    def run(self) -> None:
        print(f"[run] start, until {time.strftime('%H:%M', time.gmtime(self.until))} UTC", flush=True)
        self.idx.refresh()
        while time.time() < self.until - 15 * 60:
            self.m = self.pick_mission()
            if not self.m:
                time.sleep(60)
                continue
            self.stop = False
            m = self.m
            self.budget = src.Budget(m.p("state", "budget.json"), cap=int(self.params["tavily_cap"]))
            if not m.plan.get("tracks"):
                A.plan_mission(m)
            self.set_status("running")
            A.rebuild_playbook(m, self.steer.get("lessons", []))
            n = m.requeue_stale(0)
            print(f"[run] mission {m.id}: {len(m.track_ids())} tracks, {n} stale requeued", flush=True)
            for tid in m.track_ids():
                A.render_digest(m, tid)
            self.status()
            self.sync("mission start")
            threads = [threading.Thread(target=self.housekeeping, daemon=True),
                       threading.Thread(target=self.maintenance, daemon=True)]
            threads += [threading.Thread(target=self.worker, args=(k,), daemon=True) for k in range(self.slots)]
            for th in threads:
                th.start()
            while not self.stop and time.time() < self.until - 12 * 60:
                time.sleep(20)
            self.stop = True
            for th in threads:
                th.join(timeout=600)
            self.status()
            self.digest()
            self.sync("final" if time.time() > self.until - 15 * 60 else "mission switch")
            if self.restart:
                print("[run] restart requested by steering", flush=True)
                return 3
        print("[run] deadline reached", flush=True)
        return 0


def _ts(s) -> float:
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (TypeError, ValueError):
        return 0.0


def _fail_reasons(m: Mission) -> dict:
    out = {}
    for r in m.reg.values():
        if r.get("state") == "failed":
            k = (r.get("why") or "?").split(":")[0][:30]
            out[k] = out.get(k, 0) + 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="/kaggle/working/sabha-research")
    ap.add_argument("--until", type=float, default=0, help="epoch seconds of the session deadline")
    ap.add_argument("--hours", type=float, default=11.5)
    ap.add_argument("--slots", type=int, default=6)
    ap.add_argument("--test-paper", default="")
    a = ap.parse_args()
    until = a.until or time.time() + a.hours * 3600
    d = Daemon(a.repo, until, a.slots)
    if a.test_paper:
        d.m = d.pick_mission()
        d.budget = src.Budget(d.m.p("state", "budget.json"))
        d.idx.refresh()
        if not d.m.plan.get("tracks"):
            A.plan_mission(d.m)
        print("plan:", [(t["id"], t["name"], len(t["questions"]), t["seeds"][:4]) for t in d.m.plan["tracks"]], flush=True)
        t0 = time.time()
        d.m.mark(a.test_paper, state="queued", track=d.m.track_ids()[0], rel=3, prio=2, via="seed", seed="test")
        print("read:", A.read_paper(d.m, a.test_paper, d.idx, d.budget, d.steer, tier_b=False), round(time.time() - t0), "s", flush=True)
        d.status()
        print("push:", d.sync("test paper"), flush=True)
        return
    raise SystemExit(d.run())


if __name__ == "__main__":
    main()
