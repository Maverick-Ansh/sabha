# Sabha protocol

The contract between the **hub** (Python, runs on the Colab/Kaggle box next to the GPUs)
and the **web UI** (static files in `web/`, served by the hub). Both sides are written
against this file. If code and this file disagree, fix one of them in the same commit.

```
browser  <── HTTPS/WSS over a tunnel ──>  hub (aiohttp, one process)
                                           ├─ serves web/            (static, no auth)
                                           ├─ /api/*  REST           (token)
                                           ├─ /ws     event stream   (token)
                                           ├─ model registry + launcher (vLLM or any OpenAI-compatible URL)
                                           ├─ harness runs (dsh | mock | chat), one subprocess per run
                                           ├─ /api/research/*: reads the research daemon's data repo, queues commands for it
                                           └─ data dir: state.json, events.jsonl, workspaces/<mission_id>/
```

The hub does not implement an agent loop. Agent work is done by a harness
(DeepSeek Harness `dsh` in headless JSON mode). The hub starts runs, normalises
their events, stores them, and fans them out.

## 1. Auth

- One shared token. Source order: `--token`, env `SABHA_TOKEN`, else random (printed at start).
- REST: `Authorization: Bearer <token>`. WebSocket: `/ws?token=<token>`.
- Static files need no token. Everything under `/api` and `/ws` returns `401 {"error":"unauthorized"}` without it.
- The hub prints one link: `https://<tunnel-host>/#token=<token>`. The UI reads the token from
  `location.hash`, stores it in `localStorage["sabha.token"]`, and removes it from the URL.
- `--no-auth` is allowed only when bound to `127.0.0.1` (local demo). Then any or no token passes.

## 2. Entities

All ids are short strings (`m_`, `a_`, `ms_`, `t_`, `msg_` prefix + 8 hex). All times are epoch seconds (float).

### Model
```json
{ "id": "m_1a2b3c4d", "name": "Qwen3.5-27B",
  "kind": "chat",                       // chat | decision | embedding | custom | mock
  "base_url": "http://127.0.0.1:8000/v1",   // OpenAI-compatible root; "" for mock
  "served_name": "qwen27",              // the model id the endpoint expects
  "api_key_env": "",                    // name of an env var holding the key; "" = none
  "context_window": 24576,
  "extra_body": {},                     // merged into every chat request the hub itself sends (Talk, plans,
                                        // the chat harness), e.g. {"chat_template_kwargs":{"enable_thinking":false}}
  "status": "ready",                    // unknown | starting | ready | down
  "launch": null,                       // or { "cmd": "...", "env": {..}, "pid": 123, "started_ts": 0 }
  "notes": "" }
```
`kind` is a label for the UI except `mock`, which the hub answers itself (no GPU needed).

### Agent
```json
{ "id": "a_...", "name": "Lead", "role": "plans and reports",
  "persona": "system prompt text", "model_id": "m_...",
  "harness": "dsh",                     // dsh | mock | chat
  "hue": "sun" }                        // sun | coral | mint | sky | lilac | rose
```
`harness: chat` means no tools: a mission is one plain completion.

### Mission
```json
{ "id": "ms_...", "title": "short title", "prompt": "full task text", "agent_id": "a_...",
  "status": "running",
  "require_plan_approval": false,
  "plan": [ { "id": "p1", "text": "read the repo", "status": "done" } ],   // todo | doing | done | skipped
  "created_ts": 0, "started_ts": 0, "ended_ts": null,
  "result_text": "",                    // final answer, set when a run ends
  "error": "",
  "session_id": "",                     // harness session, used to resume
  "workspace": "workspaces/ms_...",     // relative to data dir
  "runs": 1,                            // how many harness invocations so far
  "stats": { "turns": 0, "steps": 0, "tool_calls": 0, "tool_errors": 0,
             "prompt_tokens": 0, "completion_tokens": 0 },
  "notes": [ { "id": "n1", "ts": 0, "text": "use pytest", "delivered": false } ] }
```

Status machine:
```
queued ──> planning ──> awaiting_approval ──(approve)──> running
queued ──────────────────────────────────────────────> running      (no approval asked)
running ──> done | failed | stopped
done|failed|stopped ──(resume / undelivered note)──> running
```
`title` defaults to the first ~60 chars of the prompt.

### Thread (Talk mode)
```json
{ "id": "t_...", "agent_id": "a_...", "title": "…", "created_ts": 0, "updated_ts": 0,
  "busy": false,
  "messages": [ { "id": "msg_...", "role": "user", "content": "hi", "thinking": "", "ts": 0,
                  "mission_id": null } ] }   // role: user | assistant | system
```
Snapshots (`/api/state`, `hello`) carry threads **without** `messages`; fetch them per thread.
A message with `mission_id` set is a mission card (dispatch notice or final report).

