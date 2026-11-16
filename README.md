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
   - **Research**: write your research taste, papers you love and a question, pick the hours, press **Start research**.
     The agents split the question into tracks, find and read papers for hours, and judge them by your taste.
     **Finish now** writes the final reports and synthesis early. **Download results** gives you everything as a zip.
   - **Talk**: think out loud with an agent (Lead, Scout, Critic, Builder are made for you). It remembers everything
     the box has done (research runs, paper cards, councils, missions, earlier chats) and searches that or the web
     before it answers; each lookup shows in the reply. Flip the composer to Mission for long work.
   - **Council**: give it an arXiv id; the agents split the paper, discuss it and write a verdict.
   - **Work**: give an agent a task; it plans, searches the web, runs tools and reports back.

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
- **Colab works too** (one T4, so one model does everything): [colab/sabha_colab.ipynb](colab/sabha_colab.ipynb).

## The modes

- **Research.** A long-running literature mission on one screen: one tile per research agent, tap for what it
  is doing and a summary, instruct it from your phone. See "Research on your phone" below.
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
