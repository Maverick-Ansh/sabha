# Sabha Research mode: build plan (v1, 2026-11-16)

Planner and validator: Fable 5.2, laptop side. Speeds are estimates until `bench.py` measures them.

## 0. Shape

A standalone daemon, `python -m sabha.research.run`, started with `setsid` beside the hub, never inside it. It is a fixed pipeline of plain chat completions (the Council pattern), not a dsh tool loop:

1. "Context gone after each paper" is exactly a stateless pipeline whose only input is files.
2. Models at or under 9B stall or loop in tool harnesses (Ansh's ladder runs).
3. Code owns control flow, budgets and schemas (guided JSON), so every step is replayable.

The model never picks the next step, so this is no new agent harness.

## 1. Models and throughput

| Role | Model | GPU, port | Own flags |
|---|---|---|---|
| `s1` System-1 triage | Qwen/Qwen3.5-0.8B | 0, 8003 | `--max-model-len 4096 --max-num-seqs 16 --gpu-memory-utilization 0.16`, start first |
| `deep` reduce, gaps, consolidation, reports, planner, synthesis | cyankiwi/Qwen3.5-9B-AWQ-4bit | 0, 8001 | `--max-model-len 16384 --max-num-seqs 4 --gpu-memory-utilization 0.76` |
| `reader` gate, section map | Qwen/Qwen3.5-4B | 1, 8002 | `--max-model-len 20480 --max-num-seqs 6 --gpu-memory-utilization 0.92` |
| embeddings | BAAI/bge-small-en-v1.5 | 1, then CPU | bulk build before `reader` starts |

Common: `VLLM_ATTENTION_BACKEND=TRITON_ATTN`, `--dtype float16 --enable-prefix-caching --reasoning-parser qwen3 --limit-mm-per-prompt '{"image":0,"video":0}'`. Every request: `enable_thinking:false`, `response_format` json_schema, temperature 0.2 (0.4 for report prose). No tool parser.

Fallbacks: `s1` not up in 15 min means `s1=reader`. `reader` down means `deep` maps. `deep` down means `reader` reduces and the card gets `grade:"weak"` for a redo.

Per paper: `reader` runs about 9 chunk calls (3.3k tokens in, 400 out), 26 s of GPU 1. `deep` runs one reduce (6k in, 1.3k out), 30 s of GPU 0 at an assumed 18 tok/s per stream, 55 over 4 streams. `deep` is the bottleneck and also spends 11 min per hour on gaps, consolidation and reports.

- Ceiling 95 papers/h. Planned 45/h (50% derate): 400 in a 9 h window, 65 per track. Floor 25/h (9B at half speed): 225, 37 per track.
- Under 12 tok/s per stream on `deep`: set `reduce_model=reader` for rel 2 papers.
- The brief needs 10 to 15 ranked papers per track, so the floor suffices. Surplus goes to about 36 tier B deep reads and verification.
- Set `--until` to the real session end minus 15 min (the clock started with the harvest).

## 2. Files

Code in `sabha/research/` (bold = first slice):

- **`llm.py`** wraps `talk.stream_chat`: `chat_json(role, system, user, schema, max_tokens)`, semaphores, retries, repetition guard.
- **`prompts.py`** prompts, schemas, `MEMORY_LESSON`. **`memory.py`** registry, cards, ledger, board, query log, playbook, `packet(track, paper)`.
- **`paper.py`** per-paper pipeline. **`gitsync.py`**.
- `index.py`, `sources.py` (arXiv search HTML, abs page, references, GitHub API, Tavily, token buckets), `scout.py`, `planner.py`, `consolidate.py`, `report.py`, `steer.py`, `digest.py`, `run.py`, `bench.py`, `serve.sh`.

Patch `webtools.paper`: skip `_arxiv(id_list)` (429 on Kaggle), use index metadata, return the reference list. `pip install pymupdf`.

```
missions/<id>/
  brief.md                 human only
  steering.md              validator only
  plan.json  standards.md  STATUS.md  DIGEST.md  synthesis.md
  cards/<arxiv_id>.json + .md     deep/<arxiv_id>.md     rejected/<arxiv_id>.json
  memory/registry.jsonl  board.jsonl  playbook.md  seeds.json
  tracks/<tid>/assignment.md  queries.jsonl  claims.jsonl  digest.md  report.md
  state/mission.json  budget.json  calib.json  steering_applied.json  status_line.txt
```

Outside git: `/kaggle/working/arxiv_meta`, `/kaggle/working/arxiv_index/{vecs.f16.npy,ids.txt,titles.txt}`, scratch and logs in `/kaggle/tmp/sabha/`.

## 3. Shared memory

- Working: `scratch/<id>/`, paper text and map notes, deleted on commit.
- Episodic: `cards/<id>.json`, one per paper, written once, every fact has an evidence pointer.
- Semantic: `tracks/<tid>/claims.jsonl` and `digest.md`, what the track believes.
- Procedural: `tracks/<tid>/queries.jsonl` and `memory/playbook.md`, which searches paid off.
- Board: `memory/board.jsonl`. Registry: `memory/registry.jsonl`, global dedupe and also the queue.

Append-only, last line per id wins:

```
registry: {"id","title","state":"seen|queued|reading|carded|rejected|failed","track","rel","cos","prio","via","qid","cited_by","why","ts"}
claims:   {"id":"c12","q":"t1.q5","text","status":"supported|contested|hypothesis|retired","for":[ids],"against":[ids],"ts"}
queries:  {"qid","track","q":"t3.q5","text","via":"index|arxiv|tavily","n_hits","n_new","n_rel2","n_carded","ts"}
board:    {"id":"b7","from":"t1","to":"t3|all|validator","q","status":"open|answered|stale","answer","cards":[ids],"ts"}
```

Consolidation runs per track after 8 new cards or 45 min. `deep` gets the digest, the new cards' claims and the 3 nearest ledger claims per new claim (bge on CPU). It answers with operations only:

```
{"ops":[{"op":"add|support|contradict|refine|retire","claim":"c12","text":"","q":"t1.q5","cards":["id"],"why":""}],
 "answers":[{"board":"b7","text":"","cards":["id"]}], "board":[{"to":"t3","q":""}]}
```

Code applies the ops. `contradict` never overwrites: the claim turns `contested`, keeps both card lists, and a board question is posted. `digest.md` (1200 tokens, grouped by question id) is regenerated from the ledger. Code rebuilds `playbook.md` (400 tokens) from yield stats plus validator lessons.

The memory lesson opens every system prompt (identical prefix, so cached):

```
MEMORY_LESSON
You have four memories. WORKING is this prompt. It is erased when you answer.
EPISODIC is one card per paper, written once and read by every agent.
SEMANTIC is your track's claims ledger and digest: what the team believes so far.
PROCEDURAL is the playbook: which searches and reading habits worked.
You remember nothing from earlier papers except the MEMORY PACKET below.
1. Only your JSON answer survives. Write it for a stranger.
2. Every fact carries a section name and a quote of at most 25 words, copied exactly.
3. If the packet already holds a claim, cite its id (supports c12, contradicts c12). Do not restate it.
4. Unknown is "not_stated". Never fill a field from what you remember about a famous paper.
5. A question you cannot answer goes to the board, addressed to a track.
```

`MEMORY PACKET` (2000 tokens): assignment one-liner and questions, digest, 8 ledger claims nearest to this abstract, 5 open board questions, playbook, active steering lines.

## 4. The agent loop

Six track agents share six paper slots. Every step starts from files only.

**A. Scout** (track has under 8 queued papers, at most every 10 min)

1. Gap analysis, `deep`. In: packet, coverage (claims per question id), last 10 query yields. Out: `{"gaps":[{"q","why"}],"queries":[{"text","via","q"}]}`, 6 queries, 1 Tavily at most.
2. Search: local index top 40, `arxiv.org/search` HTML top 25, Tavily top 5. Free snowballing: ids and titles from each read paper's reference list, matched against `titles.txt`.
3. Drop registry hits (version suffix stripped).
4. Triage, `s1`. In: mission and track one-liners, title, abstract (1500 chars). Out: `{"track":"t1..t6|none","rel":0-3,"kind":"method|benchmark|dataset|survey|theory|other"}`. Rel 3 answers a track question with a method or measurement. Rel 2 is a related method or benchmark. Below 2 is rejected.
5. Priority `0.45*rel/3 + 0.2*cos + 0.15*recency + 0.2*min(cited_by,5)/5 + boost`, state `queued`.
6. Log yield. A query with token Jaccard 0.7 or more against an earlier one is refused. A pattern with `n_new=0` twice is dead. Three rounds under 2 new rel 2+ papers mark the track `saturated`: no more scouting, slots go to deep reads and other tracks.

**B. Read** (deficit round-robin over track weights)

1. Fetch `arxiv.org/html/<id>`, then PDF.
2. Gate, `reader`: abstract plus introduction. Out `{"keep","track","model_type":[],"why"}`.
3. Map, `reader`, parallel: 10 chunks of 10k chars at most (24 in tier B), by title priority: method, training, loss, experiments, limitations, compute appendix, introduction. Out, 500 tokens: `{"facts":[{"field","text","sec","quote","status":"measured|hypothesis|stated"}],"repo_urls":[],"datasets":[]}`.
4. Quote check (guard below).
5. Reduce, `deep`: packet, abstract, surviving facts. Out: the card, 1500 tokens.
6. Verify, code: dates and versions from the abs page. Repo through the GitHub API with the PAT (exists, license, last push). Tavily (`"<title>" official code`) only for rel 3 cards with no repo URL.
7. Lint. On failure, one `deep` repair call naming the failed checks.
8. Commit card, registry, claims, board questions, reference candidates. Delete scratch.

Card (E is `{"sec","quote"}`):

```
{"id","version","title","authors","first_submitted","last_revised","venue",
 "source":{"kind":"arxiv_html|arxiv_pdf|web","chunks_read","chunks_total"},
 "tracks":[],"relevance":0-3,"one_line":"",
 "model_type":["representation_learner|world_model|policy|value_model|generative_simulator|benchmark|dataset|other"],
 "training_method":{"text","ev":[E]}, "architecture":{"text","ev":[E]}, "losses":[{"name","form","ev":[E]}],
 "data":[{"name","size","license","license_src":"paper|github|web|null","ev":[E]}],
 "official_repo":{"url","status":"verified|in_paper_unverified|not_found","license","last_push","src":"paper|tavily"},
 "results":[{"claim","metric","value","setting","baselines":[],"ev":[E]}],
 "hypotheses":[{"text","ev":[E]}], "limitations":[{"text","by":"authors|reader","ev":[E]}],
 "compute":{"text","repro_cost":"low|medium|high|unknown","ev":[E]}, "baselines":[],
 "causal_evidence":"intervention|counterfactual_eval|none_prediction_only|n/a",
 "independent_eval":{"as_baseline_in":[card ids]},
 "claims":[{"text","q":"t1.q5","status":"measured|hypothesis","ev":[E]}],
 "questions_for_others":[{"to","q"}],
 "quality":{"quote_pass":0.0,"lint":[],"grade":"ok|weak","deep":false}}
```

Code fills `independent_eval` with the cards that use this method as a baseline.

Hallucination guard, all code:

- A quote must be a substring of the paper text after whitespace and case folding, or the fact is dropped. `quote_pass` under 0.7: one re-read with `deep` as mapper, then `rejected/`.
- Every number in `results` must occur in the paper text.
- A repo URL must occur in the text or a Tavily result and answer on the GitHub API. Otherwise `url:null`.
- "Causal" wording needs `causal_evidence` of intervention or counterfactual_eval. Otherwise the word is removed and a lint note kept.
- Reports cite only card ids. Code deletes paragraphs citing unknown ids and flags numbers missing from cited cards.
- `web` cards (for example the Genie 3 blog) are marked secondary and cannot enter a top 5.

Tavily, hard cap 300 in `state/budget.json`: seeds 25, scouting 70, repo checks 150, reserve 55. Expected about 200.

## 5. Mission planner

`planner.py`, once per mission:

```
plan.json {"mission","one_line","hours","cats":[],"tracks":[{"id":"t1","name","one_line","weight":1.0,
  "questions":[{"id":"t1.q1","text"}],"seeds":["I-JEPA"],"starter_queries":[],"deliverable"}],
 "deliverables":[12 titles],"synthesis_questions":[],"synthesis_must_present":[]}
```

- Tracks given (`## Agent N:` headings): code cuts the brief at headings, each part verbatim into `assignment.md`, the standards into `standards.md`. `deep` only extracts the JSON fields.
- Tracks not given: embed the brief's sentences, pull the top 300 abstracts, k-means (k 4 to 6), show `deep` the cluster titles, ask for 3 to 6 non-overlapping tracks. The first validator check reviews it.
- Seeds resolve through `titles.txt`, arXiv search, then Tavily. Failures stay `unresolved` in `seeds.json` with the searches tried, and reports must say "primary source not located".
- Mission 001: t3 weight 1.5.

Scheduling: one global queue, six paper slots, semaphores `reader` 6, `deep` 4, `s1` 16. A paper is read once. Its card may list several tracks and each gets its claims.

Tier B: every resolved seed plus each track's top 5 at report time. All chunks, the track's questions as extraction targets, output `deep/<id>.md`.

Reports: `report.md` is rewritten after 20 new cards or 2 h, and at the end. One `deep` call per deliverable (700 tokens), skipped when its input hash is unchanged. Code scores the ranked list (relevance, cited_by, quality, recency) and `deep` writes the justification.

Synthesis: draft at half time, final at `until` minus 40 min. One call per synthesis question over the digests, report summaries and contested claims, then one per "must present" item. Scouting stops at minus 75 min, final reports at minus 60, final push at minus 10.

New missions: push `missions/<NNN-slug>/brief.md` with front matter `mode: queue|preempt` and `hours: N`. Phase 2: `POST /api/research/missions` on the hub writes the same file. `queue` starts when the current mission is done. `preempt` lets in-flight papers finish (5 min), writes quick reports, marks the mission `paused` and resumes it later. A paper carded in another mission is copied and re-tagged by one `deep` call, never re-read.

## 6. Validator protocol

Checks at +1 h (catches bad prompts early), +3.5 h, +6 h, +8.5 h. Each is one `git pull` and one read of `DIGEST.md`, rebuilt by code every 30 min, hard cap 60k characters:

```
# DIGEST <mission> seq <n> <utc>  run <h:mm>/<h:mm>  steering applied <seq>
## 1 Health      models, restarts, tok/s, papers/h, failures by reason, Tavily, disk, last push
## 2 Tracks      cards, queued, phase, rel 3 share, mean quote_pass, weight
## 3 Coverage    question id -> supported claims, zeros first
## 4 Top papers  top 10 per track: id | title | one_line
## 5 Card audit  per track 2 newest + 1 random: one_line, training_method, 2 results with quotes, repo, lint
## 6 Claims      new (10 per track) and every contested claim with both sides
## 7 Queries     last 5 per track with yield, dead patterns, refused loops
## 8 Board       questions to the validator, unresolved seeds
## 9 Anomalies   weak cards, duplicate one_lines, tracks under 20% rel 3, rejected count
## 10 Reports    executive summary per track, 120 words
```

I check health, drift (4), card honesty (5), contested claims (6), search gaps (3, 7), unresolved seeds (8), generic prose (10).

`steering.md` holds one YAML block, applied only when `seq` exceeds the last applied one:

```yaml
seq: 1
track_weights: {t3: 2.0, t5: 0.5}
priority_topics: [{track: t3, topic: "option discovery from video", boost: 0.3}]
seed_ids: [{id: "2506.09985", track: t1, name: "V-JEPA 2"}]   # box checks the title
banned: {queries: [], patterns: ["LLM world knowledge"], cats: ["cs.CR"], ids: []}
card_fixes: [{id: "2301.04104", action: reread|reject|patch, field: "", value: "", why: ""}]
claim_fixes: [{track: t1, claim: c12, action: retire|contest, why: ""}]
board_answers: [{id: b7, answer: ""}]
lessons: ["Predicting future frames alone is none_prediction_only."]
params: {max_chunks: 10, report_every_cards: 20, tavily_cap: 300}
stop: false
```

On each 60 s tick the box pulls, validates the block, applies each item and writes a result per item to `state/steering_applied.json`. `lessons` join the playbook, so every later prompt carries them. Results show in STATUS and the next DIGEST.

## 7. Fault tolerance

- Git: every 10 min `add -A`, commit, `pull --rebase`, push, never force. The box writes everything except `brief.md` and `steering.md`, the laptop only those, so rebases cannot conflict. Writes use tmp plus rename. Append files are opened per write (a pull truncates open handles).
- Resume: `scripts/research_boot.sh` installs, clones both repos, restores the corpus, starts models and daemon. The registry is replayed and `reading` entries older than 30 min become `queued`.
- Corpus: once the harvest and index finish, upload `arxiv_meta.tar.zst` and `vecs.f16.npy` as release assets of `sabha-research`. A fresh box downloads them.
- Watchdog (`serve.sh`): probe `/v1/models` every 30 s. Three misses, or no tokens for 5 min with requests in flight: kill the process group and relaunch. The client retries for 15 min. The daemon runs in a restart loop.
- Rate limits: arxiv.org one request per 3 s, search one per 15 s. A 429 or 503 backs off from 30 s to 10 min per host.
- Caps: `max_tokens` triage 40, gate 120, map 500, reduce 1500, consolidate 900, report 700. Timeout 240 s, reduce 420 s. A 30 character string repeated 4 times aborts the call. 12 min per paper, 2 attempts, then `failed`.
- Disk: models on `/`. Purge scratch under 2 GB free on `/kaggle/working`.
- Ping: `tail -n1 state/status_line.txt`, like `14:02 up 3h10 cards 141 q 63 pph 44 deep ok reader ok push 4m tav 61/300 steer 2`.
- `STATUS.md`, rebuilt every 2 min: track table, last 10 cards, model health, errors, steering results.

## 8. Build order (2 h)

1. 0:00 to 0:20. Start `s1`, then `deep`. Embed the harvested pages on GPU 1 (glob `**/page_*.jsonl`, title plus abstract, 256 tokens, fp16), then start `reader`. Later pages embed on CPU. Write `llm.py`, run `bench.py`.
2. 0:20 to 1:00, first slice: `prompts.py`, `memory.py`, `paper.py`, `gitsync.py`. Read 2506.09985 for t1: card, registry line, push. Check the quotes by hand.
3. 1:00 to 1:30: `index.py`, `sources.py`, `scout.py`, `consolidate.py`. Run t1 alone for 15 min. Triage gold set: brief seeds must score 2 or more, 20 random cs.CR abstracts 1 or less. Under 85% agreement means `s1=reader`.
4. 1:30 to 2:00: `planner.py`, `run.py`, `digest.py`, `steer.py`, watchdog. Start the run.
5. During the run (restart is safe): `report.py` before hour 2, corpus release, then the Research tab (`GET /api/research`, `web/js/research.js`).

## 9. Risks

- Shallow reading: every chunk is mapped separately against a field guide, `chunks_read/total` is on the card, tier B covers seeds and top 5.
- Invented repos, numbers, dates: must occur in the fetched text, then GitHub API and abs page verify. "not_stated" is always legal.
- Fame recall (Genie 4, SuperMimic): cards only from fetched text, unresolved seeds reported as not located.
- Query loops: Jaccard refusal, dead patterns, saturation.
- Drift: assignment questions in every packet, the gate, coverage by question id, validator weights and bans.
- Session death: 10 min pushes, half-time synthesis draft, release-backed corpus.