### Council
Several agents read one research paper in parts, then discuss it (`sabha/council.py`).
```json
{ "id": "c_...", "title": "paper title", "source": "2412.06769", "question": "what the human wants to know",
  "status": "discussing",               // fetching | reading | discussing | verdict | done | failed | stopped
  "note": "",                           // what it is waiting on, e.g. "searching the web: ..."
  "rounds": 2, "round": 1, "lookups": true, "lookups_used": 0, "chair_id": "a_...",
  "paper": { "title": "", "authors": [], "url": "", "abstract": "", "chars": 0,
             "sections": [ { "id": "s1", "title": "1 Introduction", "chars": 4604, "seat": 0 } ] },
  "seats": [ { "agent_id": "a_...", "section_ids": ["s1", "s2"], "part": "1 Introduction to 2 Related Work",
               "chars": 9138, "cut": false, "state": "idle" } ],   // state: idle | reading | speaking
  "messages": [ { "id": "cm_...", "role": "agent", "kind": "brief", "agent_id": "a_...", "round": 0,
                  "content": "", "thinking": "", "ts": 0, "meta": {} } ],
  "verdict": "", "error": "", "created_ts": 0, "ended_ts": null }
```
Message `kind`: `brief` (a seat's summary of its part), `say` (a discussion turn), `lookup` (web
results the hub fetched because a turn ended with `LOOKUP: <query>`; `meta` has `query`, `engine`,
`results`), `verdict` (written by the chair), `human`, `note` (hub notice). `role` is `agent`,
`human` or `system`. Snapshots carry councils **without** `messages`; fetch them per council.
The paper text itself stays on disk (`<data>/councils/<id>/paper.json`), never in an event.

