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

## 3. REST

JSON in, JSON out. Errors: `{ "error": "message" }` with a 4xx/5xx status.
Every mutation also emits the matching `*.updated` event, so the UI may treat the
REST response as an ack and the event as the source of truth.

| Method + path | Body | Returns |
|---|---|---|
| `GET /api/state` | | `{ app, system, models, agents, missions, threads, seq_head }` |
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
| `GET /api/events?since=0&limit=2000` | | `{ events, more }` (all persisted events) |

`app`: `{ name: "Sabha", version, demo: bool }`.
`system`: `{ platform: "colab"|"kaggle"|"local", started_ts, session_limit_s, data_dir, harnesses: { dsh: bool, mock: true, chat: true } }`.
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
```
`--demo` seeds a crew (4 agents on mock models) and starts two mock missions so there is
something to watch at once. `--tunnel cloudflared` downloads the cloudflared binary if
missing and prints the public link with the token in the hash.
