"""Scripted demo harness.

Plays a believable agent run with no model and no GPU: a todo plan that moves
step by step, thinking, bash / read_file / write_file / todo_write / web_search
tool calls with results (one of them sometimes fails and the agent recovers),
token usage on every step_end, and a markdown report. It writes a few real
files into the workspace so the file browser has content.

The script depends on the agent's role (builder, scout, critic, lead) and on
topic words picked from the prompt. Delays are 0.4 to 2.5 s per event divided
by env SABHA_MOCK_SPEED (default 1.0; higher is faster). A fresh run takes
about 60 to 120 s at speed 1; a resumed run is shorter and quotes the note.
"""

from __future__ import annotations

import random
import re
import secrets
import time
from pathlib import Path

from ..util import mock_speed, short
from .base import Harness, RunContext

# ---------------------------------------------------------------- topic words

_STOP = set("""
a an the and or but if then else of to in on for with from by at as is are was were be been
being it its this that these those into over under about make build write create survey
summarise summarize explain find show give get use using run test tests check review read
search look tiny small simple quick main three two one four five some any all each how what why
which who please can could would should will may might must do does did done not no yes
scratch them their there here also very more most less much many few me my we our you your i
let lets new best good way ways out up via vs versus like just into onto than so such
""".split())

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#\-]*")


def _display(tok: str) -> str:
    return tok.lower() if tok[:1].isupper() and tok[1:].islower() else tok


def topic_of(text: str) -> tuple[str, str, list[str]]:
    """(phrase, slug, keywords) from free text.

    phrase: the first run of consecutive content words (max 4),
    slug: a file-name-safe version of its first two words,
    keywords: up to 6 distinct content words in order.
    """
    runs: list[list[str]] = []
    cur: list[str] = []
    keys: list[str] = []
    seen: set[str] = set()
    for tok in _TOKEN.findall(text or ""):
        low = tok.lower().strip("-")
        if low in _STOP or len(low) < 3 or low.isdigit():
            if cur:
                runs.append(cur)
                cur = []
            continue
        cur.append(_display(tok))
        if low not in seen:
            seen.add(low)
            keys.append(_display(tok))
    if cur:
        runs.append(cur)
    if not runs:
        return "the task", "task", ["task"]
    phrase = " ".join(runs[0][:4])
    slug = re.sub(r"[^a-z0-9]+", "_", " ".join(runs[0][:2]).lower()).strip("_") or "task"
    return phrase, slug, keys[:6] or ["task"]


def role_kind(agent: dict) -> str:
    """builder | scout | critic | lead, from the agent's name and role text."""
    text = f"{agent.get('name', '')} {agent.get('role', '')}".lower()
    if any(w in text for w in ("test", "review", "critic", "poke", "hole", "verif", "audit")):
        return "critic"
    if any(w in text for w in ("search", "read", "gather", "fact", "scout", "research", "survey")):
        return "scout"
    if any(w in text for w in ("plan", "lead", "report", "coordinat", "manag", "orchestr")):
        return "lead"
    return "builder"


def default_todos(kind: str, phrase: str, slug: str) -> list[str]:
    if kind == "scout":
        return [f"Collect sources on {phrase}", "Save the sources and take notes",
                "Cluster the ideas the sources agree on", "Cross-check each claim against a second source",
                "Write the summary"]
    if kind == "critic":
        return [f"List the riskiest assumptions behind {phrase}", "Write tests that poke at them",
                "Run the tests and triage failures", "Fix, re-run, confirm green", "Write the review"]
    if kind == "lead":
        return [f"Break {phrase} into pieces a small crew can own", "Write the plan and the assignments",
                f"Check what is already known about {phrase}", "Collect status and merge findings",
                "Report back"]
    return [f"Pin down what {phrase} must do and how to check it", "Write the plan to plan.md",
            f"Write a first working version ({slug}.py)", "Self-test it and fix what breaks",
            "Measure it and record the numbers", "Write the report"]


def mock_plan(prompt: str, agent: dict) -> list[str]:
    """A plausible 4 to 6 step plan for plan approval with a mock model."""
    phrase, slug, _ = topic_of(prompt)
    steps = default_todos(role_kind(agent), phrase, slug)
    rng = random.Random(prompt)
    if len(steps) > 4 and rng.random() < 0.5:
        steps.pop(rng.randrange(1, len(steps) - 1))
    return steps


# ---------------------------------------------------------------- file bodies