Flow: fetch the paper and cut it at its section headings; give consecutive sections to the seats so
each carries about the same number of characters (`cut: true` when a part had to be trimmed to fit
the model's context); every seat writes a brief at the same time; then the seats speak in turn for
`rounds` rounds, each seeing its own part, the other briefs and the talk so far; then the chair
writes the verdict. Every turn is one plain chat completion, so the hub still has no agent loop.
A human message after the verdict starts one more round and a new verdict.

## 3. REST

JSON in, JSON out. Errors: `{ "error": "message" }` with a 4xx/5xx status.
Every mutation also emits the matching `*.updated` event, so the UI may treat the
REST response as an ack and the event as the source of truth.

| Method + path | Body | Returns |
|---|---|---|
| `GET /api/state` | | `{ app, system, models, agents, missions, threads, councils, seq_head }` |
| `POST /api/models` | Model fields (no id/status) | Model |
| `PATCH /api/models/{id}` | partial | Model |
| `DELETE /api/models/{id}` | | `{ok:true}` |
| `POST /api/models/{id}/launch` | `{ cmd, env? }` | Model (status `starting`) |
| `POST /api/models/{id}/stop` | | Model |
| `POST /api/models/{id}/probe` | | Model (status refreshed now) |
| `GET /api/models/{id}/log` | | `{ lines: [..] }` last 400 lines of the launch log |
| `POST /api/agents` / `PATCH /api/agents/{id}` / `DELETE /api/agents/{id}` | Agent fields | Agent / `{ok:true}` |
| `POST /api/missions` | `{ prompt, agent_id, title?, require_plan_approval?, thread_id? }` | Mission |
| `GET /api/missions/{id}` | | Mission |
| `POST /api/missions/{id}/approve` | `{ plan? }` (edited steps) | Mission |
| `POST /api/missions/{id}/stop` | | Mission |
| `POST /api/missions/{id}/note` | `{ text, interrupt? }` | Mission |
| `POST /api/missions/{id}/resume` | `{ text? }` | Mission |
| `DELETE /api/missions/{id}` | | `{ok:true}` |
| `GET /api/missions/{id}/events?since=0&limit=2000` | | `{ events: [..], more: bool }` |
| `GET /api/missions/{id}/files` | | `{ files: [ { path, size, mtime } ] }` (flat, relative paths, max 2000) |
| `GET /api/missions/{id}/file?path=rel/path` | | `{ path, size, truncated, text }` (text, max 200 KB) or `{ binary: true }` |
| `POST /api/threads` | `{ agent_id }` | Thread |
| `GET /api/threads/{id}` | | Thread with messages |
| `POST /api/threads/{id}/messages` | `{ content }` | `{ user: Message, assistant_id: "msg_..." }` |
| `POST /api/threads/{id}/stop` | | `{ok:true}` |
| `DELETE /api/threads/{id}` | | `{ok:true}` |
| `POST /api/councils` | `{ source?, text?, question?, agent_ids, rounds?, lookups?, chair_id? }` | Council. `source` is an arXiv id or any page or PDF link; `text` is pasted paper text. 2 to 6 agents, 1 to 4 rounds. |
| `GET /api/councils/{id}` | | Council with messages |
| `POST /api/councils/{id}/say` | `{ text, reply? }` | Council. The human speaks. On a finished council this starts one more round unless `reply` is false. |
| `POST /api/councils/{id}/stop` | | Council |
| `DELETE /api/councils/{id}` | | `{ok:true}` |
| `GET /api/councils/{id}/section?id=s1` | | `{ id, title, text }` one section of the paper |
| `GET /api/events?since=0&limit=2000` | | `{ events, more }` (all persisted events) |
| `GET /api/research[?mission=id]` | | Research overview, section 8 |
| `GET /api/research/agent/{tid}` | | One research agent (track), section 8 |
| `POST /api/research/summary/{tid}` | | `{ text, t, model, cached, fallback, error }` |
| `POST /api/research/command` | `{ cmd, track?, text?, topic?, title?, brief?, mode?, hours? }` | `{ ok, applied_by, command?, mission, daemon_alive, note }` |
| `GET /api/research/card/{arxiv_id}` | | `{ id, title, md, url }` one paper card as markdown |
| `GET /api/research/doc?kind=K[&track=tid]` | | `{ kind, track, mission, md, mtime }` |

`app`: `{ name: "Sabha", version, demo: bool }`.
`system`: `{ platform: "colab"|"kaggle"|"local", started_ts, session_limit_s, data_dir, harnesses: { dsh: bool, mock: true, chat: true }, research: bool }`.
`research` is true when a research data repo is attached (section 8).
`harnesses.dsh` is true when the `dsh` binary is on PATH.

Notes and steering. `dsh` headless takes one task per process, so a note cannot enter a
live run. `POST …/note` stores the note. When the current run ends, undelivered notes are
joined and the hub starts a new run on the same `session_id` with that text. With
`interrupt: true` the hub stops the current run first, then resumes with the note.
`resume` does the same with an explicit follow-up text on a finished mission.

Plan approval. With `require_plan_approval`, the hub asks the agent's model for a short
plan with one plain chat completion (no harness), sets `mission.plan`, and waits in
`awaiting_approval`. `approve` (optionally with edited steps) starts the run, with the
approved plan appended to the task text. Without it the run starts at once and `plan`
fills in from the harness's own todo tool, if it uses one.

File access is confined to the mission workspace. Reject any `path` that is absolute or
escapes the workspace after resolving.

## 4. WebSocket `/ws?token=…&since=<seq>`

Server → client, one JSON object per text frame.

1. First frame: `{ "type": "hello", "seq_head": 1234, "state": <same as GET /api/state> }`.
2. If `since` was given and is lower than `seq_head`, persisted events with `seq > since`
   follow in order (at most 5000, oldest dropped).
3. Then live events.

Client → server: `{ "type": "ping" }` gets `{ "type": "pong", "ts": … }`. Nothing else.
The UI reconnects with backoff and passes the last `seq` it saw.

## 5. Events

Envelope:
```json
{ "seq": 1235, "ts": 1790000000.12, "type": "run.tool_call",
  "mission_id": "ms_…", "agent_id": "a_…", "thread_id": null, "data": { } }
```
Persisted events get an increasing `seq` and one line in `events.jsonl`.
**Ephemeral** events have `seq: 0`, are never stored or replayed.

### Entity events (persisted)
| type | data |
|---|---|
| `model.updated` | `{ model }` |
| `model.removed` | `{ id }` |
| `agent.updated` | `{ agent }` |
| `agent.removed` | `{ id }` |
| `mission.updated` | `{ mission }` full object, on every status / plan / stats / notes change |
| `mission.removed` | `{ id }` |
| `thread.updated` | `{ thread }` without messages |
| `thread.removed` | `{ id }` |
| `thread.message` | `{ message }` a complete message was appended (user, final assistant, or mission card) |
| `council.updated` | `{ council }` without messages, on every status / seat / round change |
| `council.removed` | `{ id }` |
| `council.message` | `{ council_id, message }` a complete council message was appended |

Stats-only `mission.updated` is throttled to at most one per second per mission.

### Run events (persisted, always carry `mission_id` and `agent_id`)
These mirror the `dsh --json` stream, so nothing the harness reports is hidden.

| type | data | from dsh |
|---|---|---|
| `run.started` | `{ run, harness, model_id, session_id, cwd, resumed: bool }` | `session` |
| `run.status` | `{ phase, turn, step?, usage?, reason? }` phase: `turn_start`\|`step_start`\|`step_end`\|`turn_end` | `status` |
| `run.thinking` | `{ text }` | `thinking` |
| `run.text` | `{ text }` | `text` |
| `run.tool_call` | `{ call_id, tool, input }` | `tool_call` |
| `run.tool_result` | `{ call_id, status, result, error? }` status: `completed`\|`error` | `tool_result` |
| `run.final` | `{ text, reason }` | `final` |
| `run.error` | `{ message }` | `error` |
| `run.stderr` | `{ line }` harness diagnostics, one line each | stderr |
| `run.ended` | `{ run, exit_code, ok, duration_s }` | process exit |
| `run.waiting` | `{ model_id, name, why }` the run is held until the agent's model leaves `starting` (at most 30 min) | hub |
| `plan.proposed` | `{ steps }` plan waiting for approval | hub |
| `human.note` | `{ note }` a note was stored | hub |
| `human.approved` | `{ steps }` | hub |
| `human.stopped` | `{}` | hub |

`usage` is passed through as the harness gives it. The hub reads token counts from it
leniently (`input`/`output`, `prompt_tokens`/`completion_tokens`, `inputTokens`/`outputTokens`).
A run is complete only when `exit_code == 0` **and** `final.reason.kind == "completed"`.
Unknown dsh event types are forwarded as `run.raw` with `{ raw }`.

### Ephemeral events (`seq: 0`)
| type | data |
|---|---|
| `sys.telemetry` | every 5 s: `{ gpus: [ { index, name, util, mem_used_mb, mem_total_mb, temp_c } ], cpu_pct, ram_used_mb, ram_total_mb, disk_used_gb, disk_total_gb, uptime_s, session_limit_s }` |
| `chat.delta` | `{ message_id, text?, thinking? }` streamed piece of an assistant reply; carries `thread_id` |
| `chat.done` | `{ message_id }` (the full message follows as a persisted `thread.message`) |
| `model.log` | `{ model_id, line }` launch log line |
| `council.delta` | `{ council_id, message_id, mkind, round, text?, thinking? }` streamed piece of a council turn; carries `agent_id`. The first one for a turn has no text and means the turn began. |

## 6. Harness adapters

A harness adapter is a class with one coroutine:
`async def run(self, ctx) -> None`, where `ctx` gives `mission`, `agent`, `model`, `task`
(text for this invocation), `cwd`, `session_id` (empty for a first run), `emit(type, data)`
and `stop_event`. It emits run events and returns when the process is gone.

### dsh
```
dsh --profile headless --patch <run_dir>/patch.yml --json [--session-id <id>] -      (task on stdin)
cwd  = the mission workspace
env  = DSH_HOME=<data>/dsh_home/<agent_id>   DSH_PERMISSION_MODE=danger-full-access
       SABHA_LLM_KEY=<key or "none">
```
`patch.yml` is generated per run from the agent's model:
```yaml
- id: llm-pi-ai
  config:
    providers:
      sabha:
        displayName: Sabha
        apiKeyEnv: SABHA_LLM_KEY
        api: openai-completions
        baseURL: <model.base_url>
        models:
          - id: <model.served_name>
            contextWindow: <model.context_window>
- id: agent-default-model
  config:
    provider: sabha
    model: <model.served_name>
```
Web access. dsh's own `web_search` needs a paid key, so the hub writes three key-free commands
into `<data>/bin` and puts that folder first on the run's `PATH`: `websearch "query"`,
`papers "query" [--new]` (arXiv) and `webread URL [--offset N] [--find WORD]`. They are thin
wrappers around `python -m sabha.webtools` (standard library only; search tries DuckDuckGo, then
Bing, then Wikipedia). A short note describing them is prepended to the first task text.

`DSH_HOME` is per agent, not per run, because `--session-id` only resumes a session stored
in the same home and the same cwd. The agent's `persona` is prepended to the first task
text. stdout is NDJSON (one event per line, lines up to 32 KiB, `final` unbounded), stderr
is `dsh:` diagnostics.

### mock
No GPU, no dsh. Plays a believable scripted run (thinking, bash / read / write / todo tool
calls, results, a final report) with human-scale delays, and writes a few real files into
the workspace so the file browser has content. Used for `--demo` and for UI work.

### chat
One streamed completion against the model, no tools. Emits `run.thinking` / `run.text` / `run.final`.

## 7. CLI

```
python -m sabha [--host 127.0.0.1] [--port 8787] [--data ./sabha_data] [--token T] [--no-auth]
                [--demo] [--tunnel none|cloudflared|ngrok] [--session-hours 12]
                [--research-repo DIR] [--model NAME=URL ...] [--no-think]
```
`--research-repo DIR` (default: env `SABHA_RESEARCH_REPO`) attaches the research daemon's data repo and turns on
the Research view (section 8). `--model NAME=URL` registers a server that is already running as a plain
`base_url` model (kind `chat`, served name = NAME); it is repeatable and skipped when a model called NAME exists.
`--no-think` gives those models `extra_body: { chat_template_kwargs: { enable_thinking: false } }`.
`scripts/research_hub.sh` starts the hub this way on the box next to a running daemon, with a fixed token, and
writes the link to `<research repo>/box_logs/hub_link.txt`.
`--demo` seeds a crew (4 agents on mock models) and starts two mock missions so there is
something to watch at once. `--tunnel cloudflared` downloads the cloudflared binary if
missing and prints the public link with the token in the hash.

## 8. Research view

The research daemon (`python -m sabha.research.run`, design in `docs/RESEARCH_PLAN.md`) is a separate
process. It owns a data repo: `<repo>/missions/<id>/{brief.md, plan.json, cards/, tracks/<tid>/, memory/, state/}`.
The hub is started with `--research-repo <repo>`. It only reads those files. It writes one thing: lines
appended to `missions/<id>/state/inbox.jsonl`, the daemon's command inbox. An "agent" in the Research view is
one track of the mission plan (`t1`..`t6`).

```
phone -> hub: POST /api/research/command -> state/inbox.jsonl -> daemon housekeeping loop (every ~15 s)
                                                                   -> state/inbox_applied.json + a `command` event
daemon threads -> research/live.py -> state/live.json (at most every 2 s, at least every 10 s) -> hub -> phone
```

### Files the two processes share (all under `missions/<id>/state/`)

| File | Written by | What |
|---|---|---|
| `live.json` | daemon | `{ ts, epoch, mission, pid, slots: { w0..w5, maint, seeds }, tracks: { tid: { weight, paused, saturated, low_rounds, unconsolidated } } }`. A slot is `{ kind: worker|maintenance|seeds, step, since, ts, ...}`. Worker steps: `fetch`, `gate`, `map` (with `i` of `n`), `reduce`, `verify`, with `paper`, `title`, `track`. Maintenance steps: `scouting` (with `track`, `query`, `via`), `snowballing`, `consolidating`, `report`, `synthesis`; seeds: `seeds`. Every slot rests at `idle`. The hub treats the daemon as down when `epoch` is older than 90 s. |
| `inbox.jsonl` | hub (append only) | One command per line: `{ id: "c_...", cmd, by, ts, ...fields }`. |
| `inbox_applied.json` | daemon | `{ ids: [every applied id], items: [last 60: { id, cmd, track, text, ok, result, sent, ts }] }`. |
| `directives.json` | daemon | `{ instructions: [{ id, track, text, ts }], focus: [{ id, track, topic, boost, ts }], paused: { tid: weight before } }`. |

### Commands (`POST /api/research/command`)

| `cmd` | Fields | What the daemon does |
|---|---|---|
| `instruct` | `track` (a track id or `"all"`), `text` (max 600 chars) | Adds a standing instruction. `memory.packet` prints the instructions of a track (and of `all`) near the top of every memory packet, so reading, scouting and consolidation all see them. |
| `focus` | `track`, `topic` (max 120 chars) | Adds a priority topic: scouting is told to search for it and matching papers get a queue boost. Reopens a saturated track and brings its next search forward. |
| `clear` | `track` | Clears that agent's context: drops its standing instructions and priority topics (also the validator's for that track), re-renders its digest from the claims ledger, resets its saturation and low-yield counters. Cards and the ledger are kept. With `"all"` this covers every track and the instructions addressed to everyone. |
| `pause` / `resume` | `track` | Sets the track weight to 0 in `state/weights.json` (no new papers, no scouting; a paper being read is finished) / restores the weight it had. |
| `mission` | `title`, `brief` (markdown), `mode` `queue`\|`preempt`, `hours` (0.5 to 12) | Writes `missions/<NNN-slug>/brief.md` with front matter `mode`, `hours`, `title`. `queue`: it starts when the current mission ends. `preempt`: the daemon finishes the papers in hand, syncs and switches to it; the old mission resumes afterwards. A brief the planner cannot plan is marked done with an `error` and skipped. |

