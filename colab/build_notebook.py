"""Writes colab/sabha_colab.ipynb from the cells below. Run: python colab/build_notebook.py"""

import json
from pathlib import Path

CELLS = []


def md(text):
    CELLS.append(("markdown", text.strip("\n")))


def code(text):
    CELLS.append(("code", text.strip("\n")))


md("""
# Sabha on Colab / Kaggle

Run the cells top to bottom. Cell 3 prints **one link**. Open it on your laptop or phone. Cell 5 convenes a council on a paper.

- Works on Colab and on Kaggle (turn **Internet on** in Kaggle's notebook settings).
- The link carries a secret token. Anyone with the link can run commands on this box, so do not share it.
- While the repo is private, add a secret named `GH_TOKEN` (Colab: key icon in the left bar. Kaggle: Add-ons > Secrets).
- Tested on Kaggle 2x Tesla T4 with vLLM 0.33 and dsh 0.2.1-rc.5.
""")

code("""
# 1. Get the code
import os, subprocess

REPO = "https://github.com/Maverick-Ansh/sabha.git"
BASE = "/content" if os.path.isdir("/content") else "/kaggle/working"
APP = f"{BASE}/sabha"

token = ""
try:
    from google.colab import userdata
    token = userdata.get("GH_TOKEN") or ""
except Exception:
    try:
        from kaggle_secrets import UserSecretsClient
        token = UserSecretsClient().get_secret("GH_TOKEN") or ""
    except Exception:
        pass

url = REPO.replace("https://", f"https://{token}@") if token else REPO
if os.path.isdir(f"{APP}/.git"):
    r = subprocess.run(["git", "-C", APP, "pull", "--ff-only"], capture_output=True, text=True)
else:
    r = subprocess.run(["git", "clone", "--depth", "1", url, APP], capture_output=True, text=True)
print("ok" if r.returncode == 0 else r.stderr.replace(token, "***") if token else r.stderr)
""")

code("""
# 2. Set up the box: Node 24, the agent harness (dsh) and vLLM in its own venv. About 5 minutes, safe to re-run.
import subprocess
p = subprocess.Popen(["bash", f"{APP}/scripts/box_setup.sh"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
for line in p.stdout:
    print(line, end="")
""")

code("""
# 3. Start the hub in the background and print the link
import subprocess, sys, time, re, os, secrets

LOG = f"{BASE}/sabha_hub.log"
TOKEN = globals().get("TOKEN") or secrets.token_urlsafe(18)            # same token if you re-run this cell
subprocess.run("pkill -f '[p]ython.* -m sabha' || true", shell=True)   # stop an older hub (and the models it launched)
time.sleep(5)
env = dict(os.environ, PATH="/usr/local/bin:" + os.environ["PATH"], SABHA_TOKEN=TOKEN)   # Node 24, dsh and vllm live there
hub = subprocess.Popen(
    [sys.executable, "-m", "sabha", "--host", "127.0.0.1", "--port", "8787",
     "--tunnel", "cloudflared", "--data", f"{BASE}/sabha_data"],
    cwd=APP, env=env, stdout=open(LOG, "w"), stderr=subprocess.STDOUT, start_new_session=True)

link = None
for _ in range(90):                       # bounded: at most ~90 s
    time.sleep(1)
    m = re.search(r"Sabha link: (\\S+)", open(LOG).read())
    if m:
        link = m.group(1)
        break
    if hub.poll() is not None:
        break
print(link or ("hub did not start. Log tail:\\n" + open(LOG).read()[-2000:]))
""")

md("""
## Models and a crew

Open the link. You can do the rest in the app (**Crew > Models > Launch on this box**), or run the
cell below, which does the same thing through the hub's API: two models, one per T4, and one agent on each.

What the flags are for, on a 15 GB T4:

- `--max-num-seqs 8`. vLLM's default of 256 parallel sequences runs out of memory while it warms up.
- `--dtype float16`. A T4 has no fast bf16.
- `--limit-mm-per-prompt`. Qwen3.5 can also read images. Agents here are text only, so that side is switched off.
- `--tool-call-parser qwen3_xml --reasoning-parser qwen3`. Qwen3.5 writes tool calls as XML. With the
  `hermes` parser the calls are dropped without any error and the agent only talks about what it would do.

The first start of a model takes about 10 minutes (download, then vLLM compiles GPU kernels on the CPU).
Later starts in the same session take about 4. A mission sent to a model that is still loading waits for it.
""")

