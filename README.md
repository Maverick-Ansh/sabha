# Sabha

**Mission control for AI agents running on borrowed GPUs.**

No big GPU laptop? Same here. Sabha runs your models and agents on a free Colab or
Kaggle box and gives you one link to watch them, talk to them, and work with them
from any browser, including your phone.

*Sabha* means council. Your agents sit in session. You hold the floor.

```
your laptop / phone                     Colab or Kaggle box
┌──────────────────┐   one tunnel link   ┌─────────────────────────────────────┐
│  Sabha web UI    │ <─────────────────> │  hub (python -m sabha)              │
│  Watch·Talk·Work │                     │   ├─ models: vLLM, any OpenAI-style │
└──────────────────┘                     │   ├─ agents: DeepSeek Harness (dsh) │
                                         │   └─ every event saved to disk      │
                                         └─────────────────────────────────────┘
```

## Use it on your own Kaggle (3 steps, nothing to install)

[![Open in Kaggle](https://kaggle.com/static/images/open-in-kaggle.svg)](https://kaggle.com/kernels/welcome?src=https://github.com/Maverick-Ansh/sabha/blob/main/kaggle/sabha_kaggle.ipynb)

1. Click **Open in Kaggle** above (you need a Kaggle account with a verified phone number, for GPUs and internet).
   In the notebook's right panel, **Session options**: Accelerator **GPU T4 x2**, Internet **on**.
2. Run the one cell. About **1 minute** later it prints one link. Open it on your laptop or phone.
3. The app shows your box coming up in a strip under the top bar. The two models are ready about
   **15 minutes** after you ran the cell. Then use any tab:
   - **Research**: write a question and choose how the agents work. **Instant** is one pass of about 20 minutes.
     **Continuous** keeps them at it for the hours you pick (or until the session ends), in waves: after every wave
     they add a chapter to the report, compact what they know, keep lessons in long-term memory, clear their
     context and re-plan where the open questions are. **Report** shows where things stand at any moment.
     **Finish now** writes the final answer early. **Download results** gives you everything as a zip.
   - **Talk**: think out loud with an agent (Lead, Scout, Critic, Builder are made for you). It remembers everything
     the box has done (research runs, paper cards, councils, missions, earlier chats) and searches that or the web
     before it answers; each lookup shows in the reply. Flip the composer to Mission for long work.
   - **Council**: give it an arXiv id; the agents split the paper, discuss it and write a verdict.
   - **Work**: give an agent a task; it plans, searches the web, runs tools and reports back.
   - **Nirmana**: the workshop. Load any model from Hugging Face onto a GPU, remove it, drag its context window,
     try it, and chain models into flows (a small model can gate a big one) whose every step you watch as it runs.

Measured on a fresh Kaggle 2x T4 session: the link in 47 s, the agent tools and vLLM installed in 4 min, model
downloads under 2 min (while installing), then 10.5 min of model warm-up: about 15 min in all. Qwen3.5-9B (4-bit) on GPU 0 does the deep thinking, Qwen3.5-4B on GPU 1 reads.

Good to know:
- **Keep the cell running and the Kaggle tab open on an awake computer.** The cell prints a status line every 4 minutes,
  which stops Kaggle from shutting an idle session. Stopping the cell stops nothing else; run it again to get the link back (`SABHA_NEW_LINK=1` for a new one).
- **Keep the link private.** It carries the box's token: whoever has it controls the box.
- **Kaggle limits:** a session ends after 12 hours, and you get about 30 GPU hours a week. Everything on the box is
  gone when the session ends, so download your research results first (or set a GitHub repo, below).
- **Better web search:** free search engines answer only a few searches from Kaggle's servers. A free
  [Tavily](https://tavily.com) key fixes that: paste it in the Research tab, or add a Kaggle Secret `TAVILY_API_KEY`.
- **Optional Kaggle Secrets** (Add-ons > Secrets, and tick each one for this notebook): `TAVILY_API_KEY`, `HF_TOKEN`,
  and `SABHA_GIT_REPO` (`owner/name`) with `SABHA_GIT_TOKEN` to push research results to your own GitHub repo every 10 minutes.
  Alerts on your phone read secrets too (see "Alerts" below), so every new box texts you its link.
- **Colab works too** (one T4, so one model does everything; an A100 gets Qwen3.5 27B): [colab/sabha_colab.ipynb](colab/sabha_colab.ipynb).
- **Laptop closed?** Kaggle can run the notebook in the background: Save Version, then *Save & Run All (Commit)*, with the
  GPU and Internet on and your alert secrets attached. The box then texts you its link (see "Alerts"). Not yet verified
  end to end on our side: try it with a short run first.
- **Light or dark:** the sun / moon button in the top bar (it follows your system until you choose).

## Continuous research: how the agents keep going for hours

A long run is cut into **waves** (20 to 60 minutes each, chosen from the run length, or set by you). Inside a wave
the agents work as before: scout searches, readers take one paper each (a fresh context per paper), the deep model
folds new cards into each track's claims ledger. When the wave ends, the deep model:

1. writes the wave's **chapter** (`waves/wNN.md`): what it found, what changed in what the team believes, dead ends;
2. **compacts** the state of knowledge (`knowledge.md`, under 900 words): the answer so far, what is established,
   contested, open, the dead ends, the next directions. This is the only history the next wave carries;
3. tends **long-term memory** (below): a few lessons, preferences, facts and sources worth keeping after the run;
4. **re-plans**: keeps, refocuses or retires each track and may open new ones for the open questions.

Then every agent starts the next wave from files only, so a 12-hour run keeps every prompt the same size. The
**living report** (`REPORT.md`, the Report button) is rebuilt from those parts: where we stand on top, one chapter
per wave below, and the final answer at the very top when the run ends. A wave also ends early when every track has
run dry, so the agents never sit idle waiting for the clock.

Context engineering in the prompts: the parts that do not change within a wave (the memory lesson, the mission, the
track, your taste, the state of knowledge, the long-term memories picked for this run) come first, so vLLM's prefix
cache keeps hitting; the parts that change (the paper, the nearest claims, the board) come last. Every block has
its own size budget, so the important ones are never cut.

**Long-term memory** (`<data>/memory/`, one file per memory and a `MEMORY.md` index) is what the agents keep across
runs: `owner` (what you like and distrust), `lesson` (searches and habits that worked or failed), `fact` (findings
with their arXiv ids) and `source` (papers, repos, benchmarks worth coming back to). Each run starts from the
memories closest to its question; the gardener adds, updates or deletes a few at every wave. Research, Memory lists
them; delete anything wrong. Talk searches them too.

## Alerts: the box texts you

Crew, **Alerts** sets any of these (all optional, all free except a real phone call):

| Channel | Setup | What you can do |
|---|---|---|
| Phone push (ntfy) | install the ntfy app, subscribe to a long topic name, type the same name in Sabha | read |
| Telegram | @BotFather, /newbot, paste the token, message your bot once, press Find my chat | read and reply: `/status`, `/report`, `/research <question> for 4h`, `/tell <instruction>`, `/finish`, `/stop` |
| WhatsApp | CallMeBot (free; message the number on callmebot.com once for a key, their bot is sometimes full), or Twilio's WhatsApp sandbox (free trial; send it `join <code>`, say hi when you start a box) | read |
| Email | SMTP (for Gmail an app password) | read |
| Phone call | Twilio (paid), or a free Telegram voice call through CallMeBot | rings when a run finishes or breaks |

You hear about: the box being up (with the new link, so you never copy it from the notebook), the models ready, a
run started, every wave (its headline), the run finished (the first sentence of the answer), problems, and 30
minutes before the session ends. Calls ring only for "finished" and "problem". The same settings come from Kaggle /
Colab secrets on every new box: `NTFY_TOPIC`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WHATSAPP_PHONE`,
`CALLMEBOT_KEY`, `WHATSAPP_PROVIDER`, `TWILIO_WHATSAPP_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `NOTIFY_EMAIL`, `TWILIO_SID`, `TWILIO_TOKEN`,
`TWILIO_FROM`, `CALL_TO`, `CALL_TELEGRAM_USER`.

## Bigger GPUs, more models

Crew, **Models** shows the box's GPUs, which model serves which role, ready layouts for this hardware and a catalog
with a fit verdict per model (fits, tight, too big, GPU too old), worked out from the weights, the KV cache and
the GPU's memory and compute capability. Search finds any model on Hugging Face (size from its safetensors
metadata, context cost from its config.json). **Use** switches the box live: the supervisor stops the old server,
downloads the new weights and starts vLLM, and the boot strip shows the progress. Agents reach models by role, so
the crew and research keep working.

| Box | Default | Also ready |
|---|---|---|
| Kaggle 2x T4 | Qwen3.5 9B (4-bit) deep + 4B reader | Qwen3.5 27B (4-bit) over both GPUs |
| Colab T4 | Qwen3.5 9B (4-bit) | Qwen3.5 4B |
| Colab L4 | Qwen3.5 9B (4-bit) | full 9B, Qwen3.5 35B-A3B MoE (4-bit) |
| Colab A100 40 GB | Qwen3.5 27B (4-bit) | + a 9B reader, 35B-A3B MoE, gpt-oss 20B |
| Colab A100 80 GB / H100 | Qwen3.5 27B (full precision) | + a 9B reader, 35B-A3B MoE, gpt-oss 120B, Llama 3.3 70B (4-bit) |

On a GPU newer than a T4 the servers run in bf16 with vLLM's own attention kernels; on a T4 in fp16 with Triton
attention. vLLM is pinned to 0.33.0: 0.34.0 fails on the T4 (its FlashInfer JIT kernel does not link on sm75).

## The modes

- **Research.** A literature mission on one screen, instant or continuous: the waves, the living report, one tile
  per research agent (tap for what it is doing and a summary, instruct it from your phone), long-term memory.
  On a phone the form at the top is the whole "give the agents a task" screen; add the page to your home screen.
- **Watch.** A live orbit of your agents, a full transcript of every thought, tool call
  and result, and a timeline of the whole 12-hour session you can scrub back through.
  A `raw` switch shows the exact event JSON. Nothing is hidden.
- **Talk.** Talk with any agent like a colleague. It understands what you want, says a short plan, and looks
  things up in the box's memory (research, councils, missions, chats) or on the web before it answers. Flip the composer to *Mission* and the same message
  becomes a task. The agent plans, works, and the report lands back in the chat.
- **Work.** A mission board. Approve or edit a plan before it runs, whisper a note to
  a running agent, stop, resume, and read the files it produced.
- **Council.** Give it an arXiv id. The hub cuts the paper at its section headings and
  gives each agent one part. They write briefs at the same time, discuss for a few rounds,
  look things up on the web when a point needs it, and the chair writes a verdict. You can
  speak at any time. A message after the verdict starts one more round.
- **Nirmana.** The workshop. **Models**: each GPU is drawn as a vessel holding the models on it. Each model is a
  card with its live state (the vLLM log line while it loads), a context-window slider capped at what fits on its
  GPU, how full its KV cache is right now, tokens per second, a *Try* box that streams a prompt straight at it, and
  *Remove*. *Load a model* takes a curated pick, a Hugging Face search, or a pasted `owner/name` or huggingface.co
  link, checks that it fits, and the box downloads and starts it next to the others. **Flows**: chain stages:
  *Decide* (a model picks a branch and says why), *Think* (your own instructions), *Agent* (a crew agent takes it as
  a real mission with tools), *Search* and *Read* (key-free web). Run one and the work plays out on a lit thread:
  each stage streams its output, a gate lights the option it chose and its reason, and every stage shows its model,
  time to first token, tokens and seconds.

## Research on your phone

The research daemon (`python -m sabha.research.run`, see `docs/RESEARCH_PLAN.md`) reads papers for hours on its
own. The **Research** view is its remote control, built for a phone first. It is the first screen whenever the hub
has a research mission.

- One tile per agent (a track of the mission): what it is doing right now, with a pulse while it works, its card
  count and the paper in hand.
- Tap a tile for a sheet: now doing with the step it is on, a summary on request (one call to the small reader
  model), its recent paper cards (tap one to read it), its report and digest, what it did lately.
- Give instructions from the sheet. An **instruction** stays in force and joins everything that agent reads,
  searches and writes. A **priority topic** steers its next searches. **Clear context** drops its instructions and
  topics, rebuilds its digest from the claims ledger and resets its search counters, while cards and the ledger are
  kept. **Pause** and **Resume** stop and restart one agent.
- **New mission** sends a brief from the phone, to queue behind the current mission or to start now.

Commands travel through one file, `missions/<id>/state/inbox.jsonl`. The daemon applies each within about 15
seconds and writes the result back, so the hub can restart at any time without touching the run.

On the box, next to a running daemon:

```
bash scripts/research_hub.sh
```

It starts the hub detached with a fixed token (log in `/kaggle/working/logs/hub.log`), registers the `deep` and
`reader` servers as models, and writes the phone link to `sabha-research/box_logs/hub_link.txt`. That repo is
private and the daemon pushes it every 10 minutes, so the link reaches you through git. By hand:

```
python -m sabha --tunnel cloudflared --research-repo /kaggle/working/sabha-research \
       --model reader=http://127.0.0.1:8002/v1 --model deep=http://127.0.0.1:8001/v1 --no-think
```

## Try it on your laptop (no GPU, 20 seconds)

```
pip install aiohttp
python -m sabha --demo --no-auth
```

Open http://127.0.0.1:8787. Demo mode uses a scripted mock harness and mock models, so
you can see the whole app move without any model behind it.

## Run it by hand

The notebook cell runs `python sabha/box.py up` (see the top of `sabha/box.py` for its options): it starts the hub
with a tunnel, then a detached supervisor installs the tools (`scripts/box_setup.sh`), starts one vLLM server per
GPU (`sabha/research/serve.sh`), and registers the models and a default crew. `python sabha/box.py status` prints
the state and the link, `python sabha/box.py stop` stops it all. The hub alone is one command:

```
python -m sabha --tunnel cloudflared --data /kaggle/working/sabha_data
```

The token in the link is the only thing protecting a box that can run shell commands.
Do not share the link.

## What is in the box

| Path | What |
|---|---|
| `sabha/` | The hub. One aiohttp process: REST, WebSocket, model launcher, harness runs. |
| `sabha/harness/` | Adapters. `dsh` (real agents), `mock` (demo), `chat` (no tools). |
| `sabha/council.py` | Council sessions: split a paper, briefs, rounds, verdict. |
| `sabha/nirmana.py` | Nirmana: load / remove / resize models from the app, live vLLM numbers, Try, flows and their runs. |
| `sabha/research/` | The research daemon: paper workers, scouting, memory files, `live.py` (what each agent is doing), `inbox.py` (commands from the phone). |
| `sabha/research_api.py` | The hub's Research endpoints: reads the daemon's data repo, queues commands. |
| `scripts/research_hub.sh` | Starts the hub on the box next to a running daemon and writes the phone link. |
| `sabha/webtools.py` | Key-free web access: `websearch`, `papers` (arXiv), `webread`. Standard library only. |
| `web/` | The UI. Plain ES modules and CSS, no build step. |
| `docs/PROTOCOL.md` | The contract between hub and UI. Read this to add a model kind or a harness. |
| `colab/sabha_colab.ipynb` | Launch notebook. |
| `tests/smoke.py` | End-to-end check against the mock and a fake dsh. `python tests/smoke.py` |

Sabha does not contain an agent loop. The thinking and tool use is done by
[DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) in headless JSON mode.
Sabha starts runs, records every event, and shows them to you.

## Models

Anything that speaks the OpenAI chat API works: vLLM, llama.cpp server, TGI, a remote
endpoint. Give each model a `kind` (chat, decision, embedding, custom) so you can tell
them apart in the UI. For agent work the server must support tool calling. On vLLM that
means `--enable-auto-tool-choice --tool-call-parser <parser for your model>`. The parser must
match the model: Qwen3 and Qwen3.5 need `qwen3_xml` (with `hermes` their tool calls are dropped
without an error and the agent only talks). The launch form picks this for you.

Tested on Kaggle's 2x Tesla T4: Qwen3.5-4B on one card and Qwen3.5-9B (4-bit AWQ) on the other,
vLLM 0.33, dsh 0.2.1-rc.5. `scripts/box_setup.sh` installs Node 24, dsh and vLLM on the box.
On a 15 GB card keep `--max-num-seqs 8`. The first start of a model takes about 10 minutes.

Web access. dsh's own `web_search` tool needs a paid search key, so Sabha does not use it.
Every agent run gets three commands on its PATH that need no key: `websearch "query"`,
`papers "query"` (arXiv) and `webread URL` (a page or a PDF as text, 6000 characters at a time,
with `--find WORD` to jump to a term). Search tries DuckDuckGo, then Bing, then Wikipedia.
You can run them yourself: `python -m sabha.webtools search "joint embedding predictive architecture"`.

Free search is weak from cloud machines: measured from a Kaggle box, DuckDuckGo starts sending a captcha
after a few searches, Brave and Mojeek refuse, and Bing often answers with unrelated pages. arXiv search
and page reading stay solid. For reliable web search give the hub one key in its environment:
`TAVILY_API_KEY`, `BRAVE_API_KEY` or `SERPER_API_KEY`. That service is then asked first and the free
engines are the fallback. The launch notebook reads the key from a Colab or Kaggle secret of the same name.

## Status

Early. Work mode is a base that will grow. Custom non-chat models (JEPA-style world
models, decision models with their own API) are planned as plug-ins.