Every command is applied once (by `id`) and logged as an event `{ kind: "command", id, cmd, track, text, ok, result }`.
STATUS.md lists standing instructions and the last 8 commands. If no daemon is working a mission (none running, or
`live.json` is stale), the hub writes the brief of a `mission` command itself (`applied_by: "hub"`); every other
command waits in the inbox, and the response says so (`daemon_alive: false`).

### `GET /api/research`

```jsonc
{
  "available": true,                 // false when the hub has no research repo (then only `hint` and `repo` are set)
  "repo": "/kaggle/working/sabha-research", "now": 1763250000.0,
  "missions": [ { "id", "status": "waiting|running|done", "error", "started", "title", "one_line", "tracks", "mode" } ],
  "mission": { /* the active one: same fields, plus */ "hours", "status_line", "status_age_s", "has_synthesis",
               "stats": { "cards", "queued", "reading", "rejected", "failed", "per_hour", "left_m", "errors" } },
  "daemon": { "alive": true, "age_s": 3 },
  "workers": { "total": 6, "busy": 4 },
  "agents": [ { "id": "t1", "name", "one_line", "hue", "weight", "paused", "saturated",
                "state": "working|idle|paused|saturated",
                "counts": { "carded", "queued", "reading", "rejected", "failed" },
                "now": [ { "slot": "w0", "kind", "step", "paper", "title", "i", "n", "query", "via", "since" } ],
                "last_card": { "id", "title", "t" }, "instructions": 1, "focus": 0, "pending": 0 } ],
  "team": [ /* busy slots that belong to no track: seeds, snowballing, synthesis */ ],
  "feed": [ { "kind", "track", "id", "ok", "t", "text" } ],          // last 12 events, newest first, in plain words
  "commands": { "pending": [ ... ], "applied": [ ... last 8, newest first ] },
  "standing": { "instructions": [ ... ], "focus": [ ... ] }            // those addressed to "all"
}
```
The active mission is the one whose `live.json` is fresh, else the running one, else the first that is not done.
`?mission=<id>` reads another one. Times `t`, `since`, `started`, `now` are epoch seconds.