code("""
# 4. Two models (one per GPU) and two agents, through the same REST API the app uses
import json, urllib.request

def api(method, path, body=None):
    req = urllib.request.Request("http://127.0.0.1:8787" + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http": e.code, "body": e.read().decode()[:500]}

COMMON = ("--max-model-len 24576 --max-num-seqs 8 --gpu-memory-utilization 0.92 --dtype float16 "
          "--limit-mm-per-prompt '{\\"image\\":0,\\"video\\":0}' --enable-prefix-caching "
          "--enable-auto-tool-choice --tool-call-parser qwen3_xml --reasoning-parser qwen3")
#        name                  Hugging Face id                 served    port  GPU
SPECS = [("Qwen3.5-4B",         "Qwen/Qwen3.5-4B",              "qwen4b", 8000, "0"),
         ("Qwen3.5-9B (4-bit)", "cyankiwi/Qwen3.5-9B-AWQ-4bit", "qwen9b", 8001, "1")]
#        name       role                                             served    hue     persona
CREW = [("Lead",    "chairs the council and keeps it on the question", "qwen9b", "sky",
         "You weigh what the others say and look for the one thing that decides the question."),
        ("Scout",   "reads fast and reports what is there",            "qwen4b", "mint",
         "You report exactly what the text says, with numbers, and nothing more."),
        ("Critic",  "doubts every claim until it sees the evidence",   "qwen9b", "rose",
         "You are a sceptical reviewer. You look for missing controls, weak baselines and claims bigger than the evidence."),
        ("Builder", "asks how you would reproduce it",                 "qwen4b", "sun",
         "You are an engineer who would have to reproduce this work. You care about what is specified well enough to build.")]

state = api("GET", "/api/state")
have = {m["name"]: m for m in state["models"]}
for name, hf, served, port, gpu in SPECS:
    # extra_body: Qwen3.5 thinks for minutes before a one-line chat reply. This keeps Talk and plans quick.
    # Agent runs go through dsh and still think.
    m = have.get(name) or api("POST", "/api/models", {"name": name, "kind": "chat", "served_name": served,
            "base_url": f"http://127.0.0.1:{port}/v1", "context_window": 24576,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}})
    have[name] = m
    if m.get("status") in ("starting", "ready"):
        print(name, "already", m["status"])
        continue
    r = api("POST", f"/api/models/{m['id']}/launch", {"env": {"CUDA_VISIBLE_DEVICES": gpu},
            "cmd": f"vllm serve {hf} --port {port} --served-model-name {served} {COMMON}"})
    print(name, r.get("status", r))

by_served = {m["served_name"]: m["id"] for m in have.values()}
agents = {a["name"]: a for a in state["agents"]}
for name, role, served, hue, persona in CREW:
    if name not in agents:
        agents[name] = api("POST", "/api/agents", {"name": name, "role": role, "model_id": by_served[served],
            "harness": "dsh", "hue": hue, "persona": persona})
print("crew:", list(agents))
""")

md("""
## A council on a paper

Open the **Council** tab, paste an arXiv id and press **Convene**. Or run the cell below, which does the
same thing through the API. The hub fetches the paper, cuts it at its section headings and gives each
member one part. They write briefs at the same time, discuss for two rounds (a member can end a turn
with `LOOKUP: <query>` and the hub searches the web for everyone), and the chair writes the verdict.
Four members on two T4s take about five minutes for a ten page paper.

Agents on missions (Work tab) also have the web: `websearch`, `papers` and `webread` are on their PATH.
They need no API key.
""")

code("""
# 5. Convene a council (wait until both models show "ready" in the status cell)
PAPER = "2412.06769"          # an arXiv id, or any link to a paper page or PDF
QUESTION = "Do the continuous thoughts really carry the reasoning, or could the gain come from something else?"

state = api("GET", "/api/state")
members = [a["id"] for a in state["agents"]][:4]
council = api("POST", "/api/councils", {"source": PAPER, "question": QUESTION, "agent_ids": members,
                                        "rounds": 2, "lookups": True})
print(council.get("id"), council.get("status", council))
""")

code("""
# Council status (run any time). Prints the verdict when it is done.
c = api("GET", f"/api/councils/{council['id']}")
print(c["status"], "| round", c["round"], "of", c["rounds"], "|", c["note"] or c["error"])
for m in c["messages"]:
    who = next((a["name"] for a in state["agents"] if a["id"] == m["agent_id"]), "-")
    print(f"  {m['kind']:8} r{m['round']} {who:8} {m['content'][:90]!r}")
if c["verdict"]:
    print("\\n" + c["verdict"])
""")

code("""
# Status (run any time): models, GPUs, missions
import subprocess
s = api("GET", "/api/state")
for m in s["models"]:
    tail = api("GET", f"/api/models/{m['id']}/log").get("lines", [])[-1:]
    print(f"{m['name']:22} {m['status']:9} {tail[0][-110:] if tail else ''}")
print(subprocess.run("nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader",
                     shell=True, capture_output=True, text=True).stdout.strip())
for ms in s["missions"]:
    st = ms["stats"]
    print(f"{ms['status']:9} {st['tool_calls']:3} tool calls  {ms['title'][:60]}  {ms['error'][:80]}")
""")

code("""
# Hub log (run any time to see what the hub is doing)
print(open(LOG).read()[-3000:])
""")


def main():
    cells = []
    for kind, text in CELLS:
        cell = {"cell_type": kind, "metadata": {}, "source": text.splitlines(keepends=True)}
        if kind == "code":
            cell.update({"execution_count": None, "outputs": []})
        cells.append(cell)
    nb = {"cells": cells, "nbformat": 4, "nbformat_minor": 5,
          "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                       "language_info": {"name": "python"}, "accelerator": "GPU"}}
    for kind, text in CELLS:
        if kind == "code":
            compile(text, "<cell>", "exec")
    out = Path(__file__).with_name("sabha_colab.ipynb")
    out.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print("wrote", out, len(cells), "cells")


if __name__ == "__main__":
    main()