_TOKENIZER_PY = '''"""Tiny byte-level BPE tokenizer, from scratch.

Demo output written by the Sabha mock harness. The code is real and runs:
    python __NAME__             train, encode, round-trip check
    python __NAME__ --selftest  quick correctness checks
    python __NAME__ --bench     rough speed numbers
"""
import sys
import time
from collections import Counter

SAMPLE = (
    "the quick brown fox jumps over the lazy dog. "
    "tokenizers turn text into numbers, and numbers back into text. "
) * 12


def merge(ids, pair, new_id):
    """Replace every occurrence of `pair` in `ids` with `new_id`."""
    out, i = [], 0
    while i < len(ids):
        if i + 1 < len(ids) and ids[i] == pair[0] and ids[i + 1] == pair[1]:
            out.append(new_id)
            i += 2
        else:
            out.append(ids[i])
            i += 1
    return out


def train(text, vocab_size=300):
    """Learn merges greedily: always merge the most frequent adjacent pair."""
    ids = list(text.encode("utf-8"))
    merges = {}
    for new_id in range(256, vocab_size):
        pairs = Counter(zip(ids, ids[1:]))
        if not pairs:
            break
        pair = max(pairs, key=pairs.get)
        merges[pair] = new_id
        ids = merge(ids, pair, new_id)
    return merges


def encode(text, merges):
    ids = list(text.encode("utf-8"))
    for pair, new_id in merges.items():  # dicts keep training order
        ids = merge(ids, pair, new_id)
    return ids


def decode(ids, merges):
    vocab = {i: bytes([i]) for i in range(256)}
    for (a, b), new_id in merges.items():
        vocab[new_id] = vocab[a] + vocab[b]
    return b"".join(vocab[i] for i in ids).decode("utf-8", errors="replace")


def selftest():
    merges = train(SAMPLE, 280)
    for text in ["", "a", "the fox", "naive cafe", SAMPLE[:200]]:
        assert decode(encode(text, merges), merges) == text, text
    assert len(encode(SAMPLE, merges)) < len(SAMPLE.encode("utf-8"))
    print("selftest ok (6 checks)")


def bench(repeats=20):
    t0 = time.perf_counter()
    merges = train(SAMPLE, 300)
    t_train = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(repeats):
        ids = encode(SAMPLE, merges)
    t_enc = (time.perf_counter() - t0) / repeats
    n_bytes = len(SAMPLE.encode("utf-8"))
    print(f"train   {t_train * 1000:.1f} ms  ({len(merges)} merges)")
    print(f"encode  {n_bytes / t_enc / 1e6:.2f} MB/s")
    print(f"ratio   {n_bytes / len(ids):.2f} bytes per token")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--bench" in sys.argv:
        bench()
    else:
        m = train(SAMPLE, 300)
        ids = encode(SAMPLE, m)
        assert decode(ids, m) == SAMPLE
        print(f"{len(SAMPLE.encode())} bytes -> {len(ids)} tokens with a {256 + len(m)}-token vocab")
'''

_GENERIC_PY = '''"""__TITLE__: a first small working version.

Demo output written by the Sabha mock harness. It is real, runnable Python,
but the task itself was not solved by a model.
    python __NAME__             profile the sample text
    python __NAME__ --selftest  quick checks
    python __NAME__ --bench     rough speed numbers
"""
import sys
import time
from collections import Counter

SAMPLE = __SAMPLE__


def words(text):
    """Lower-case alphanumeric words."""
    return "".join(c if c.isalnum() else " " for c in text.lower()).split()


def profile(text):
    ws = words(text)
    bigrams = Counter(zip(ws, ws[1:]))
    top = bigrams.most_common(1)[0][0] if bigrams else None
    return {"words": len(ws), "unique": len(set(ws)), "top_bigram": top}


def selftest():
    assert words("") == []
    assert words("A b, a!") == ["a", "b", "a"]
    assert profile("x y x y")["top_bigram"] == ("x", "y")
    print("selftest ok (3 checks)")


def bench(repeats=2000):
    t0 = time.perf_counter()
    for _ in range(repeats):
        profile(SAMPLE)
    dt = time.perf_counter() - t0
    print(f"{repeats / dt:,.0f} profiles/s on a {len(SAMPLE)}-char sample")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--bench" in sys.argv:
        bench()
    else:
        print(profile(SAMPLE))
'''

_CRITIC_PY = '''"""Checks around __PHRASE__ (demo output written by the Sabha mock harness).

Run: python __NAME__
"""


def normalise(text):
    """Collapse whitespace and fold case so equal strings compare equal."""
    return " ".join(text.__FOLD__().split())


def test_idempotent():
    s = "  Mixed   CASE text "
    assert normalise(normalise(s)) == normalise(s)


def test_empty():
    assert normalise("") == ""


def test_casefold():
    assert normalise("Stra\\u00dfe") == normalise("STRASSE")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
            except AssertionError:
                failed += 1
                print(f"FAILED {name}")
    print(f"{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
'''


