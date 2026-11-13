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

## Three modes

- **Watch.** A live orbit of your agents, a full transcript of every thought, tool call
  and result, and a timeline of the whole 12-hour session you can scrub back through.
  A `raw` switch shows the exact event JSON. Nothing is hidden.
- **Talk.** Chat with any agent. Flip the composer to *Mission* and the same message
  becomes a task. The agent plans, works, and the report lands back in the chat.
- **Work.** A mission board. Approve or edit a plan before it runs, whisper a note to
  a running agent, stop, resume, and read the files it produced.

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

## Status

Early. Work mode is a base that will grow. Custom non-chat models (JEPA-style world
models, decision models with their own API) are planned as plug-ins.