### `GET /api/research/agent/{tid}`

The agent row above, plus `mission`, `daemon_alive`, `deliverable`, `questions`, `events` (its last 15, newest first),
`cards` (its last 8: `{ id, title, one_line, grade, quote_pass, causal, repo, t, secs }`), `queue` (next 5 papers),
`digest` (markdown), `report_head` (first 1800 characters) and `report_chars`, `standing`
(`{ instructions, focus }` for this track and for `all`), `board` (open questions addressed to it), `board_open`,
`pending_commands`, and `summary` (`{ text, t, model, fallback }` or null).

### `POST /api/research/summary/{tid}`

One plain chat completion to the model called `reader` (a registered model with that name or served name, else
`SABHA_READER_URL`, default `http://127.0.0.1:8002/v1`), thinking off, 340 tokens: 4 to 6 plain sentences on what
the agent did, built from its counts, live state, last 8 card one-liners and last 15 events. The answer is kept
for 5 minutes per agent (`cached: true`). If the model does not answer, the hub returns a summary built from the
counts with `fallback: true` and the reason in `error`.

### `GET /api/research/doc`

`kind` is one of `status` (STATUS.md), `digest` (DIGEST.md), `synthesis`, `brief`, and with `track=<tid>`:
`report`, `track_digest`, `assignment`. 404 when the file is not written yet.