def _facts(low: str, phrase: str) -> list[tuple[str, str]]:
    """Three (headline, explanation) pairs for the scout's notes and summary."""
    if "jepa" in low or "world model" in low:
        return [
            ("Predict embeddings, not pixels",
             "A JEPA encodes a context view and a target view, and trains a predictor to output the "
             "target's embedding from the context's. The loss lives in representation space, so the "
             "model does not have to spend capacity on unpredictable pixel detail."),
            ("Avoid collapse with asymmetry",
             "The target encoder is a moving average (EMA) of the context encoder and gets no gradient. "
             "With large masked target blocks this keeps the trivial constant solution away. "
             "Variance and covariance penalties (VICReg style) are the other common guard."),
            ("Plan in latent space",
             "With an action-conditioned predictor the same machinery becomes a world model: roll the "
             "predictor forward under candidate actions and score the predicted embeddings against a "
             "goal embedding (for example with CEM or MPPI) instead of generating video."),
        ]
    if "token" in low:
        return [
            ("Byte-level BPE is the default",
             "Start from 256 byte tokens and keep merging the most frequent adjacent pair. "
             "Every string is encodable, so there is no unknown token."),
            ("Vocabulary size is a trade",
             "A bigger vocabulary gives shorter sequences but a bigger embedding table and softmax, "
             "and more rare tokens that are poorly trained."),
            ("Pre-tokenisation matters as much as merging",
             "Splitting on spaces, digits and punctuation before merging changes compression and "
             "how numbers are handled downstream."),
        ]
    return [
        (f"Name the bottleneck {phrase} removes",
         f"Most write-ups open with one concrete bottleneck that {phrase} addresses; the rest of the "
         "design follows from it, so that is the first thing to pin down."),
        ("The core mechanism is simple; the details carry it",
         "The headline idea fits in a paragraph. The results depend on a few unglamorous choices "
         "(data, normalisation, schedules) that are easy to miss."),
        ("Evaluation is the weak spot",
         "Claims are often measured on small or friendly benchmarks, and a second source rarely "
         "reproduces the headline number exactly. Treat single numbers with care."),
    ]


_TOOL_TIME = {
    "bash": (1.0, 2.5),
    "web_search": (1.2, 2.5),
    "read_file": (0.5, 1.0),
    "write_file": (0.5, 1.1),
    "todo_write": (0.4, 0.7),
}

DEMO_LINE = "_Scripted demo run by the mock harness: no model or GPU did this work._"


class _Stopped(Exception):
    pass


