"""Bring up a whole Sabha box from one notebook cell (Kaggle or Colab). Standard library only.

    python sabha/box.py up          what the notebook cell runs (through runpy)
    python sabha/box.py supervise   detached helper started by `up`
    python sabha/box.py status      one status line and the link
    python sabha/box.py stop        stop the hub, the supervisor and the model servers

`up` checks the GPUs, starts the hub with its tunnel (detached) and the supervisor (detached),
prints the link, then prints one status line every few minutes so Kaggle does not stop an idle
kernel. Running it again starts nothing twice: every process is looked up in /proc first.

`supervise` installs the tools (scripts/box_setup.sh) while the model weights download, starts one
vLLM server per model outside the hub (sabha/research/serve.sh), registers the models and a default
crew in the hub, and writes <data>/boot.json, which the app shows as the boot strip (GET /api/boot).

Environment (all optional)
    SABHA_KEEPALIVE=0      `up` returns as soon as the link is printed (no heartbeat loop)
    SABHA_NEW_LINK=1       restart only the hub, for a fresh tunnel link (models keep running)
    SABHA_MODELS=0         start no model servers (hub only)
    SABHA_DEEP_MODEL, SABHA_READER_MODEL    Hugging Face ids of the two models
    SABHA_BASE, SABHA_DATA, SABHA_PORT, SABHA_TUNNEL (cloudflared | ngrok | none), SABHA_TOKEN
    TAVILY_API_KEY, HF_TOKEN, SABHA_GIT_REPO, SABHA_GIT_TOKEN    also read from Kaggle / Colab secrets;
                           they fill empty fields of <data>/settings.json (the app can change them later)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _base() -> str:
    if os.environ.get("SABHA_BASE"):
        return os.environ["SABHA_BASE"]
    for d in ("/kaggle/working", "/content"):
        if os.path.isdir(d):
            return d
    return os.path.expanduser("~")


BASE = _base()
DATA = os.environ.get("SABHA_DATA") or os.path.join(BASE, "sabha_data")
LOGS = os.path.join(DATA, "logs")
RUN = os.path.join(DATA, "run")
PORT = int(os.environ.get("SABHA_PORT") or 8787)
TUNNEL = os.environ.get("SABHA_TUNNEL") or "cloudflared"
BOOT = os.path.join(DATA, "boot.json")
SETTINGS = os.path.join(DATA, "settings.json")
HUB_MARK = f"-m sabha --host 127.0.0.1 --port {PORT} "
SUP_MARK = "box.py supervise"
SESSION_S = 11.5 * 3600
BEAT_S = 240
SECRET_NAMES = {"TAVILY_API_KEY": "tavily_key", "HF_TOKEN": "hf_token",
                "SABHA_GIT_REPO": "git_repo", "SABHA_GIT_TOKEN": "git_token"}
NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
LOG_PREFIX = re.compile(r"^(\(\w+ pid=\d+\)\s*)?((INFO|WARNING|ERROR|DEBUG)\s+[\d\-]+\s+[\d:]+\s*)?(\[[^\]]*\]\s*)?")
CREW = [  # name, role, preferred model role, hue, persona
    ("Lead", "chairs the council and keeps it on the question", "deep", "sky",
     "You weigh what the others say and look for the one thing that decides the question."),
    ("Scout", "reads fast and reports what is there", "reader", "mint",
     "You report exactly what the text says, with numbers, and nothing more."),
    ("Critic", "doubts every claim until it sees the evidence", "deep", "rose",
     "You are a sceptical reviewer. You look for missing controls, weak baselines and claims bigger than the evidence."),
    ("Builder", "asks how you would reproduce it", "reader", "sun",
     "You are an engineer who would have to reproduce this work. You care about what is specified well enough to build."),
]


def say(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- small helpers


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str, obj, mode: int | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def tail(path: str, n: int = 2000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(size - n, 0))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def find(marker: str) -> int:
    """Pid of a live process whose command line contains marker (0 = none). Zombies have no command line."""
    me = os.getpid()
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return 0
    for pid in sorted(pids):
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace") + " "
        except OSError:
            continue
        if marker in cmd:
            return pid
    return 0


def spawn(args: list[str], log: str, env: dict | None = None, fresh_log: bool = False) -> int:
    """Start a detached process (its own session) with output in a log file."""
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "wb" if fresh_log else "ab") as f:
        p = subprocess.Popen(args, cwd=APP, env=env or box_env(), stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT, start_new_session=True)
    return p.pid


def kill_group(pid: int, grace: float = 8.0) -> None:
    if not pid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        t0 = time.time()
        while time.time() - t0 < grace:
            if not os.path.exists(f"/proc/{pid}/cmdline") or not tail(f"/proc/{pid}/cmdline", 10):
                return
            time.sleep(0.3)


def http_json(method: str, url: str, body=None, token: str = "", timeout: float = 8):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def port_up(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3).read()
        return True
    except Exception:
        return False


def get_token() -> str:
    if os.environ.get("SABHA_TOKEN"):
        return os.environ["SABHA_TOKEN"]
    path = os.path.join(DATA, ".token")
    try:
        tok = open(path, encoding="utf-8").read().strip()
    except OSError:
        tok = ""
    if not tok:
        tok = secrets.token_urlsafe(18)
        os.makedirs(DATA, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(tok)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return tok


def box_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = "/usr/local/bin:" + env.get("PATH", "")  # Node 24, dsh and the vllm link live there
    env.update(SABHA_TOKEN=get_token(), PYTHONUNBUFFERED="1", SABHA_LOGS=LOGS)
    hf = (read_json(SETTINGS, {}) or {}).get("hf_token") or ""
    if hf and not env.get("HF_TOKEN"):
        env["HF_TOKEN"] = hf
    return env


def secret(name: str) -> str:
    """An optional value: environment first, then Kaggle Secrets, then Colab secrets. Never raises."""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    try:
        from kaggle_secrets import UserSecretsClient  # type: ignore
        return (UserSecretsClient().get_secret(name) or "").strip()
    except Exception:
        pass
    try:
        from google.colab import userdata  # type: ignore
        return (userdata.get(name) or "").strip()
    except Exception:
        return ""


def import_secrets() -> list[str]:
    """Copy optional secrets into empty fields of settings.json. Returns the names that were filled."""
    cur = read_json(SETTINGS, {}) or {}
    got = []
    for name, field in SECRET_NAMES.items():
        if cur.get(field):
            continue
        v = secret(name)
        if v:
            cur[field] = v
            got.append(name)
    if got:
        write_json(SETTINGS, cur, 0o600)
    return got


# ---------------------------------------------------------------- GPUs and the model layout


def gpus() -> list[dict]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 3 and p[0].isdigit():
            try:
                rows.append({"index": int(p[0]), "name": p[1], "mem_mb": int(float(p[2]))})
            except ValueError:
                pass
    return rows


def layout(g: list[dict]) -> tuple[list[dict], str]:
    """(models to serve, problem). Measured on Kaggle 2x T4: the 9B AWQ needs a GPU to itself. Context 32768 for both:
    a Work run (dsh system prompt + fetched pages) filled a 20480 window by its third step; both servers start at 32768."""
    if os.environ.get("SABHA_MODELS", "1") == "0":
        return [], ""
    if not g:
        return [], ("No GPU found. In the notebook: Settings > Accelerator > GPU T4 x2, then run the cell again. "
                    "(To start only the hub, for models behind an API, set SABHA_MODELS=0.)")
    if any("P100" in x["name"] for x in g):
        return [], ("This session has a P100, which the model server (vLLM) no longer supports. "
                    "Settings > Accelerator > GPU T4 x2, then run the cell again.")
    ok = [x for x in g if x["mem_mb"] >= 14000]
    if not ok:
        return [], f"The GPU has {g[0]['mem_mb']} MB of memory; the models need a 15 GB card (a T4 or larger)."
    reader = {"role": "reader", "name": "Qwen3.5-4B (reader)", "hf": os.environ.get("SABHA_READER_MODEL") or "Qwen/Qwen3.5-4B",
              "port": 8002, "util": "0.85", "ctx": int(os.environ.get("SABHA_READER_CTX") or 32768), "seqs": 8}
    if len(ok) >= 2:
        deep = {"role": "deep", "name": "Qwen3.5-9B 4-bit (deep)",
                "hf": os.environ.get("SABHA_DEEP_MODEL") or "cyankiwi/Qwen3.5-9B-AWQ-4bit", "gpu": ok[0]["index"],
                "port": 8001, "util": "0.90", "ctx": int(os.environ.get("SABHA_DEEP_CTX") or 32768), "seqs": 8}
        reader["gpu"] = ok[1]["index"]
        return [deep, reader], ""
    reader["gpu"] = ok[0]["index"]
    return [reader], ""


# ---------------------------------------------------------------- up (the notebook cell)


def hub_link() -> str:
    m = re.search(r"^Sabha link: (\S+)", tail(os.path.join(LOGS, "hub.log"), 200000), re.M)
    return m.group(1) if m else ""


def box_uptime() -> float:
    """Seconds since the box booted (Kaggle's 12 h clock starts then), 0 when unknown or implausible."""
    try:
        up_s = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    return up_s if 0 < up_s < 12 * 3600 else 0.0


def session_start() -> float:
    try:
        return float(open(os.path.join(RUN, "session.started"), encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return time.time()


def start_hub() -> str:
    """Start the hub and wait for its link (at most about 2.5 minutes, one retry if the tunnel fails)."""
    link = ""
    for attempt in (1, 2):
        left_h = max((session_start() + 12 * 3600 - time.time()) / 3600, 0.5)  # the app's countdown: what is left of 12 h
        pid = spawn([sys.executable, "-m", "sabha", "--host", "127.0.0.1", "--port", str(PORT), "--data", DATA,
                     "--tunnel", TUNNEL, "--session-hours", f"{left_h:.2f}"], os.path.join(LOGS, "hub.log"), fresh_log=True)
        for _ in range(75):
            time.sleep(1)
            link = hub_link()
            if link or not find(HUB_MARK):
                break
        if link and (TUNNEL == "none" or not link.startswith("http://127.0.0.1")):
            return link
        if attempt == 1 and link:  # the hub is up but the tunnel is not: one more try
            say("The tunnel did not come up. Trying once more...")
            kill_group(pid)
            time.sleep(2)
    return link


def status_line() -> str:
    b = read_json(BOOT, {}) or {}
    bits = []
    for s in b.get("steps") or []:
        st = s.get("status")
        if s.get("id") == "tools" and st == "done":
            continue
        mins = f" {int((s.get('secs') or 0) // 60)} min" if st in ("running", "downloading", "starting") else ""
        bits.append(f"{s.get('short') or s.get('id')}: {st}{mins}")
    if b.get("note"):
        bits.append(b["note"][:100])
    try:
        r = http_json("GET", f"http://127.0.0.1:{PORT}/api/research", token=get_token(), timeout=6)
        m = (r or {}).get("mission") or {}
        run = (r or {}).get("run") or {}
        if run.get("running") and m:
            st = m.get("stats") or {}
            bits.append(f"research: {st.get('cards', 0)} cards, {st.get('queued', 0)} queued")
    except Exception:
        if not find(HUB_MARK):
            bits.append("HUB NOT RUNNING (run the cell again)")
    return " | ".join(bits) or "starting"


def up() -> str:
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(RUN, exist_ok=True)
    g = gpus()
    models, problem = layout(g)
    if problem:
        say(problem)
        return ""
    if not find(HUB_MARK) and not find(SUP_MARK):  # nothing of ours is running: the session started when the box booted
        with open(os.path.join(RUN, "session.started"), "w", encoding="utf-8") as f:
            f.write(str(int(time.time() - box_uptime())))
        try:
            os.remove(BOOT)
        except OSError:
            pass
    say("Sabha box: " + (", ".join(f"GPU {x['index']} {x['name']}" for x in g) or "no GPU") +
        (" | models: " + ", ".join(m["name"] for m in models) if models else " | no model servers"))
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        say("Installing aiohttp (the hub's only dependency)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "aiohttp>=3.9"], check=False)
    got = import_secrets()
    if got:
        say("Optional settings found: " + ", ".join(got))

    hub = find(HUB_MARK)
    dead_link = bool(hub) and TUNNEL != "none" and hub_link().startswith("http://127.0.0.1")  # the tunnel failed last time
    if hub and (os.environ.get("SABHA_NEW_LINK") == "1" or dead_link):
        say("Restarting the hub for a new link (models and research keep running)...")
        kill_group(hub)
        hub = 0
    if hub:
        link = hub_link()
        say("The hub is already running.")
    else:
        say("Starting the hub and the tunnel (about a minute)...")
        link = start_hub()
    if not link:
        say("The hub did not start. Log tail:\n" + tail(os.path.join(LOGS, "hub.log")))
        return ""
    with open(os.path.join(DATA, "link.txt"), "w", encoding="utf-8") as f:
        f.write(link + "\n")
    if link.startswith("http://127.0.0.1") and TUNNEL != "none":
        say("WARNING: no tunnel. This link works only on the box itself. Run the cell again in a minute.")

    if find(SUP_MARK):
        say("The box supervisor is already running.")
    else:
        spawn([sys.executable, os.path.join(APP, "sabha", "box.py"), "supervise"], os.path.join(LOGS, "boot.log"))
        say("Setting up in the background: tools about 5 min, models ready about 15 to 20 min from now.")
    say("")
    say("Open Sabha:  " + link)
    say("Keep the link private: whoever has it controls this box. The app shows the setup progress.")
    say("")

    if os.environ.get("SABHA_KEEPALIVE", "1") == "0":
        return link
    until = session_start() + SESSION_S
    say(f"This cell now prints a status line every {BEAT_S // 60} minutes so the session stays alive "
        f"(until {time.strftime('%H:%M', time.gmtime(until))} UTC). Stopping the cell stops nothing else.")
    n = 0
    try:
        while time.time() < until:
            time.sleep(BEAT_S)
            n += 1
            say(f"[{time.strftime('%H:%M', time.gmtime())} UTC] {status_line()}")
            if n % 8 == 0:
                say("Open Sabha:  " + (hub_link() or link))
    except KeyboardInterrupt:
        say("Heartbeat stopped. The hub, the models and research keep running. Run the cell again to resume it.")
        return link
    say("The session is about to end. Download your research results from the Research tab now.")
    return link


# ---------------------------------------------------------------- supervise (detached)


class Boot:
    """boot.json: what the app shows while the box comes up."""

    def __init__(self, g: list[dict], models: list[dict]):
        self.lock = threading.Lock()
        self.d = {"pid": os.getpid(), "started": time.time(), "stage": "setup", "ready": False, "note": "", "gpus": g,
                  "steps": [{"id": "tools", "short": "tools", "label": "Agent tools: Node 24, dsh, vLLM", "status": "running",
                             "detail": "", "since": time.time(), "secs": 0, "expect_s": 300}] +
                           [{"id": "model:" + m["role"], "short": m["role"], "label": f"{m['name']} on GPU {m['gpu']}",
                             "status": "waiting", "detail": "waits for the tools", "since": time.time(), "secs": 0,
                             "expect_s": 600, "model": m["hf"], "port": m["port"]} for m in models]}
        self.save()

    def step(self, sid: str, **fields) -> None:
        with self.lock:
            for s in self.d["steps"]:
                if s["id"] == sid:
                    if "status" in fields and fields["status"] != s["status"]:
                        s["since"] = time.time()
                    s.update(fields)

    def get(self, sid: str) -> dict:
        return next((s for s in self.d["steps"] if s["id"] == sid), {})

    def save(self) -> None:
        with self.lock:
            t = time.time()
            for s in self.d["steps"]:
                if s["status"] not in ("done", "ready", "failed"):
                    s["secs"] = int(t - s["since"])
            self.d["epoch"] = round(t, 1)
            self.d["ready"] = all(s["status"] in ("done", "ready") for s in self.d["steps"])
            if self.d["ready"]:
                self.d["stage"] = "ready"
            try:
                write_json(BOOT, self.d)
            except OSError:
                pass


def run_setup(boot: Boot, want_vllm: bool) -> bool:
    cmd = ["bash", os.path.join(APP, "scripts", "box_setup.sh")] + ([] if want_vllm else ["--no-vllm"])
    last = ""
    with open(os.path.join(LOGS, "setup.log"), "a", encoding="utf-8") as logf:
        p = subprocess.Popen(cmd, cwd=APP, env=box_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            logf.write(line)
            logf.flush()
            line = line.strip()
            if line:
                last = line
                boot.step("tools", detail=re.sub(r"^\[setup [\d:]+\]\s*", "", line)[:160])
                boot.save()
        p.wait()
    ok = last.startswith("SETUP_OK")
    boot.step("tools", status="done" if ok else "failed", detail="installed" if ok else last[:200])
    boot.save()
    return ok


def cache_gb(hf: str) -> float:
    root = os.path.join(os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"), "hub",
                        "models--" + hf.replace("/", "--"), "blobs")
    total = 0
    try:
        for name in os.listdir(root):
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    except OSError:
        pass
    return total / 2**30


def prefetch(boot: Boot, m: dict, done: threading.Event) -> None:
    """Download the weights with the notebook's own huggingface_hub while the tools install (best effort:
    vLLM downloads whatever is missing when it starts)."""
    sid = "model:" + m["role"]
    try:
        if port_up(m["port"]):
            return
        boot.step(sid, status="downloading", detail="downloading the weights")
        code = "import sys; from huggingface_hub import snapshot_download; snapshot_download(sys.argv[1])"
        with open(os.path.join(LOGS, f"fetch_{m['role']}.log"), "ab") as logf:
            p = subprocess.Popen([sys.executable, "-c", code, m["hf"]], env=box_env(), stdout=logf, stderr=subprocess.STDOUT)
            t0 = time.time()
            while p.poll() is None and time.time() - t0 < 1500:
                boot.step(sid, detail=f"downloading the weights: {cache_gb(m['hf']):.1f} GB so far")
                time.sleep(4)
            if p.poll() is None:
                p.kill()
        boot.step(sid, detail=f"weights on disk ({cache_gb(m['hf']):.1f} GB)" if p.returncode == 0
                  else "the model server will download the weights itself")
    except Exception as e:
        boot.step(sid, detail=f"download helper failed ({type(e).__name__}); the model server downloads the weights itself")
    finally:
        done.set()


def start_server(m: dict) -> None:
    role = m["role"]
    if find(f"serve.sh {role} ") or port_up(m["port"]):
        return
    spawn(["bash", os.path.join(APP, "sabha", "research", "serve.sh"), role, str(m["gpu"]), str(m["port"]), m["hf"],
           m["util"], str(m["ctx"]), str(m["seqs"])], os.path.join(LOGS, f"{role}.serve.log"))


def last_log_line(role: str) -> str:
    """The newest line of the vLLM log, without colour codes and the "(APIServer pid=1) INFO 11-16 12:00:00 [x.py:1]" prefix."""
    for line in reversed(tail(os.path.join(LOGS, f"{role}.vllm.log"), 3000).splitlines()):
        line = ANSI.sub("", line).strip()
        line = LOG_PREFIX.sub("", line).strip() or line
        if len(line) > 12:
            return line[:150]
    return ""


def register(models: list[dict], token: str) -> bool:
    """Models and a default crew in the hub, once. A model that is still loading shows as down until it answers."""
    hub = f"http://127.0.0.1:{PORT}"
    state = http_json("GET", hub + "/api/state", token=token)
    have = {(x.get("served_name"), (x.get("base_url") or "").rstrip("/")): x for x in state.get("models") or []}
    ids = {}
    for m in models:
        url = f"http://127.0.0.1:{m['port']}/v1"
        x = have.get((m["role"], url)) or http_json("POST", hub + "/api/models", {
            "name": m["name"], "kind": "chat", "base_url": url, "served_name": m["role"], "context_window": m["ctx"],
            "extra_body": NO_THINK, "notes": f"{m['hf']} on GPU {m['gpu']}, started by the box launcher"}, token=token)
        if x.get("context_window") != m["ctx"]:  # registered by an older launcher: the server now has another window
            x = http_json("PATCH", f"{hub}/api/models/{x['id']}", {"context_window": m["ctx"]}, token=token)
        ids[m["role"]] = x["id"]
    if ids and not state.get("agents"):
        for name, role, want, hue, persona in CREW:
            mid = ids.get(want) or next(iter(ids.values()))
            http_json("POST", hub + "/api/agents", {"name": name, "role": role, "model_id": mid, "harness": "dsh",
                                                   "hue": hue, "persona": persona}, token=token)
    return True


def supervise() -> int:
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(RUN, exist_ok=True)
    if find(SUP_MARK):
        say("a supervisor is already running")
        return 0
    token = get_token()
    g = gpus()
    models, problem = layout(g)
    boot = Boot(g, models)
    if problem:
        boot.d["note"] = problem
    say(f"[boot] gpus {g} models {[m['role'] for m in models]}")

    fetched = {m["role"]: threading.Event() for m in models}
    for m in models:
        threading.Thread(target=prefetch, args=(boot, m, fetched[m["role"]]), daemon=True).start()
    setup_ok: list[bool] = []
    threading.Thread(target=lambda: setup_ok.append(run_setup(boot, bool(models))), daemon=True).start()

    registered, launched, t_end = False, set(), time.time() + 12 * 3600
    while time.time() < t_end:
        if not registered:
            try:
                registered = register(models, token)
                say("[boot] models and crew registered in the hub")
            except Exception as e:
                say(f"[boot] hub not ready for registration: {type(e).__name__}: {str(e)[:120]}")
        if setup_ok and not setup_ok[0]:
            boot.d["stage"] = "failed"
            boot.d["note"] = "The tools did not install. Run the notebook cell again to retry. Details: " + boot.get("tools").get("detail", "")
            for m in models:
                if not port_up(m["port"]):
                    boot.step("model:" + m["role"], status="failed", detail="the tools did not install")
            boot.save()
            say("[boot] setup failed: " + boot.get("tools").get("detail", ""))
            return 1
        for m in models:
            sid, role = "model:" + m["role"], m["role"]
            if port_up(m["port"]):
                if boot.get(sid).get("status") != "ready":
                    boot.step(sid, status="ready", detail="ready")
                    say(f"[boot] {role} is ready")
                continue
            if not setup_ok or not fetched[role].is_set():
                continue
            if role not in launched or not find(f"serve.sh {role} "):
                start_server(m)
                launched.add(role)
                boot.step(sid, status="starting", detail="starting the model server")
                boot.d["stage"] = "models"
                continue
            was_ready = boot.get(sid).get("status") == "ready"
            boot.step(sid, status="starting", detail=("stopped answering; restarting. " if was_ready else "") +
                      (last_log_line(role) or "loading the weights, then warming up (about 10 min the first time)"))
        boot.save()
        time.sleep(5 if not boot.d["ready"] else 30)
    return 0


# ---------------------------------------------------------------- status / stop


def stop() -> None:
    for name, marker in (("supervisor", SUP_MARK), ("hub", HUB_MARK)):
        pid = find(marker)
        if pid:
            kill_group(pid)
            say(f"stopped the {name}")
    for role in ("deep", "reader", "s1"):
        pid = find(f"serve.sh {role} ")
        if pid:
            kill_group(pid, 2)
        subprocess.run(["bash", os.path.join(APP, "sabha", "research", "serve.sh"), "stop", role], check=False)
    say("stopped the model servers. A research run, if any, is stopped from the app (or it ends on its own).")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "up"
    if cmd == "up":
        return 0 if up() else 1
    if cmd == "supervise":
        return supervise()
    if cmd == "status":
        say(status_line())
        say("Open Sabha:  " + (hub_link() or "(no link yet)"))
        return 0
    if cmd == "stop":
        stop()
        return 0
    say(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