## 9. Box, settings, taste and research runs started from the app

The notebook cell runs `sabha/box.py up`. It starts the hub (`--data <base>/sabha_data`) and a detached
supervisor, which writes `<data>/boot.json` every few seconds while it installs the tools and starts the vLLM
servers of `<data>/run/layout.json` (section 11; on first boot the catalog's default for the GPUs: on 2x T4,
deep = Qwen3.5-9B AWQ on GPU 0, port 8001, and reader = Qwen3.5-4B on GPU 1, port 8002). It registers each model
(`served_name` = `deep` / `reader`, `extra_body` turns thinking off for Qwen) and, once, a default crew of four agents.

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/api/boot` | | `{present, ready, stage, note, alive, age_s, gpus, steps:[{id, short, label, status, detail, secs, expect_s}]}`. `present:false` = the hub was not started by the launcher. Step status: `waiting`, `running`, `downloading`, `starting`, `ready`, `done`, `failed`. |
| GET | `/api/settings` | | `{git_repo, tavily_key:{set, hint, source}, hf_token:{…}, git_token:{…}, search, git_sync}`. A key never comes back, only `set` and its last 4 characters. |
| PUT | `/api/settings` | any of `tavily_key`, `hf_token`, `git_repo` (`owner/name`), `git_token`; `""` clears | same as GET. Stored in `<data>/settings.json` (mode 600); the Tavily key goes into the hub's environment at once (web tools, dsh runs and the research daemon inherit it). |
| GET | `/api/taste` | | `{taste, loves:[…], topic, hours, mode, wave_min, updated}` |
| PUT | `/api/taste` | any of `taste` (≤ 6000 chars), `loves` (list or text; arXiv ids are pulled out of links, other lines kept as titles), `topic`, `hours` (0.5 to 12, or 0 = until the session ends), `mode` (`instant` \| `continuous`), `wave_min` (0 = automatic) | same as GET |
| POST | `/api/research/start` | optional taste fields (saved first) | the run status (below) plus `title`, `note`. 400 when the question is shorter than 12 characters, 409 when a run is going or no model served as `deep`/`reader` is ready. |
| POST | `/api/research/stop` | `{mode: "finish" \| "now"}` | the run status. `finish` sends the daemon a `finish` command: final reports and synthesis from what is in hand, then it ends. `now` stops it at once. |
| GET | `/api/research/export/{mission}` | | a zip of the mission folder (brief, taste, plan, cards, track reports, ledgers, synthesis). Accepts `?token=` because a download link sends no header. |

Run status, also under `run` in `GET /api/research`: `{running, mission, started, until, hours, mock, mode,
wave_min, finishing, ended, how, git_sync, log}`. `mode` is `instant`, `continuous` or `one pass` (a continuous run
too short for waves).

Run shape: **instant** = 0.34 h, 2-3 tracks, no waves. **continuous** = the hours asked for (0 = until 15 min before
the session ends, capped by what is left), cut into waves of `wave_min` minutes, automatic = 20 (≤ 1.5 h), 30
(≤ 3 h), 45 (≤ 6 h) or 60. The brief's front matter carries `run`, `tracks`, `wave_minutes`; the daemon gets
`--wave-min`. Section 12 has what happens at each wave.

Start writes `missions/<NNN-slug>/{brief.md, taste.md, loves.json}` under the research home (`<data>/research`, or
`--research-repo`) and starts, detached, `python -m sabha.research.run --mission <id> --once` (in demo mode
`sabha.research.mock`), the arXiv harvester and the embedding index. Their pids live in `<data>/run/research.json`,
so a restarted hub finds a run that is still going. The taste reaches the planner (through the brief), System-1
triage, the reader's gate, every card (`why_it_matters`, shown as "Why it matters to you"), the memory packet of
every deep step, the track reports and the synthesis. Loved papers are queued first. Git sync runs only when the
research home is a git checkout: the hub makes it one at Start when a repo and token are set.

## 10. Memory and Talk

`sabha/recall.py` turns everything the box did into one searchable memory: research runs (`research:<mission>`), their
syntheses, track reports and digests (`synthesis:`, `report:<mission>/<track>`, `digest:`), paper cards
(`card:<mission>/<arxiv id>`), councils (`council:<id>`), Work missions with their final report and text files
(`mission:<id>`), earlier Talk chats (`chat:<thread>`) and the research taste (`taste`). Folders listed in
`SABHA_MEMORY_DIRS` (os.pathsep separated, each holding `missions/`) are searched too, e.g. an older research repo.
The index is rebuilt whenever something new lands. Search is BM25 over words with a title boost.

| Method | Path | Answer |
|---|---|---|
| GET | `/api/memory/search?q=…&kind=card,report&k=8` | `{hits:[{id, kind, title, score, snippet, chars}]}` |
| GET | `/api/memory/doc?id=…&part=1` | `{id, kind, title, part, parts, text}` (6000 characters a part); 404 for an unknown id |

A Talk reply (real models) gets a system prompt with the agent's name, role and persona, how to talk (understand
first, say a short plan, then do it; never invent; long work becomes a mission), a catalogue of what the box
remembers and the owner's taste. Before the model is called, the hub searches memory for the owner's message and
adds the matching snippets. The request carries four read-only tools: `search_memory {query, kind?}`,
`read_memory {id, part?}`, `web_search {query}`, `read_page {url, find?}`; up to 6 rounds of tool calls per reply.
A reply that only announces a lookup is nudged once to make it. A server that refuses `tools` gets plain chat.
Each lookup is streamed as `chat.delta {message_id, step:{tool, label, result, id?, url?}}` and kept on the final
message as `steps: [...]`.

## 11. Models catalog: which model serves which role (`sabha/catalog.py`)

`<data>/run/layout.json` = `{servers:[{role, hf, name, label, family, gpus:[…], tp, port, util, ctx, seqs, dtype,
tool_parser, reasoning_parser, mm, no_think, weights_gib, fits, why}], by, ts}`. The supervisor re-reads it every few
seconds; a server whose fields changed is stopped (`serve.sh stop`), its weights are fetched, and it starts again
with `CUDA_VISIBLE_DEVICES=<gpus>`, `--tensor-parallel-size <tp>`, `--dtype <dtype>` (float16 on a T4, auto on
newer GPUs; Triton attention only on fp16). Two servers on one GPU start one after the other. A role that is not
served (one model for every role) gets its hub entry pointed at the deep server, so agents seated on it keep
working; research then runs with `SABHA_READER=deep`. Each role's context window reaches the daemon as
`SABHA_CTX_<ROLE>`.

Fit per server: weights / tp + the KV cache of one full-length request (KV per token from config.json: full-attention
layers x KV heads x head dim x 2 x 2 bytes, MLA from `kv_lora_rank`) + overhead (1.5 GB + 1.5 % of the weights,
+0.4 GB with tensor parallel) ≤ GPU memory x util (0.90 on GPUs under 30 GB, else 0.92) x share. `yes` leaves room
for three full requests, `tight` for one (the context may be halved down to 8k to fit), `no` does not fit or the GPU's
compute capability is too low (FP8 / MXFP4 / GPTQ-Marlin need 8.0, NVFP4 needs 10.0).

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/api/catalog` | | `{gpus:[{index, name, mem_mb, sm}], layout, presets:[{id, title, why, fits, servers}], models:[{hf, label, family, weights_gib, kv_kib, min_sm, max_ctx, note, curated, solo:{fits, tp, gpus, util, ctx, free_gib, why}, seats:{deep?, reader?}}], demo}`. Layout servers carry `status`/`detail` from the boot strip. Demo mode reports two demo T4s. |
| GET | `/api/catalog/search?q=` | | `{models:[…]}` the same shape. Text = Hugging Face search (text-generation and image-text-to-text, by downloads; GGUF, MLX, EXL, NVFP4, ONNX and bnb repos left out). `owner/name` = one lookup with the real size and config. |
| POST | `/api/catalog/use` | `{preset}` or `{hf, as: "solo" \| "deep" \| "reader"}` | `{layout, note, why}`. 409 when it does not fit (the answer says why), or when the seat does not exist. |