class _Play:
    """One scripted run. Each script method is plain sequential code."""

    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self.speed = mock_speed()
        m = ctx.mission
        self.rng = random.Random(f"{m.get('id', '')}:{ctx.run}")
        self.prompt = m.get("prompt") or ctx.task
        self.title = m.get("title") or short(self.prompt, 60)
        self.low = self.prompt.lower()
        self.phrase, self.slug, self.keys = topic_of(self.prompt)
        self.kind = role_kind(ctx.agent)
        self.name = ctx.agent.get("name") or "Agent"
        self.cwd = Path(ctx.cwd)
        self.turn = 1
        self.step_no = 0
        self.calls = 0
        self.ctx_tokens = self.rng.randint(1900, 3400)
        self.todos: list[dict] = []
        self.cursor = -1
        self.written: list[str] = []
        self.session = ctx.session_id or f"mock-{secrets.token_hex(4)}"

    # ------------------------------------------------------------ plumbing

    def emit(self, type_: str, data: dict) -> None:
        self.ctx.emit(type_, data)

    async def pause(self, lo: float, hi: float) -> None:
        if await self.ctx.sleep(self.rng.uniform(lo, hi) / self.speed):
            raise _Stopped()

    async def step(self, think: str | None = None, say: str | None = None, tools=()) -> None:
        tools = [t for t in tools if t]
        if not (think or say or tools):
            return
        self.step_no += 1
        await self.pause(0.4, 0.9)
        self.emit("run.status", {"phase": "step_start", "turn": self.turn, "step": self.step_no})
        if think:
            await self.pause(1.1, 2.5)
            self.emit("run.thinking", {"text": think})
        if say:
            await self.pause(0.6, 1.4)
            self.emit("run.text", {"text": say})
        for name, inp, fn in tools:
            self.calls += 1
            call_id = f"call_{self.calls:02d}_{self.rng.getrandbits(24):06x}"
            await self.pause(0.4, 1.0)
            self.emit("run.tool_call", {"call_id": call_id, "tool": name, "input": inp})
            lo, hi = _TOOL_TIME.get(name, (0.5, 1.4))
            await self.pause(lo, hi)
            status, result, error = fn()
            data = {"call_id": call_id, "status": status, "result": result}
            if error:
                data["error"] = error
            self.emit("run.tool_result", data)
        await self.pause(0.4, 0.8)
        out = self.rng.randint(70, 560)
        usage = {"input": self.ctx_tokens, "output": out}
        self.ctx_tokens += out + self.rng.randint(120, 900)
        self.emit("run.status", {"phase": "step_end", "turn": self.turn, "step": self.step_no,
                                 "usage": usage})

    # ------------------------------------------------------------ tools

    def t_todo(self):
        snap = [{"content": t["content"], "status": t["status"]} for t in self.todos]
        done = sum(t["status"] == "completed" for t in snap)
        doing = sum(t["status"] == "in_progress" for t in snap)
        msg = f"Todo list updated: {done} done, {doing} in progress, {len(snap) - done - doing} pending."
        return ("todo_write", {"todos": snap}, lambda: ("completed", msg, None))

    def advance(self):
        """Finish the current todo and start the next. None when the list is used up."""
        if self.cursor >= len(self.todos) - 1:
            return None
        if self.cursor >= 0:
            self.todos[self.cursor]["status"] = "completed"
        self.cursor += 1
        self.todos[self.cursor]["status"] = "in_progress"
        return self.t_todo()

    def finish_todos(self):
        for t in self.todos:
            if t["status"] != "cancelled":
                t["status"] = "completed"
        self.cursor = len(self.todos)
        return self.t_todo() if self.todos else None

    def t_write(self, rel: str, content: str):
        def run():
            path = self.cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if rel not in self.written:
                self.written.append(rel)
            return "completed", f"Wrote {len(content.encode('utf-8'))} bytes to {rel}", None
        return ("write_file", {"path": rel, "content": content}, run)

    def t_read(self, rel: str, fail: str | None = None):
        def run():
            if fail:
                return "error", None, fail
            try:
                text = (self.cwd / rel).read_text("utf-8")
            except OSError:
                return "error", None, f"ENOENT: no such file or directory, open '{rel}'"
            return "completed", text if len(text) < 4000 else text[:4000] + "\n...(truncated)", None
        return ("read_file", {"path": rel}, run)

    def t_bash(self, cmd: str, output, fail: str | None = None):
        def run():
            out = output() if callable(output) else output
            return ("error", out, fail) if fail else ("completed", out, None)
        return ("bash", {"command": cmd}, run)

    def t_search(self, query: str, fail: str | None = None):
        def run():
            if fail:
                return "error", None, fail
            return "completed", self.search_results(query), None
        return ("web_search", {"query": query, "max_results": 5}, run)

    def listing(self) -> str:
        rows = []
        total = 0
        try:
            entries = sorted(self.cwd.iterdir())
        except OSError:
            entries = []
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            stamp = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
            mode = "drwxr-xr-x" if p.is_dir() else "-rw-r--r--"
            rows.append(f"{mode} 1 agent agent {st.st_size:6d} {stamp} {p.name}")
        head = f"total {max(1, total // 1024) if rows else 0}"
        return "\n".join([head, "drwxr-xr-x 2 agent agent   4096 . ", *rows])

    def line_counts(self) -> str:
        rows = []
        for p in sorted(self.cwd.glob("*.md")):
            try:
                n = len(p.read_text("utf-8", errors="replace").splitlines())
            except OSError:
                continue
            rows.append(f"{n:5d} {p.name}")
        return "\n".join(rows) or "wc: *.md: No such file or directory"

    def search_results(self, query: str) -> str:
        p = self.phrase
        k = self.keys
        titles = [
            f"A practical guide to {p}",
            f"{p.capitalize()}: what the benchmarks miss",
            f"Notes on {k[-1] if k else p}, with worked examples",
            f"Why {p} works (and when it does not)",
            f"{p.capitalize()} from first principles",
        ]
        self.rng.shuffle(titles)
        hosts = ["blog.example.org", "papers.example.org", "notes.example.net", "wiki.example.com"]
        lines = [f'Results for "{query}":']
        for i, t in enumerate(titles[:4], 1):
            lines.append(f"{i}. {t}\n   {hosts[i - 1]} - {self.rng.choice(['tutorial', 'survey', 'critique', 'lecture notes'])}")
        lines.append("[mock search: these results are generated, nothing was fetched]")
        return "\n".join(lines)

    # ------------------------------------------------------------ entry

    async def go(self) -> None:
        ctx = self.ctx
        await self.pause(0.4, 0.8)
        self.emit("run.started", {"run": ctx.run, "harness": "mock",
                                  "model_id": (ctx.model or {}).get("id", ""),
                                  "session_id": self.session, "cwd": ctx.cwd, "resumed": ctx.resumed})
        await self.pause(0.4, 0.6)
        self.emit("run.status", {"phase": "turn_start", "turn": self.turn})
        if ctx.resumed:
            report = await self.followup()
        else:
            approved = [s.get("text", "") for s in ctx.mission.get("plan") or [] if s.get("text")]
            steps = approved or default_todos(self.kind, self.phrase, self.slug)
            self.todos = [{"content": s, "status": "pending"} for s in steps]
            report = await getattr(self, self.kind)()
        await self.step(say="All steps done. Writing up the report.", tools=[self.finish_todos()])
        await self.pause(0.6, 1.5)
        self.emit("run.status", {"phase": "turn_end", "turn": self.turn, "reason": {"kind": "completed"}})
        await self.pause(0.4, 0.8)
        self.emit("run.final", {"text": report, "reason": {"kind": "completed"}, "session_id": self.session})

    # ------------------------------------------------------------ builder

    def plan_md(self) -> str:
        lines = [f"# Plan: {self.title}", "", f"Owner: {self.name}", ""]
        lines += [f"{i}. {t['content']}" for i, t in enumerate(self.todos, 1)]
        lines += ["", "## Done means", f"- {self.phrase} runs end to end on a sample input",
                  "- a self-test passes", "- one honest number is recorded in results.md", ""]
        return "\n".join(lines)

    async def builder(self) -> str:
        p, rng = self.phrase, self.rng
        tok = "token" in self.low
        code_name = "tokenizer.py" if tok else f"{self.slug}.py"
        if tok:
            code = _TOKENIZER_PY.replace("__NAME__", code_name)
        else:
            code = (_GENERIC_PY.replace("__TITLE__", self.title).replace("__NAME__", code_name)
                    .replace("__SAMPLE__", repr(self.prompt)))

        await self.step(
            think=(f"Task: “{short(self.prompt, 140)}”. Before writing code I want to pin down "
                   f"what {p} has to do, how I will know it works, and which single number I will "
                   "report at the end."),
            say=f"Starting on {p}. Plan first, then a small working version, then measure it.",
            tools=[self.advance()])
        await self.step(tools=[self.t_bash("ls -la", self.listing),
                               self.t_bash("python3 --version", f"Python 3.{rng.choice([10, 11, 12])}.{rng.randint(1, 12)}")])
        await self.step(
            think=("Empty workspace, so nothing to stay compatible with. Standard library only, so it "
                   "runs anywhere. I'll write the plan down so the steps stay visible."),
            tools=[self.advance(), self.t_write("plan.md", self.plan_md())])
        await self.step(tools=[self.t_read("plan.md")])
        if tok:
            design = ("Design: byte-level BPE. Start from the 256 byte values, count adjacent pairs, "
                      "merge the most frequent pair into a new id, repeat until the vocab is full. "
                      "Encoding replays the merges in training order; decoding concatenates bytes. "
                      "Quadratic, but fine for a first version.")
        else:
            design = (f"Design: keep the first version of {p} tiny and checkable. One pure function "
                      "that does the core step, a --selftest flag with asserts, and a --bench flag so "
                      "the measurement is reproducible.")
        await self.step(say="Plan is written. Moving to the first version.", tools=[self.advance()])
        await self.step(think=design, tools=[self.t_write(code_name, code)])
        await self.step(
            think="Reading it back once before running it: the cheapest bug to fix is the one caught by eye.",
            tools=[self.t_read(code_name)])
        if tok:
            plain = f"1296 bytes -> {rng.randint(420, 470)} tokens with a 300-token vocab"
        else:
            plain = f"{{'words': {rng.randint(8, 30)}, 'unique': {rng.randint(6, 20)}, 'top_bigram': None}}"
        await self.step(tools=[self.t_bash(f"python {code_name}", plain)])
        await self.step(tools=[self.t_bash(f"wc -l {code_name} plan.md",
                                           f"  {code.count(chr(10))} {code_name}\n  {self.plan_md().count(chr(10))} plan.md")])
        await self.step(say="It runs. Now the checks.", tools=[self.advance()])
        selftest = "selftest ok (6 checks)" if tok else "selftest ok (3 checks)"
        stumbled = rng.random() < 0.7
        if stumbled:
            await self.step(tools=[self.t_bash("python -m pytest -q", "/usr/bin/python3: No module named pytest",
                                               fail="command exited with code 1")])
            await self.step(
                think=("pytest is not installed on this box and a dependency is not worth it for a "
                       "smoke check. The script has a built-in --selftest, so I'll use that instead."),
                tools=[self.t_bash(f"python {code_name} --selftest", selftest)])
        else:
            await self.step(tools=[self.t_bash(f"python {code_name} --selftest", selftest)])
        await self.step(say="Self-test passes. Now the measurement.", tools=[self.advance()])
        if tok:
            train_ms = rng.uniform(4, 12)
            mbs = rng.uniform(0.22, 0.48)
            ratio = rng.uniform(2.75, 3.05)
            bench = f"train   {train_ms:.1f} ms  (44 merges)\nencode  {mbs:.2f} MB/s\nratio   {ratio:.2f} bytes per token"
            table = (f"| metric | value |\n|---|---|\n| train time | {train_ms:.1f} ms (44 merges) |\n"
                     f"| encode speed | {mbs:.2f} MB/s |\n| compression | {ratio:.2f} bytes per token |")
            bench_thought = ("Benchmark: time training once, then encode the sample 20 times and take the "
                             "mean. Report MB/s and bytes per token, since compression is the point of a "
                             "tokenizer.")
        else:
            rate = rng.randint(90000, 220000)
            bench = f"{rate:,} profiles/s on a {len(self.prompt)}-char sample"
            table = f"| metric | value |\n|---|---|\n| throughput | {rate:,} profiles/s |\n| sample | {len(self.prompt)} chars |"
            bench_thought = (f"Benchmark: run the core step of {p} 2000 times on the sample and report "
                             "throughput. Not a real workload, but it gives a baseline to compare against.")
        await self.step(think=bench_thought, tools=[self.t_bash(f"python {code_name} --bench", bench)])
        if rng.random() < 0.5:
            await self.step(
                think="The first run includes warm-up. One more run to see whether the number is stable.",
                tools=[self.t_bash(f"python {code_name} --bench", bench)])
        results = "\n".join([
            f"# Results: {self.title}", "", table, "",
            "## Notes",
            "- pure Python, standard library only",
            "- numbers come from a single small sample; treat them as a baseline, not a claim",
            "",
        ])
        await self.step(tools=[self.advance(), self.t_write("results.md", results)])
        await self.step(
            think="Re-reading results.md so the numbers in the report match the file exactly.",
            tools=[self.t_read("results.md")])
        nexts = (["- replace the quadratic merge loop with a priority queue",
                  "- train on a real corpus and compare with a library tokenizer"]
                 if tok else [f"- run {p} on a real input instead of the sample",
                              "- add a second metric that matters to the user"])
        return "\n".join([
            f"## {self.title}", "",
            f"**Summary.** A first working version of {p} is in `{code_name}`. It passes its self-test "
            "and has a baseline measurement.", "",
            "### What I did",
            "1. Wrote a short plan with a definition of done (`plan.md`).",
            f"2. Wrote `{code_name}` using only the standard library.",
            "3. Checked it with the built-in `--selftest`" + (" (pytest was not installed)." if stumbled else "."),
            "4. Measured it with `--bench` and recorded the numbers.", "",
            "### Results", table, "",
            "### Files",
            "- `plan.md` - the plan", f"- `{code_name}` - the code", "- `results.md` - the numbers", "",
            "### Next steps", *nexts, "", DEMO_LINE,
        ])

    # ------------------------------------------------------------ scout

    async def scout(self) -> str:
        p, rng = self.phrase, self.rng
        facts = _facts(self.low, p)
        await self.step(
            think=(f"I need a map of {p} before I can summarise anything. Plan: gather a handful of "
                   "sources, take notes per source, then keep only the ideas that more than one source "
                   "agrees on."),
            say=f"Researching {p}. Sources and notes will be in the workspace as I go.",
            tools=[self.advance()])
        if rng.random() < 0.65:
            await self.step(tools=[self.t_search(f"{p} overview",
                                                 fail="HTTP 429 Too Many Requests: search quota hit, retry after 2 s")])
            await self.step(think="Rate limited. Waiting a moment and retrying with a narrower query.",
                            tools=[self.t_search(f"{p} key ideas")])
        else:
            await self.step(tools=[self.t_search(f"{p} key ideas")])
        sources = "\n".join([f"# Sources: {p}", ""] + [
            f"{i}. {t} ({h})" for i, (t, h) in enumerate([
                (f"A practical guide to {p}", "blog.example.org"),
                (f"{p.capitalize()}: what the benchmarks miss", "papers.example.org"),
                (f"Why {p} works (and when it does not)", "notes.example.net"),
                (f"{p.capitalize()} from first principles", "wiki.example.com"),
            ], 1)] + ["", "(mock sources: generated, not fetched)", ""])
        await self.step(
            think="Four usable hits: two tutorials, one critique, one reference. That mix is good for checking claims.",
            tools=[self.advance(), self.t_write("sources.md", sources)])
        await self.step(tools=[self.t_search(f"{p} limitations")])
        await self.step(tools=[self.t_search(f"{p} tutorial worked example")])
        notes = "\n".join([f"# Notes: {p}", ""] + [
            f"## {h}\n{body}\n" for h, body in facts])
        await self.step(
            think=f"First pass over the sources. The idea that keeps coming back: {facts[0][0].lower()}.",
            say=f"Taking notes. Strongest thread so far: {facts[0][0].lower()}.",
            tools=[self.t_write("notes.md", notes)])
        await self.step(tools=[self.t_read("sources.md")])
        await self.step(
            think=(f"Second thread: {facts[1][0].lower()}. Two of the four sources say it in almost the "
                   "same words, which is either strong agreement or one copying the other. Noting both options."),
            tools=[self.t_read("notes.md")])
        await self.step(
            think=("Clustering: " + "; ".join(h.lower() for h, _ in facts)
                   + ". Everything else in the notes is detail under one of these three."),
            tools=[self.advance()])
        await self.step(
            think=f"Third thread: {facts[2][0].lower()}. This is the one the critique pushes on hardest.",
            say="Three ideas found. Checking them against the critique before I write anything final.")
        await self.step(
            think=f"Cross-check: does the critique disagree with any of the three? It mostly argues about evaluation, not the ideas.",
            tools=[self.t_search(f"{self.keys[0]} criticism"), self.advance()])
        summary = "\n".join([f"# Summary: {self.title}", ""] + [
            f"{i}. **{h}.** {body}" for i, (h, body) in enumerate(facts, 1)] + ["", DEMO_LINE, ""])
        await self.step(tools=[self.advance(), self.t_write("summary.md", summary)])
        await self.step(think="Re-reading the summary for any sentence I cannot back with a source.",
                        tools=[self.t_read("summary.md")])
        return "\n".join([
            f"## {self.title}", "",
            f"**Summary.** The sources on {p} agree on three ideas:", "",
            *[f"{i}. **{h}.** {body}" for i, (h, body) in enumerate(facts, 1)], "",
            "### Files",
            "- `sources.md` - where the claims came from",
            "- `notes.md` - per-idea notes",
            "- `summary.md` - this summary", "",
            "### Caveat",
            "The critique source mostly questions evaluation, not the ideas themselves. "
            "Check headline numbers against a second source before quoting them.", "",
            DEMO_LINE,
        ])

    # ------------------------------------------------------------ critic

    async def critic(self) -> str:
        p, rng = self.phrase, self.rng
        test_name = f"test_{self.slug}.py"
        v1 = _CRITIC_PY.replace("__PHRASE__", p).replace("__NAME__", test_name).replace("__FOLD__", "lower")
        v2 = v1.replace("text.lower()", "text.casefold()")
        claims = "\n".join([
            f"# Assumptions behind {p}", "",
            "1. Inputs that look equal are treated as equal (case, spacing, Unicode).",
            "2. Empty input is handled, not crashed on.",
            "3. Running the step twice gives the same result as running it once.", "",
            "Each one gets a test in " + test_name + ".", "",
        ])
        await self.step(
            think=(f"My job is to find where {p} breaks before a user does. Start from the assumptions "
                   "it silently makes, then write the smallest test for each."),
            say=f"Reviewing {p}. I'll list assumptions first, then test them.",
            tools=[self.advance()])
        if rng.random() < 0.65:
            await self.step(tools=[self.t_read(f"src/{self.slug}.py")])
            await self.step(
                think="Wrong guess: there is no src/ folder in this workspace. Listing what is actually here.",
                tools=[self.t_bash("ls -la", self.listing)])
        else:
            await self.step(tools=[self.t_bash("ls -la", self.listing)])
        await self.step(
            think="Nothing to review in place yet, so I'll test the assumptions directly with a reference helper.",
            tools=[self.t_write("claims.md", claims)])
        await self.step(
            think=("Ranking the assumptions by how quietly they fail. Equality of 'equal-looking' input "
                   "is first: it never crashes, it just gives wrong answers."),
            tools=[self.t_read("claims.md")])
        await self.step(tools=[self.advance(), self.t_write(test_name, v1)])
        await self.step(tools=[self.t_bash(f"grep -n \"def test_\" {test_name}",
                                           "12:def test_idempotent():\n17:def test_empty():\n21:def test_casefold():")])
        await self.step(say="Tests written. Running them.", tools=[self.advance()])
        await self.step(tools=[self.t_bash(f"python {test_name}", "FAILED test_casefold\n2 passed, 1 failed\n[exit code 1]")])
        await self.step(
            think=("test_casefold fails: lower() maps 'Straße' to 'straße' but 'STRASSE' to "
                   "'strasse'. casefold() is the Unicode-aware version and folds ß to 'ss'. "
                   "That is exactly the kind of silent mismatch this review is for."),
            tools=[self.advance(), self.t_write(test_name, v2)])
        await self.step(tools=[self.t_bash(f"python {test_name}", "3 passed, 0 failed")])
        if rng.random() < 0.5:
            await self.step(think="Running once more to make sure the pass is not order-dependent.",
                            tools=[self.t_bash(f"python {test_name}", "3 passed, 0 failed")])
        review = "\n".join([
            f"# Review: {self.title}", "",
            "| check | result |", "|---|---|",
            "| idempotent | pass |", "| empty input | pass |",
            "| Unicode case folding | **failed with lower(), fixed with casefold()** |", "",
            "## Verdict",
            "One real bug found and fixed. The other two assumptions hold.", "",
        ])
        await self.step(
            think=("What is still untested: very long inputs and mixed scripts. Not blocking, but worth "
                   "saying in the review so nobody assumes they were covered."))
        await self.step(tools=[self.advance(), self.t_write("review.md", review)])
        await self.step(tools=[self.t_read("review.md")])
        return "\n".join([
            f"## Review: {self.title}", "",
            f"**Verdict.** One real bug in how {p} compares text: `lower()` is not Unicode-safe. "
            "Fixed with `casefold()`; all three checks now pass.", "",
            "| check | result |", "|---|---|",
            "| idempotent | pass |", "| empty input | pass |", "| Unicode case folding | fixed |", "",
            "### Files",
            "- `claims.md` - the assumptions under test",
            f"- `{test_name}` - the tests (run with `python {test_name}`)",
            "- `review.md` - the verdict", "",
            DEMO_LINE,
        ])

    # ------------------------------------------------------------ lead

    async def lead(self) -> str:
        p, rng = self.phrase, self.rng
        crew = [a.get("name") for a in self.ctx.crew if a.get("name")] or ["Scout", "Builder", "Critic"]
        while len(crew) < 3:
            crew.append(["Scout", "Builder", "Critic"][len(crew)])
        scout, builder, critic = crew[0], crew[1], crew[2]
        await self.step(
            think=(f"Goal: {short(self.prompt, 140)}. My job is to cut {p} into pieces small enough "
                   "that each one has an owner and a clear done-check, then merge what comes back."),
            say=f"Taking the lead on {p}. Plan and assignments first.",
            tools=[self.advance()])
        if rng.random() < 0.65:
            await self.step(tools=[self.t_bash("cat status.json", "cat: status.json: No such file or directory",
                                               fail="command exited with code 1")])
            await self.step(
                think="No status file yet, so this is a fresh start. Creating one so the crew has a shared place to report.",
                tools=[self.t_write("status.json", '{\n  "phase": "planning",\n  "owners": {}\n}\n')])
        else:
            await self.step(tools=[self.t_bash("ls -la", self.listing)])
        plan = "\n".join([f"# Plan: {self.title}", ""] + [
            f"{i}. {t['content']}" for i, t in enumerate(self.todos, 1)] + [""])
        await self.step(
            think=f"Three pieces: find out what is known ({scout}), build the smallest working thing ({builder}), and try to break it ({critic}).",
            tools=[self.advance(), self.t_write("plan.md", plan)])
        assignments = "\n".join([
            f"# Assignments: {p}", "",
            "| who | what | done when |", "|---|---|---|",
            f"| {scout} | survey prior work on {p} | three ideas with sources |",
            f"| {builder} | first working version | self-test passes, one number recorded |",
            f"| {critic} | tests and review | every assumption has a test |", "",
        ])
        await self.step(tools=[self.t_write("assignments.md", assignments)])
        await self.step(tools=[self.t_read("plan.md")])
        await self.step(say="Assignments are out. Checking what is already known.",
                        tools=[self.advance(), self.t_search(f"{p} prior work")])
        await self.step(
            think=f"Prior work exists, so {builder} should not start from a blank page: the first version should copy the simplest known recipe and measure it.",
            tools=[self.t_read("assignments.md")])
        await self.step(tools=[self.t_search(f"{p} common mistakes")])
        await self.step(
            think=(f"The common mistakes are mostly about measuring the wrong thing. Adding that to "
                   f"{critic}'s brief: check that the number {builder} reports is the one the goal cares about."),
            say=f"Adding a measurement check to {critic}'s brief.")
        await self.step(tools=[self.advance(), self.t_write("status.json",
                        '{\n  "phase": "merging",\n  "owners": {"%s": "survey", "%s": "build", "%s": "review"}\n}\n'
                        % (scout, builder, critic))])
        report = "\n".join([
            f"# Report: {self.title}", "",
            f"- {scout}: map the known ideas first",
            f"- {builder}: smallest working version, one honest number",
            f"- {critic}: one test per assumption", "",
        ])
        await self.step(
            think="Merging: the order matters. Survey first, build second, review last, so nobody builds the wrong thing.",
            tools=[self.t_write("report.md", report)])
        await self.step(tools=[self.t_bash("ls -la", self.listing)])
        await self.step(tools=[self.advance(), self.t_read("report.md")])
        return "\n".join([
            f"## {self.title}", "",
            f"**Summary.** {p.capitalize()} is split into three owned pieces with clear done-checks.", "",
            "| who | what | done when |", "|---|---|---|",
            f"| {scout} | survey prior work | three ideas with sources |",
            f"| {builder} | first working version | self-test passes, one number recorded |",
            f"| {critic} | tests and review | every assumption has a test |", "",
            "### Order", f"1. {scout} maps what is known.", f"2. {builder} builds the simplest known recipe.",
            f"3. {critic} tries to break it.", "",
            "### Files", "- `plan.md`, `assignments.md`, `status.json`, `report.md`", "",
            DEMO_LINE,
        ])

    # ------------------------------------------------------------ follow-up run

    def note_text(self) -> str:
        text = self.ctx.task.replace("Notes from the human:", " ").replace("Follow-up:", " ")
        text = re.sub(r"(^|\n)\s*-\s+", r"\1", text)
        return " / ".join(part.strip() for part in text.splitlines() if part.strip()) or "carry on"

    async def followup(self) -> str:
        note = self.note_text()
        prev = [s for s in self.ctx.mission.get("plan") or [] if s.get("text")]
        self.todos = [{"content": s["text"], "status": "completed"} for s in prev]
        self.todos.append({"content": f"Follow-up: {short(note, 60)}", "status": "pending"})
        self.cursor = len(self.todos) - 2
        try:
            files = sorted(x.name for x in self.cwd.iterdir() if x.is_file())
        except OSError:
            files = []
        have = ", ".join(files[:4]) if files else "nothing yet"
        await self.step(
            think=(f"New input from the human: “{short(note, 160)}”. The workspace already has "
                   f"{have}, so I'll build on that instead of starting over."),
            say=f"Picking this back up. Your note: “{short(note, 90)}”.",
            tools=[self.advance()])
        await self.step(tools=[self.t_bash("ls -la", self.listing)])
        target = next((f for f in files if f.endswith(".md")), files[0] if files else None)
        if target:
            await self.step(tools=[self.t_read(target)])
        body = "\n".join([
            "# Follow-up", "", f"> {note}", "",
            f"Applied to the earlier work on {self.phrase}.",
            f"Checked against `{target}`." if target else "There was no earlier output to check against.",
            "",
        ])
        await self.step(
            think=(f"What does “{short(note, 70)}” change? Not the core of {self.phrase}, but it "
                   "shifts what the report should lead with."),
            say="Adjusting the result to your note.")
        await self.step(
            think="Folding the note into the existing result rather than redoing it. Writing down what changed so it is reviewable.",
            tools=[self.t_write("followup.md", body)])
        if self.rng.random() < 0.5:
            await self.step(tools=[self.t_bash("wc -l *.md", self.line_counts)])
        return "\n".join([
            f"### Follow-up on: {self.title}", "",
            f"> {note}", "",
            f"**Done.** I took the note into account and recorded the change in `followup.md`"
            + (f", building on `{target}`." if target else "."), "",
            DEMO_LINE,
        ])


class MockHarness(Harness):
    name = "mock"

    async def run(self, ctx: RunContext) -> None:
        try:
            await _Play(ctx).go()
            ctx.exit_code = 0
        except _Stopped:
            ctx.exit_code = 130
