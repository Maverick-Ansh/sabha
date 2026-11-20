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

from . import agents as A, inbox, live, llm, longmem, notebook as NB, sources as src, trace, waves as W
from .index import Index
from .memory import Mission, append, now, read_json, read_jsonl, write, write_json


def sh(cmd: str, cwd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        tok = open("/root/.gh_token").read().strip() if os.path.exists("/root/.gh_token") else "@@"
        return r.returncode, (r.stdout + r.stderr).replace(tok, "***")[-800:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def margins(total_s: float) -> dict:
    """Seconds before the deadline at which each phase ends. 12 h runs keep the measured values; short runs scale down."""
    def m(minutes: float, frac: float) -> float:
        return min(minutes * 60, frac * max(total_s, 600))
    return {"scout": m(75, .35), "reports": m(60, .30), "work": m(45, .22), "synth": m(40, .18), "loop": m(15, .07), "end": m(12, .05)}


class Daemon:
    def __init__(self, repo: str, until: float, slots: int = 6, mission_id: str = "", once: bool = False, wave_min: float = -1):
        self.repo, self.until, self.slots = repo, until, slots
        self.wave_min = wave_min  # -1: from the brief (wave_minutes:), 0: no waves (one pass), >0: continuous run in waves
        self.wave_s = 0.0
        self.waves: dict = {}
        self.home = longmem.home_for(repo)
        self.turning = False                 # a wave turnover is running in its own thread
        self.final_thread: threading.Thread | None = None  # the end of the run: final reports, last wave, synthesis
        self.cons_lock = threading.Lock()    # one consolidation of a track at a time (maintenance and turnover both do it)
        self.mission_id, self.once = mission_id, once
        self.t_start = time.time()
        self.M = margins(until - self.t_start)
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
        for role in ("s1", "deep", "reader"):  # e.g. SABHA_S1=reader (no 0.8B server), SABHA_DEEP=reader (one-GPU box)
            if os.environ.get(f"SABHA_{role.upper()}"):
                llm.OVERRIDE[role] = os.environ[f"SABHA_{role.upper()}"]

    # ------------------------------------------------------------ mission selection
    def pick_mission(self) -> Mission | None:
        best = None
        for b in sorted(glob.glob(os.path.join(self.repo, "missions", self.mission_id or "*", "brief.md"))):
            root = os.path.dirname(b)
            st = read_json(os.path.join(root, "state", "mission.json"), {}) or {}
            if st.get("status") == "done":
                continue
            head = open(b, encoding="utf-8").read(400)
            pre = "mode: preempt" in head
            if best is None or (pre and not best[1]):
                best = (root, pre)
        return Mission(best[0]) if best else None

    def queue_loved(self, m: Mission) -> None:
        loved = read_json(m.p("loves.json"), []) or []
        tids = m.track_ids()
        for aid in loved if tids else []:
            if isinstance(aid, str) and aid not in m.reg:
                m.mark(aid, state="queued", track=tids[0], rel=3, prio=3, via="seed", seed="loved by the owner")
        if loved:
            print(f"[run] {len(loved)} loved papers queued first", flush=True)

    def setup_waves(self, m: Mission) -> None:
        wm = self.wave_min
        if wm < 0:
            head = open(m.p("brief.md"), encoding="utf-8").read(600)
            mm = re.search(r"^wave_minutes:\s*([\d.]+)", head, re.M)
            wm = float(mm.group(1)) if mm else 0.0
        self.wave_s = max(wm, 0.0) * 60
        self.waves = W.start(m, self.wave_s, self.until, self.M["reports"]) if self.wave_s else {}
        if self.waves:
            print(f"[run] continuous: wave {self.waves['n']} of about {max(int((self.until - self.t_start) // self.wave_s), 1)}, "
                  f"{int(self.wave_s // 60)} min each", flush=True)

    def finish(self) -> str:
        """The app's Finish now: final reports and the synthesis from what is in hand, then the run ends."""
        self.until = min(self.until, time.time() + self.M["synth"] + 120)
        return "finishing: final reports and synthesis now, then the run ends"

    def set_status(self, status: str) -> None:
        p = self.m.p("state", "mission.json")
        st = read_json(p, {}) or {}
        st.update(status=status, ts=now(), started=st.get("started") or now())
        write_json(p, st)

    def weights(self) -> dict[str, float]:
        w = {t["id"]: float(t.get("weight", 1.0)) for t in self.m.plan["tracks"]}
        w.update({k: float(v) for k, v in (read_json(self.m.p("state", "weights.json"), {}) or {}).items() if k in w})
        for t in self.m.plan["tracks"]:
            if t.get("retired"):  # closed by a wave re-plan: its cards stay, it reads no more
                w[t["id"]] = 0.0
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
            q = [r for r in self.m.queued() if w.get(r.get("track"), 1.0) > 0]  # a paused track (weight 0) stays paused
            if q:
                self.m.mark(q[0]["id"], state="reading")
                return q[0]["id"]
        return None

    def worker(self, k: int) -> None:
        live.slot(f"w{k}", "worker")
        while not self.stop:
            if time.time() > self.until - self.M["work"]:
                return
            aid = self.next_paper()
            if not aid:
                time.sleep(20)
                continue
            try:
                reg = self.m.reg.get(aid, {})
                live.begin(step="fetch", paper=aid, title=reg.get("title") or "", track=reg.get("track"))
                res = A.read_paper(self.m, aid, self.idx, self.budget, self.steer, max_chunks=int(self.params["max_chunks"]),
                                   tier_b=reg.get("via") == "seed")
                if res == "carded":
                    try:
                        NB.add_paper(self.m, aid)
                        W.render_report(self.m)  # the living report shows every paper the moment it is read
                    except Exception as e:
                        self.err(f"notebook {aid}: {type(e).__name__}: {str(e)[:120]}")
                    tid = self.m.reg[aid].get("track")
                    ts = self.track_state.setdefault(tid, {})
                    ts.setdefault("new_cards", []).append(aid)
                    ts["cards_since_report"] = ts.get("cards_since_report", 0) + 1
            except Exception as e:
                self.err(f"worker {k} {aid}: {type(e).__name__}: {str(e)[:200]}")
                self.m.mark(aid, state="failed", why=f"crash: {str(e)[:150]}")
            finally:
                live.idle()

    def err(self, msg: str) -> None:
        self.errors = (self.errors + [f"{time.strftime('%H:%M')} {msg}"])[-20:]
        if self.m:
            self.m.log("error", msg=msg[:400])

    # ------------------------------------------------------------ maintenance (deep-model jobs, one at a time)
    def maintenance(self) -> None:
        m = self.m
        live.slot("maint", "maintenance")
        if not os.path.exists(m.p("memory/seeds.json")):
            def seeds():
                live.slot("seeds", "seeds")
                try:
                    A.resolve_seeds(m, self.idx, self.budget)
                except Exception as e:
                    self.err(f"seeds: {e}")
                    traceback.print_exc()
                finally:
                    live.idle()
            threading.Thread(target=seeds, daemon=True).start()  # scouting starts at once, seeds resolve beside it
        while not self.stop:
            left = self.until - time.time()
            try:
                did = False
                self.maybe_turnover()
                if left > self.M["scout"]:
                    for tid in m.active_track_ids():
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
                        with self.cons_lock:
                            A.consolidate(m, tid, new)
                        self.note(tid, new)
                        A.rebuild_playbook(m, self.steer.get("lessons", []))
                        ts["last_cons"] = time.time()
                        did = True
                if left > self.M["reports"]:
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
                if not self.synth_draft_done and time.time() > half and self.until - self.t_start >= 3 * 3600:
                    A.write_synthesis(m, final=False)
                    self.synth_draft_done = True
                # the final reports and the synthesis run in their own thread (maybe_finalize, from housekeeping): this
                # loop can sit in one scout pass for 20 minutes on a busy box (seen on a real run), past the deadline
                if not did:
                    live.idle()
                    time.sleep(30)
            except Exception as e:
                self.err(f"maintenance: {type(e).__name__}: {str(e)[:200]}")
                traceback.print_exc()
                live.idle()
                time.sleep(60)

    def note(self, tid: str, ids: list[str]) -> None:
        """The lab note after a consolidation, and the living report rebuilt with it: the report grows all run long."""
        try:
            NB.think(self.m, tid, ids, W.question_of(self.m))
            W.render_report(self.m)
        except Exception as e:
            self.err(f"notebook note {tid}: {type(e).__name__}: {str(e)[:160]}")

    # ------------------------------------------------------------ waves (continuous runs, research/waves.py)
    def wave_due(self, left: float) -> bool:
        """Time for a turnover: the wave's time is up, or every active track ran dry (nothing queued, all saturated)
        after at least 8 minutes. Never when the time left is too short for another wave: the end closes the last one."""
        w = self.waves
        if left - self.M["reports"] < 0.4 * self.wave_s:
            return False
        if time.time() >= float(w.get("ends") or 0):
            return True
        act = self.m.active_track_ids()
        dry = act and all(self.track_state.get(t, {}).get("saturated") for t in act) and not self.m.queued()
        return bool(dry) and time.time() - float(w.get("started") or 0) > 480

    def maybe_finalize(self) -> None:
        """Start the end of the run in its own thread once the time left is under the final-reports margin."""
        if self.final_thread is None and self.m is not None and self.until - time.time() < self.M["reports"]:
            self.final_thread = threading.Thread(target=self._finalize, name="finalize", daemon=True)
            self.final_thread.start()

    def _finalize(self) -> None:
        m = self.m
        live.slot("final", "final")
        try:
            t0 = time.time()
            while self.turning and time.time() - t0 < 900:
                time.sleep(5)
            if not self.final_reports_done:
                for tid in m.track_ids():
                    live.begin(step="report", track=tid)
                    A.write_report(m, tid)
                self.final_reports_done = True
            self.close_run()
            for tid in m.track_ids():  # cards read since the last consolidation still get their note
                ts = self.track_state.setdefault(tid, {})
                new, ts["new_cards"] = ts.get("new_cards") or [], []
                if new:
                    with self.cons_lock:
                        A.consolidate(m, tid, new)
                    self.note(tid, new)
            A.write_synthesis(m, final=True)
            live.begin(step="refining the report", final=True)
            try:
                NB.refine(m)
            except Exception as e:
                self.err(f"refine: {type(e).__name__}: {str(e)[:200]}")
                traceback.print_exc()
            W.render_report(m, final=True)
            append(m.p("state", "notify.jsonl"), {"kind": "done", "headline": W.headline_of_synthesis(m),
                                                  "cards": m.count("carded"), "waves": len((self.waves or {}).get("history") or [])})
            trace.emit("final", W.headline_of_synthesis(m), track="team", step="synthesis")
            self.synth_final_done = True
            self.set_status("done")
            self.sync("mission done")
        except Exception as e:
            self.err(f"finalize: {type(e).__name__}: {str(e)[:200]}")
            traceback.print_exc()
        finally:
            live.idle()
            self.stop = True

    def maybe_turnover(self) -> None:
        """Start a wave turnover in its own thread when one is due. Called by housekeeping every 15 s: a scout pass can
        take ten minutes (its triage queues behind the busy readers) and the turnover needs only the deep model."""
        if (self.wave_s and self.waves and not self.waves.get("closed") and not self.turning and self.m is not None
                and self.wave_due(self.until - time.time())):
            self.turning = True
            threading.Thread(target=self._turnover_thread, name="wave-turnover", daemon=True).start()

    def _turnover_thread(self) -> None:
        live.slot("waves", "waves")
        try:
            self.turnover()
        except Exception as e:
            self.err(f"turnover: {type(e).__name__}: {str(e)[:200]}")
            traceback.print_exc()
        finally:
            live.idle()
            self.turning = False

    def turnover(self) -> None:
        m = self.m
        for tid in m.active_track_ids():  # the wave's last cards go into the ledgers before the chapter is written
            ts = self.track_state.setdefault(tid, {})
            new, ts["new_cards"] = ts.get("new_cards") or [], []
            if new:
                with self.cons_lock:
                    A.consolidate(m, tid, new)
                self.note(tid, new)
        old = int(self.waves.get("n") or 1)
        print(f"[run] wave {old} turnover", flush=True)
        self.waves = W.turnover(m, self.waves, self.until, self.M["reports"], self.home, self.track_state)
        for tid in m.active_track_ids():  # a fresh start for every agent: new questions, the searches may run again
            ts = self.track_state.setdefault(tid, {})
            ts.update(saturated=False, low_rounds=0, last_scout=0)
        self.deficit = {}
        A.rebuild_playbook(m, self.steer.get("lessons", []))
        print(f"[run] wave {self.waves.get('n')} starts: {self.waves.get('plan_changes')}", flush=True)
        self.last["status"] = 0
        self.sync(f"wave {old} done")

    def close_run(self) -> None:
        """Before the final synthesis: close the last wave (chapter, knowledge, memory); a one-pass run tends memory once."""
        m = self.m
        t0 = time.time()
        while self.turning and time.time() - t0 < 900:  # a turnover in flight finishes first
            time.sleep(5)
        try:
            if self.wave_s and self.waves and not self.waves.get("closed"):
                self.waves = W.turnover(m, self.waves, self.until, self.M["reports"], self.home, self.track_state, final=True)
            elif not self.wave_s:
                cards = m.cards()[:20]
                text = "\n".join(f"- [{c['id']}] {c.get('title', '')[:100]}: {c.get('one_line', '')[:200]}" for c in cards)
                W.garden(m, "PAPERS READ IN THIS RUN:\n" + text, self.home)
        except Exception as e:
            self.err(f"close_run: {type(e).__name__}: {str(e)[:200]}")
            traceback.print_exc()

    # ------------------------------------------------------------ git, steering, status, digest
    def sync(self, why: str = "sync") -> bool:
        if not os.path.isdir(os.path.join(self.repo, ".git")):
            return True
        tok = os.environ.get("SABHA_GIT_TOKEN") or ""
        with self.git_lock:
            cwd = self.repo
            sh("git add -A", cwd)
            rc, out = sh(f'git commit -qm "box: {why} {now()}"', cwd)
            rc, out = sh("git fetch -q origin && git merge -q --no-edit origin/main", cwd)
            if rc != 0:
                self.err("git merge: " + (out[-200:].replace(tok, "***") if tok else out[-200:]))
                sh("git merge --abort", cwd)
            rc, out = sh("git push -q origin HEAD:main", cwd, timeout=180)
            if rc == 0:
                self.last_push_ok = time.time()
                return True
            self.err("git push: " + (out[-200:].replace(tok, "***") if tok else out[-200:]))
            return False

    def standing_steering(self) -> None:
        """Steering state lives in memory, so a restart (steering restart: true) would drop it. Re-read the
        lessons, priority topics and bans of the current steering.md at mission start; seq is not re-applied."""
        try:
            import yaml
            text = open(self.m.p("steering.md"), encoding="utf-8").read()
            mm = re.search(r"```ya?ml\n(.*?)```", text, re.S)
            d = yaml.safe_load(mm.group(1) if mm else text) or {}
        except Exception:
            return
        for k in ("priority_topics", "lessons"):
            self.steer[k] = list(dict.fromkeys(self.steer.get(k, []) + [str(x) for x in d.get(k) or []]))[-30:]
        for k, v in (d.get("banned") or {}).items():
            b = self.steer.setdefault("banned", {})
            b[k] = sorted(set(b.get(k, []) + list(v or [])))

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
                self.steer[k] = list(dict.fromkeys(self.steer.get(k, []) + [str(x) for x in d[k]]))[-30:]
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
        pl = llm.pool()  # role: healthy servers / all servers (local and other boxes)
        health = " ".join(f"{r}=>{llm.OVERRIDE[r]}" if r in llm.OVERRIDE else
                          f"{r}:{sum(e.ok() for e in pl.get(r) or [])}/{len(pl.get(r) or [])}" for r in llm.ROLES)
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
        L += inbox.status_lines(m)
        write(m.p("STATUS.md"), "\n".join(L) + "\n")

    def digest(self) -> None:
        m = self.m
        ev = read_jsonl(m.p("state", "events.jsonl"))
        line = open(m.p("state", "status_line.txt"), encoding="utf-8").read().strip() if os.path.exists(m.p("state", "status_line.txt")) else ""
        seq = len([e for e in ev if e.get("kind") == "digest"]) + 1
        L = [f"# DIGEST {m.id} seq {seq} {now()}", "", "## 1 Health", line, f"tokens/s {llm.speed()}  llm stats {llm.STATS}", f"pool {llm.pool_status()}",
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
            if inbox.poll(self):  # commands from the phone, every pass (~15 s); STATUS shows the result at once
                self.last["status"] = 0
            try:
                self.maybe_finalize()
                self.maybe_turnover()
            except Exception as e:
                self.err(f"turnover check: {type(e).__name__}: {str(e)[:160]}")
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
        while time.time() < self.until - self.M["loop"]:
            self.m = self.pick_mission()
            if not self.m and self.once:
                print("[run] no open mission left (--once): exit", flush=True)
                return 0
            if not self.m:
                time.sleep(60)
                continue
            self.stop = False
            m = self.m
            self.budget = src.Budget(m.p("state", "budget.json"), cap=int(self.params["tavily_cap"]))
            try:  # long-term memory: the owner's taste becomes an owner memory; the planner reads the closest memories
                longmem.seed_owner(self.home, m.taste, read_json(m.p("loves.json"), []) or [], source=m.id)
                W.refresh_memory_block(m, self.home)
            except Exception as e:
                self.err(f"long-term memory: {type(e).__name__}: {str(e)[:160]}")
            if not m.plan.get("tracks"):
                try:
                    A.plan_mission(m)
                except Exception as e:  # a brief the planner cannot turn into tracks (sent from the phone) must not stop the daemon
                    print(f"[run] mission {m.id}: planning failed, mission skipped: {type(e).__name__}: {str(e)[:200]}", flush=True)
                    write_json(m.p("state", "mission.json"), {"status": "done", "error": f"planning failed: {str(e)[:300]}", "ts": now()})
                    continue
            if getattr(self, "_last_mid", m.id) != m.id:  # a switch inside one process (phone command: mission, mode preempt)
                self.track_state, self.deficit, self.t_start = {}, {}, time.time()  # per-track state belonged to the old mission
                self.synth_draft_done = self.synth_final_done = self.final_reports_done = False
            self._last_mid = m.id
            self.setup_waves(m)
            self.queue_loved(m)
            self.set_status("running")
            live.bind(m.p("state", "live.json"), m.id)
            trace.bind(m.p("state", "trace.jsonl"))
            inbox.restore(self)
            self.standing_steering()
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
            while not self.stop and time.time() < self.until - self.M["end"]:
                time.sleep(20)
            if self.final_thread is not None and self.final_thread.is_alive():
                self.final_thread.join(timeout=1500)  # the answer is the point of the run: let it finish
            self.stop = True
            for th in threads:
                th.join(timeout=20 if self.synth_final_done else 600)  # after the answer only a scout may be left: do not wait on it
            self.status()
            self.digest()
            self.sync("final" if time.time() > self.until - self.M["loop"] else "mission switch")
            if self.restart:
                print("[run] restart requested by steering", flush=True)
                return 3
            if self.once:
                if read_json(m.p("state", "mission.json"), {}).get("status") != "done":
                    self.set_status("done")
                print(f"[run] mission {m.id} ended (--once): exit", flush=True)
                return 0
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
    ap.add_argument("--mission", default="", help="work on this mission id only")
    ap.add_argument("--once", action="store_true", help="exit when the mission ends instead of waiting for the next one")
    ap.add_argument("--wave-min", type=float, default=-1, help="minutes per wave (0 = one pass, -1 = the brief's wave_minutes)")
    a = ap.parse_args()
    until = a.until or time.time() + a.hours * 3600
    d = Daemon(a.repo, until, a.slots, a.mission, a.once, a.wave_min)
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
