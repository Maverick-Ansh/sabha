"""Research runs owned by the hub: start from the taste form, stop, status, download (PROTOCOL.md section 8).

Start writes missions/<NNN-slug>/{brief.md, taste.md, loves.json} in the research home and starts
three detached processes: the daemon (`python -m sabha.research.run --mission <id> --once`), the
arXiv harvester and the embedding index builder. Their pids live in <data>/run/research.json, so a
restarted hub finds a run that is still going. One run at a time. The hub never stops them when it
exits. In demo mode the daemon is `sabha.research.mock` and there is no harvester or index.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from .research import inbox as rinbox, longmem
from .research.memory import read_json, write, write_json
from .research.waves import auto_wave_minutes
from .util import ApiError, IS_WINDOWS, group_kwargs

log = logging.getLogger("sabha.research.run")

PKG_ROOT = str(Path(__file__).resolve().parent.parent)
SYNTHESIS_QUESTIONS = (
    "What is the direct answer to the question of this run, and how strong is the evidence for it?",
    "Which five to ten papers matter most for this question by the owner's taste, and what does each one show?",
    "Where do the papers disagree, and which popular claims did not hold up under the owner's standards?",
    "What is still unknown or untested?",
    "What should the owner read first, and what are three concrete next steps, one of them a small experiment?",
)
MARKS = {"daemon": ("sabha.research.run", "sabha.research.mock"), "harvest": ("sabha.research.harvest",),
         "index": ("sabha.research.index",)}
SKIP_IN_ZIP = ("scratch", ".git")
INSTANT_HOURS = 0.34       # one pass: plan 2-3 tracks, read what fits, answer (about 20 minutes)
SESSION_MARGIN_H = 0.25    # "until the session ends" stops this long before the box's 12 h clock runs out
DEMO_WAVE_S = 14           # demo mode: a continuous run turns a wave over every few seconds


def pid_alive(pid: int, marks: tuple[str, ...] = ()) -> bool:
    """True when pid is a live process (on Linux: one whose command line carries one of marks)."""
    if not pid or pid <= 0:
        return False
    if os.path.isdir("/proc"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return False
        return bool(cmd) and (not marks or any(m in cmd for m in marks))
    if IS_WINDOWS:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_pid(pid: int, grace: float = 8.0) -> None:
    """Stop a detached process and its group: SIGTERM, then SIGKILL after grace seconds."""
    if not pid_alive(pid):
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        t0 = time.time()
        while time.time() - t0 < grace:
            if not pid_alive(pid):
                return
            time.sleep(0.2)


def find_proc(mark: str) -> int:
    """A process started outside this hub (by hand on the box) whose command line carries mark. Linux only."""
    if not os.path.isdir("/proc"):
        return 0
    for name in os.listdir("/proc"):
        if name.isdigit() and int(name) != os.getpid() and pid_alive(int(name), (mark,)):
            return int(name)
    return 0


def build_mission(taste: dict) -> tuple[str, str, str]:
    """(title, brief.md body, taste.md) from the taste form. The brief uses the headings the planner reads."""
    topic = taste["topic"].strip()
    first = re.split(r"(?<=[.?!])\s|\n", topic, maxsplit=1)[0].strip()
    title = (first if len(first) <= 70 else first[:67].rsplit(" ", 1)[0] + "...") or "Research run"
    text = taste["taste"].strip()
    ids = [x for x in taste["loves"] if re.fullmatch(r"\d{4}\.\d{4,5}", x)]
    names = [x for x in taste["loves"] if x not in ids]
    L = [f"# {title}", "", "## The question", "", topic, ""]
    if text:
        L += ["## Research standards: the owner's taste", "",
              "The owner of this research wrote the lines below about their taste: what they care about, what good work "
              "looks like to them, what they distrust or skip. Plan the tracks, choose the papers, judge them and write for this person.",
              "", text, ""]
    if taste["loves"]:
        L += ["## Papers the owner loves", "", "Read these first. They show what good work looks like to the owner.", ""]
        L += [f"- arXiv:{x}" for x in ids] + [f"- {x}" for x in names] + [""]
    L += ["# Synthesis", "", "At the end, answer these from the evidence:", ""]
    L += [f"{i}. {q}" for i, q in enumerate(SYNTHESIS_QUESTIONS, 1)]
    md = ""
    if text or taste["loves"]:
        md = (text + "\n" if text else "") + ("\nPapers the owner loves: " + ", ".join(taste["loves"]) + "\n" if taste["loves"] else "")
    return title, "\n".join(L) + "\n", md


class Runner:
    def __init__(self, hub, repo: str):
        self.hub = hub
        self.repo = repo
        self.data = Path(hub.cfg.data_dir)
        self.state_path = self.data / "run" / "research.json"
        self.logs = self.data / "logs"
        self.procs: dict[str, subprocess.Popen] = {}  # children of this hub process (polled, so they never stay zombies)

    # ------------------------------------------------------------ state

    def _state(self) -> dict:
        d = read_json(str(self.state_path), {})
        return d if isinstance(d, dict) else {}

    def _alive(self, name: str, pid: int) -> bool:
        p = self.procs.get(name)
        if p is not None and p.pid == pid:
            if p.poll() is None:
                return True
            self.procs.pop(name, None)
            return False
        return pid_alive(pid, MARKS[name])

    def status(self) -> dict:
        """What the Research tab needs to draw Start / Stop. Cleans up after a daemon that ended on its own."""
        st = self._state()
        pids = st.get("pids") or {}
        running = bool(pids.get("daemon")) and self._alive("daemon", int(pids["daemon"]))
        if st and not running and not st.get("ended"):
            self._stop_helpers(pids)
            mj = read_json(os.path.join(self.repo, "missions", str(st.get("mission")), "state", "mission.json"), {}) or {}
            st.update(ended=time.time(), how=st.get("how") or ("finished" if mj.get("status") == "done" else "the daemon exited"))
            if mj.get("status") != "done":  # a crashed daemon must not leave a mission that looks alive
                self._mark_mission(st.get("mission"), "stopped", st["how"])
            write_json(str(self.state_path), st)
        return {"running": running, "mission": st.get("mission") or "", "started": st.get("started") or 0,
                "until": st.get("until") or 0, "hours": st.get("hours") or 0, "mock": bool(st.get("mock")),
                "mode": st.get("mode") or "", "wave_min": st.get("wave_min") or 0,
                "finishing": bool(running and st.get("finishing")), "ended": st.get("ended") or 0, "how": st.get("how") or "",
                "git_sync": os.path.isdir(os.path.join(self.repo, ".git")),
                "log": str(self.logs / "research.log")}

    def _mark_mission(self, mid, status: str, why: str) -> None:
        if not mid:
            return
        p = os.path.join(self.repo, "missions", str(mid), "state", "mission.json")
        mj = read_json(p, {}) or {}
        mj.update(status="done", stopped=status != "done", note=why, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        try:
            write_json(p, mj)
        except OSError:
            pass

    def _stop_helpers(self, pids: dict) -> None:
        for name in ("harvest", "index"):
            pid = int(pids.get(name) or 0)
            if pid and self._alive(name, pid):
                kill_pid(pid, 3)
            self.procs.pop(name, None)

    # ------------------------------------------------------------ start

    def _spawn(self, name: str, args: list[str], env: dict, log_name: str) -> int:
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / log_name, "ab") as f:
            p = subprocess.Popen([sys.executable, *args], cwd=PKG_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=f,
                                 stderr=subprocess.STDOUT, **group_kwargs())
        self.procs[name] = p
        return p.pid

    def _served(self) -> set[str]:
        return {(m.get("served_name") or "") for m in self.hub.store.models.values()
                if m.get("status") == "ready" and m.get("base_url")}

    def _git_setup(self) -> str:
        """Make the research home a git checkout when a repo and token are set. Returns a note for the UI."""
        s = self.hub.settings
        repo, token = s.get("git_repo"), s.get("git_token")
        if os.path.isdir(os.path.join(self.repo, ".git")):
            return "git sync on"
        if not (repo and token):
            return ""
        url = f"https://x-access-token:{token}@github.com/{repo}.git"

        def git(*a, timeout=90):
            r = subprocess.run(["git", *a], cwd=self.repo, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr).replace(token, "***")[-300:]
        try:
            os.makedirs(self.repo, exist_ok=True)
            git("init", "-q")
            git("config", "user.email", "sabha@box")
            git("config", "user.name", "Sabha box")
            git("remote", "add", "origin", url)
            write(os.path.join(self.repo, ".gitignore"), "scratch/\n*.tmp\nstate/live.json\n")
            rc, _ = git("fetch", "-q", "origin", "main")
            if rc == 0:  # the repo already has results: build on them
                git("checkout", "-q", "-B", "main")
                git("reset", "-q", "--mixed", "origin/main")
            else:
                git("checkout", "-q", "-B", "main")
            return f"git sync on: {repo}, pushed every 10 minutes"
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"git sync could not be set up ({type(e).__name__}); results stay on the box"

    def start(self, body: dict) -> dict:
        """Blocking (git, process start): call through asyncio.to_thread."""
        if self.status()["running"]:
            raise ApiError(409, "a research run is already going: stop it first")
        hub = self.hub
        fields = {k: body[k] for k in ("taste", "topic", "loves", "hours", "mode", "wave_min") if k in body}
        taste = hub.taste.save(fields) if fields else hub.taste.load()
        if len(taste["topic"].strip()) < 12:
            raise ApiError(400, "write the question or topic of this run first (a sentence or more)")
        demo = bool(hub.cfg.demo)
        served = self._served()
        if not demo and not ({"deep", "reader"} & served):
            raise ApiError(409, "no research model is ready yet: wait until the boot strip shows a model as ready "
                                "(research uses the models served as 'reader' and 'deep')")
        note = "" if demo else self._git_setup()
        title, brief, taste_md = build_mission(taste)
        hours, wave_min, extra = self.run_shape(taste)
        mid = rinbox.new_mission(self.repo, title, brief, "queue", hours, extra)
        mroot = os.path.join(self.repo, "missions", mid)
        if taste_md:
            write(os.path.join(mroot, "taste.md"), taste_md)
        write_json(os.path.join(mroot, "loves.json"), [x for x in taste["loves"] if re.fullmatch(r"\d{4}\.\d{4,5}", x)])

        env = dict(os.environ)
        env.update(PYTHONUNBUFFERED="1", PYTHONPATH=PKG_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""))
        env.setdefault("SABHA_ARXIV_META", str(self.data / "arxiv_meta"))
        env.setdefault("SABHA_ARXIV_INDEX", str(self.data / "arxiv_index"))
        env.setdefault("SABHA_INDEX_SLEEP", "3")
        if hub.settings.get("git_token"):
            env["SABHA_GIT_TOKEN"] = hub.settings.get("git_token")  # only so the daemon can mask it in its logs
        for mdl in hub.store.models.values():  # the context window of each role's server (a switched model may differ)
            role = (mdl.get("served_name") or "").lower()
            if role in ("deep", "reader", "s1") and mdl.get("context_window"):
                env[f"SABHA_CTX_{role.upper()}"] = str(int(mdl["context_window"]))
        pids: dict[str, int] = {}
        if demo:
            pids["daemon"] = self._spawn("daemon", ["-m", "sabha.research.mock", "--repo", self.repo, "--mission", mid,
                                                    "--hours", str(hours), "--wave-s", str(DEMO_WAVE_S if wave_min else 0)],
                                         env, "research.log")
        else:
            if "s1" not in served:
                env.setdefault("SABHA_S1", "reader" if "reader" in served else "deep")
            if "deep" not in served:
                env.setdefault("SABHA_DEEP", "reader")
            if "reader" not in served:
                env.setdefault("SABHA_READER", "deep")
            boot = read_json(str(self.data / "boot.json"), {}) or {}
            n_gpu = len(boot.get("gpus") or [])
            device = os.environ.get("SABHA_INDEX_DEVICE") or ("cuda:1" if n_gpu >= 2 else "cuda:0" if n_gpu == 1 else "cpu")
            if not find_proc("sabha.research.harvest"):
                pids["harvest"] = self._spawn("harvest", ["-m", "sabha.research.harvest", "--out", env["SABHA_ARXIV_META"],
                                                         "--set", "cs"], env, "harvest.log")
            if not find_proc("sabha.research.index"):
                pids["index"] = self._spawn("index", ["-m", "sabha.research.index", "build", "--device", device], env, "index.log")
            pids["daemon"] = self._spawn("daemon", ["-m", "sabha.research.run", "--repo", self.repo, "--mission", mid,
                                                    "--hours", f"{hours:.3f}", "--once", "--wave-min", f"{wave_min:g}"],
                                         env, "research.log")
        now = time.time()
        write_json(str(self.state_path), {"mission": mid, "started": now, "until": now + hours * 3600, "hours": hours,
                                          "pids": pids, "mock": demo, "note": note, "mode": extra["run"], "wave_min": wave_min})
        log.info("research run started: mission %s, %.1f h, pids %s", mid, hours, pids)
        return {**self.status(), "note": note, "title": title}

    def run_shape(self, taste: dict) -> tuple[float, float, dict]:
        """(hours, minutes per wave, brief front matter) from the run-length choice. Instant: one short pass with few
        tracks. Continuous: the hours asked for (0 = until the session ends), cut into waves."""
        if taste.get("mode") == "instant":
            return INSTANT_HOURS, 0.0, {"run": "instant", "tracks": 3, "wave_minutes": 0}
        hours = float(taste.get("hours") or 0)
        left = (getattr(self.hub, "session_end", 0) - time.time()) / 3600 - SESSION_MARGIN_H
        if hours <= 0:
            hours = max(left, 0.5) if left > 0 else 6.0
        elif left > 0.5:
            hours = min(hours, left)
        wave = float(taste.get("wave_min") or 0) or auto_wave_minutes(hours)
        if wave and wave * 60 > hours * 3600 * 0.6:  # a wave longer than most of the run is one pass
            wave = 0.0
        return round(hours, 3), wave, {"run": "continuous" if wave else "one pass", "wave_minutes": f"{wave:g}"}

    # ------------------------------------------------------------ stop

    def stop(self, mode: str = "now") -> dict:
        """mode finish: the daemon writes the reports and the synthesis with what it has, then ends on its own.
        mode now: everything stops at once. Blocking: call through asyncio.to_thread."""
        if mode not in ("now", "finish"):
            raise ApiError(400, "mode must be now or finish")
        st = self._state()
        pids = st.get("pids") or {}
        pid = int(pids.get("daemon") or 0)
        if not (pid and self._alive("daemon", pid)):
            return self.status()
        mroot = os.path.join(self.repo, "missions", str(st.get("mission")))
        if mode == "finish":
            rinbox.submit(mroot, rinbox.clean({"cmd": "finish", "by": "hub"}, []))
            st["finishing"] = True
            write_json(str(self.state_path), st)
            return self.status()
        kill_pid(pid)
        self.procs.pop("daemon", None)
        self._stop_helpers(pids)
        self._mark_mission(st.get("mission"), "stopped", "stopped from the app")
        st.update(ended=time.time(), how="stopped from the app", finishing=False)
        write_json(str(self.state_path), st)
        return self.status()

    # ------------------------------------------------------------ download

    def export(self, mission_id: str) -> tuple[str, bytes]:
        """(file name, zip bytes) of one mission: brief, taste, plan, cards, track reports, ledgers, synthesis."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,80}", mission_id or ""):
            raise ApiError(404, "no such mission")
        root = os.path.join(self.repo, "missions", mission_id)
        if not os.path.isfile(os.path.join(root, "brief.md")):
            raise ApiError(404, "no such mission")
        buf = io.BytesIO()
        n = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{mission_id}/README.txt",
                       "Sabha research results.\n\nsynthesis.md        the answer, written at half time and at the end\n"
                       "tracks/<t>/report.md  one report per agent (track)\ntracks/<t>/digest.md  what the agent believes, from its claims ledger\n"
                       "cards/<id>.md       one card per paper read, every fact with a checked quote\n"
                       "REPORT.md           the living report: where we stand, one chapter per wave, the answer\n"
                       "knowledge.md        the compact state of knowledge every agent read at each step\n"
                       "waves/wNN.md        each wave's chapter\n"
                       "longterm-memory/    what the agents keep for later runs (one file per memory)\n"
                       "STATUS.md, DIGEST.md  the run's own status pages\nbrief.md, taste.md  what you asked for\n"
                       "memory/, state/     registry of every paper seen, events, queries (JSON lines)\n")
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_IN_ZIP]
                for name in sorted(filenames):
                    if name.endswith(".tmp") or name in ("live.json", "trace.jsonl"):  # the trace is for the live view
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        z.write(full, f"{mission_id}/" + os.path.relpath(full, root).replace(os.sep, "/"))
                        n += 1
                    except OSError:
                        pass
            home = longmem.home_for(self.repo)
            for p in sorted(Path(home).glob("*.md")) if os.path.isdir(home) else []:
                try:
                    z.write(str(p), f"{mission_id}/longterm-memory/{p.name}")
                    n += 1
                except OSError:
                    pass
        log.info("export %s: %d files, %d bytes", mission_id, n, buf.tell())
        return f"sabha-research-{mission_id}.zip", buf.getvalue()


def read_boot(data_dir: Path) -> dict:
    """<data>/boot.json for GET /api/boot. No file means this hub was not started by the box launcher."""
    d = read_json(str(Path(data_dir) / "boot.json"), None)
    if not isinstance(d, dict):
        return {"present": False, "ready": True, "steps": [], "note": ""}
    age = time.time() - float(d.get("epoch") or 0)
    return {"present": True, "ready": bool(d.get("ready")), "stage": d.get("stage") or "", "note": d.get("note") or "",
            "alive": age < 90, "age_s": int(min(age, 1e9)), "gpus": d.get("gpus") or [],
            "steps": [{k: s.get(k) for k in ("id", "short", "label", "status", "detail", "secs", "expect_s")}
                      for s in d.get("steps") or [] if isinstance(s, dict)]}
