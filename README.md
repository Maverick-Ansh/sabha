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

## The modes

- **Research.** A long-running literature mission on one screen: one tile per research agent, tap for what it
  is doing and a summary, instruct it from your phone. See "Research on your phone" below.
- **Watch.** A live orbit of your agents, a full transcript of every thought, tool call
  and result, and a timeline of the whole 12-hour session you can scrub back through.
  A `raw` switch shows the exact event JSON. Nothing is hidden.
- **Talk.** Chat with any agent. Flip the composer to *Mission* and the same message
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

## Run it for real on Colab or Kaggle

Open `colab/sabha_colab.ipynb` and run the cells. They clone this repo, install the
DeepSeek Harness and vLLM, start the hub in the background, and print one link like

```
https://something.trycloudflare.com/#token=…
```

Open that link. Then in the app: **Crew → Models → Launch on this box** to start a vLLM
server for any Hugging Face model (or add the URL of one you already started), and point
an agent at it.

By hand it is one command:

```
python -m sabha --tunnel cloudflared --data /content/sabha_data
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