## 12. Continuous research: waves (`sabha/research/waves.py`)

`state/waves.json` = `{n, wave_s, started, ends, closed, history:[{n, started, ended, cards, headline, memory}],
plan_changes}`. A wave turns over when its time is up or when every active track is saturated with nothing queued
(after 8 minutes), never when less than 0.4 of a wave is left before the final reports. The turnover, in the deep
model's one maintenance thread while the readers keep reading: fold the wave's last cards into the ledgers; write
`waves/wNN.md` (HEADLINE line + What we found, What changed in what we believe, Dead ends, What the next wave should
do); rewrite `knowledge.md` (Answer so far, Established, Contested, Open questions, Dead ends, Next directions;
≤ 7000 chars); tend long-term memory (≤ 5 ops); re-plan `plan.json` (`keep` / `refocus` / `retire` per track, up to
2 `new` tracks, at most 6 active; new questions carry `wave`, retired tracks keep their cards and get `retired: n`);
refresh `state/longmem.txt`; reset saturation; rebuild `REPORT.md`; log `wave` and `replanned` events and a line in
`state/notify.jsonl`. At the end the last wave is closed the same way (no re-plan), the synthesis gets the knowledge
and every headline first, and `REPORT.md` puts the answer on top.

Every agent prompt (the memory packet) now carries, after the track and the taste: the answer so far, the open
questions and the dead ends from `knowledge.md` (≤ 1400 chars) and the long-term memories picked for the mission
(`state/longmem.txt`, ≤ 1300 chars). Both change only at a turnover, so the prompt prefix stays stable for the
server's prefix cache within a wave.

