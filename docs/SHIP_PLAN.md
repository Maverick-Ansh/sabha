# Sabha v1: ship plan

Goal: a colleague opens one notebook on their own Kaggle account, runs one cell, and gets their own
Sabha: their agents (Talk, Council, Work with web search) and a Research tab that runs for hours on
their own research taste. No repo of ours, no keys, no validator needed.

## What a colleague does

| step | what | wait |
|---|---|---|
| 1 | Click **Open in Kaggle** in the README. In the notebook: Settings > Accelerator **GPU T4 x2**, Internet **on**. | 1 min |
| 2 | Run the one cell. It prints one link. Open it on a laptop or phone. | about 2 min |
| 3 | The app shows the box coming up: tools, then one line per model. Talk, Council and Work work as soon as the first model is ready. | tools about 5 min, models ready about 15 to 20 min after the cell started |
| 4 | Research tab: write your taste, the question, the hours. Press **Start research**. | first cards after about 10 min |
| 5 | Before the session ends: **Download results** (a zip). | a few seconds |

The cell keeps printing a status line every few minutes, so Kaggle does not stop an idle kernel.
Nothing is required besides the Kaggle account. Optional: a Tavily key (better web search), a
Hugging Face token, a GitHub repo and token (research results pushed every 10 minutes). Each can be a
Kaggle Secret, an environment variable, or typed into the app later.

## Components

- `sabha/box.py` (standard library only). Two roles in one file.
  - `up`, run by the notebook cell: checks the GPUs, starts the hub (detached, with the tunnel) and the
    supervisor (detached), prints the link, then prints a heartbeat line every 4 minutes for at most 11.5 h.
    Re-running it starts nothing twice (pid files checked against live processes). `SABHA_KEEPALIVE=0` returns at once.
  - `supervise`, detached: runs `scripts/box_setup.sh` while the model weights download in parallel, starts
    one `serve.sh` per model outside the hub, registers each model in the hub when it answers, adds a
    default crew, and writes `<data>/boot.json` every few seconds.
- GPU layout, detected from `nvidia-smi`:
  - two GPUs: deep = Qwen3.5-9B AWQ alone on GPU 0, reader = Qwen3.5-4B on GPU 1.
  - one GPU: reader only. Research then runs every role on the reader.
  - P100: refused with a message to pick "GPU T4 x2". No GPU: hub only (API models still work).
- Hub: `GET /api/boot` serves boot.json, the app shows it as a strip under the top bar until all is ready.
- Hub: settings (`<data>/settings.json`, file mode 600): Tavily key, HF token, git repo and token. Keys are
  never sent back to the browser, only "set" plus the last four characters.
- Research taste: `<data>/taste.json` (taste text, papers I love, the question, hours). Survives hub restarts.
- Research runs, owned by the hub (`sabha/research_run.py`): **Start** writes
  `missions/<NNN-slug>/{brief.md, taste.md}` in the research home (`<data>/research` unless `--research-repo`
  is given) and starts three detached processes: the daemon (`--once`), the arXiv harvester and the
  embedding index. One run at a time. They survive a hub restart (pid files). **Finish now** writes the
  reports and the synthesis with what is there, then stops. **Stop** stops at once.
  In demo mode the daemon is a mock (`sabha/research/mock.py`) so the smoke test covers start, live view,
  stop and the zip without a GPU.
- Taste in the prompts: mission planner (through the brief), System-1 triage, the reader's gate, the card
  (`why_it_matters`, shown as "Why it matters to you"), the memory packet of every deep step, track reports
  and the synthesis. Papers I love are queued as seeds with top priority.
- Git sync is off unless the research home is a git checkout. It becomes one when a repo and token are set
  in Settings (the hub runs `git init` and adds the remote at the next Start). The git-driven path with
  `brief.md` and `steering.md` in a private repo keeps working: point `--research-repo` at the checkout.
- No validator on a colleague's box. Steering is the human: standing instructions, priority topics, pause
  and clear from the Research tab (the inbox from the previous session, kept).

## What changes

- New: `sabha/box.py`, `sabha/settings.py`, `sabha/research_run.py`, `sabha/research/mock.py`,
  `kaggle/sabha_kaggle.ipynb` (generated), `web/js/taste.js`, `web/js/boot.js`.
- Kept and finished from the previous session: the Research tab, the inbox, the live view (`research_api.py`,
  `research/inbox.py`, `research/live.py`, `web/js/research.js`).
- `research/run.py`: git optional, `--once`, a short run scales its end-of-run margins (a 2 h run still
  scouts, reads and writes a synthesis), `finish` command, SIGTERM marks the mission stopped.
- `research/serve.sh`: tool-call parser flags (Work mode needs them), log and pid folders from env.
- `colab/build_notebook.py` writes both notebooks (same single cell).
- README quickstart, PROTOCOL sections 8 and 9, smoke tests for taste, settings, boot, start, stop, export.

## Not in v1

- Self-steering by the deep model (the validator loop). The hooks stay; nobody writes `steering.md` on a colleague's box.
- Keeping results after the session without a download or git sync.
- Accounts. One link, one token, one person per box.
- UI polish: a separate pass.