`GET /api/research` mission fields added: `has_report`, `has_knowledge`, `waves` (the object above, `{on:false}` for
a run without waves); agent rows add `retired`, `since_wave`. Docs: `kind=living` (REPORT.md), `kind=knowledge`.

## 13. Long-term memory (`sabha/research/longmem.py`)

`<data>/memory/` (env `SABHA_LONGMEM` overrides): `MEMORY.md` (index: `- [name](file) (type) description`) and one
`<slug>.md` per memory with front matter `name, description, type, created, updated, source, uses`. Types: `owner`,
`lesson`, `fact`, `source`. The owner's taste becomes the `owner-research-taste` memory at every run start. The
gardener never deletes an `owner` memory; above 160 memories the least used, oldest non-owner ones go. Retrieval is
BM25 with name and description weighted 3x. Talk's memory search indexes them as `longmem:<name>` and living reports
as `living:<mission>`. The export zip carries them under `longterm-memory/`.

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/api/longmem?q=` | | `{home, types, memories:[{name, type, description, updated, source, uses, score?}]}` (all, or the 30 best for q) |
| GET | `/api/longmem/{name}` | | the memory with `body`; 404 when missing |
| PUT | `/api/longmem/{name}` | `{type, description, body}` | the memory (the owner writes one) |
| DELETE | `/api/longmem/{name}` | | `{ok:true}`; 404 when missing |

## 14. Alerts (`sabha/notify.py`)

`<data>/notify.json` (mode 600): `{ntfy:{topic, server, level}, telegram:{bot_token, chat_id, level}, whatsapp:{provider, phone,
apikey, twilio_sid, twilio_token, twilio_from, level}, email:{host, port, user, password, to, level}, call:{provider, twilio_sid, twilio_token, twilio_from,
to, telegram_user, level}}`. Levels: `all`, `important` (link, run_done, problem, session_ending), `off`. Calls ring
only for run_done and problem. Kaggle / Colab secrets fill empty fields at boot (names in the README).

Events: `link` (from `__main__` as soon as the tunnel is up), `box_ready` (boot.json turns ready), `run_started`,
`wave` and `run_done` (from `state/notify.jsonl`, scanned every 15 s from a saved offset so a hub restart never
re-sends), `problem` (a run that ended without finishing), `session_ending` (30 minutes left).

Telegram is two-way: the hub long-polls `getUpdates` and answers only the saved `chat_id`. Commands: `/status`,
`/report`, `/research <question> [for N h | until end | instant]`, `/tell <instruction>` (an `instruct` for every
agent), `/finish`, `/stop`, `/link`, `/help`. The same parser (`Notifier.command`) serves any later two-way channel.

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/api/notify` | | per channel `{ready, level, <field>: value or {set, hint} for secrets}`, plus `recent:[{channel, kind, title, ok, info, t, ms}]` |
| PUT | `/api/notify` | `{<channel>: {<field>: value, level}}`; `""` clears a field | same as GET. 400 for a bad level or an ntfy topic that is not 6-64 of `[A-Za-z0-9_-]`. |
| POST | `/api/notify/test` | `{channel?}` | `{results:[…]}`; 409 when the named channel is not set up |
| POST | `/api/notify/telegram/find` | | `{id, name}` of the newest chat that messaged the bot (saved as chat_id); 404 when none yet |
