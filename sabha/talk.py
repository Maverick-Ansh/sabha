"""Talk mode: threads of messages between the human and one agent.

Replies stream as ephemeral chat.delta events and end with a persisted
thread.message. Real models are called with POST {base_url}/chat/completions
(stream: true) and the SSE stream is parsed by hand: delta.content becomes
text, delta.reasoning_content / delta.reasoning becomes thinking, and inline
<think>...</think> is split out of the content. Mock models are answered by
the hub with a short scripted reply.

`stream_chat` is also used for plan proposals and by the chat harness.

A Talk reply is a conversation, not a task: the agent gets a system prompt that tells it where it is
and what the box remembers (recall.py), and four read-only tools it may call before it answers:
search_memory, read_memory, web_search, read_page. Up to MAX_HOPS rounds of tool calls per reply,
each shown in the app as a step. Long work (files, code, hours) belongs in a mission (Work).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time

import aiohttp

from . import webtools
from .harness.mock import topic_of
from .models import auth_headers
from .util import ApiError, mock_speed, new_id, now, short

log = logging.getLogger("sabha.talk")

CARD_MAX_CHARS = 6000
MAX_HOPS = 6
# a reply that ends by announcing a lookup it never made ("Let's start by searching memory.") gets one nudge
PROMISE = re.compile(r"\b(let me|let['’]s|i['’]ll|i will|start by|going to|i am going|i'm going)\b", re.I)
NUDGE = "(Sabha: go ahead now. Call the tools you said you would use, then answer the owner.)"

TALK_RULES = """You are {name}, one of the agents of Sabha: the owner's own AI team, running on their GPU box. {role}
{persona}
How to talk:
- Talk like a thoughtful colleague: warm, direct and curious. Small talk gets a short, friendly answer; a real question gets a full one.
- First understand what the owner wants. If a request is genuinely unclear, ask one short question. Otherwise go ahead.
- When something needs more than one step, say your plan in two to four short lines, then carry it out in this same reply. Never end a reply by saying you will look something up: call the tool right away.
- You can use the box's whole memory: research runs (briefs, paper cards, track reports, syntheses), councils (paper discussions and verdicts), Work missions and their reports, earlier chats, and the owner's research taste. Whenever a question touches anything done before, call search_memory first (try other words if it misses), read_memory for the details, then answer and name what you used by its id in brackets, like [card:001-x/2301.08243].
- For facts from the outside world, call web_search and read_page, and name your sources.
- Never invent papers, numbers or findings. If memory and the web have nothing, say so plainly.
- For long work (writing code or files, many steps, hours of reading), offer to turn it into a mission: the owner flips the composer to Mission, or starts a research run in the Research tab. Say what that mission would do.

WHAT THE BOX REMEMBERS (ids in brackets):
{overview}
{taste}Today is {today}."""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "Search everything this box has done: research runs, paper cards, track reports, syntheses, councils, "
                       "Work missions, earlier chats, the owner's research taste. Returns ids, titles and matching snippets.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "a few keywords"},
            "kind": {"type": "string", "description": "optional filter, comma separated: research, synthesis, report, digest, "
                                                      "card, council, mission, chat, taste"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_memory",
        "description": "Read one memory document by its id (6000 characters a part; ask for part 2, 3 ... for more).",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}, "part": {"type": "integer"}},
                       "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web. Returns titles, links and snippets.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_page",
        "description": "Read a web page or PDF by its link (an arXiv id works too). Optional find: a word to jump to.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "find": {"type": "string"}},
                       "required": ["url"]}}},
]


class ChatError(Exception):
    """The model endpoint could not be reached or answered with an error."""


# ---------------------------------------------------------------- <think> splitting


class ThinkSplitter:
    """Splits streamed content into ("text" | "thinking", piece) around <think> tags.

    Tags may arrive split across chunks, so a possible partial tag at the end
    of a chunk is held back until the next one.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.pending = ""

    @staticmethod
    def _partial(s: str, tags: tuple[str, ...]) -> int:
        best = 0
        for tag in tags:
            for k in range(min(len(tag) - 1, len(s)), 0, -1):
                if s.endswith(tag[:k]):
                    best = max(best, k)
                    break
        return best

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        s = self.pending + chunk
        self.pending = ""
        out: list[tuple[str, str]] = []
        while s:
            kind = "thinking" if self.inside else "text"
            tag = self.CLOSE if self.inside else self.OPEN
            i = s.find(tag)
            stray = -1 if self.inside else s.find(self.CLOSE)
            if stray != -1 and (i == -1 or stray < i):
                # a closing tag without an opening one: drop the tag
                if stray:
                    out.append((kind, s[:stray]))
                s = s[stray + len(self.CLOSE):]
                continue
            if i != -1:
                if i:
                    out.append((kind, s[:i]))
                s = s[i + len(tag):]
                self.inside = not self.inside
                continue
            hold = self._partial(s, (tag,) if self.inside else (self.OPEN, self.CLOSE))
            body = s[: len(s) - hold]
            self.pending = s[len(s) - hold:]
            if body:
                out.append((kind, body))
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        out = [("thinking" if self.inside else "text", self.pending)] if self.pending else []
        self.pending = ""
        return out


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return text.replace("</think>", "").replace("<think>", "")


# ---------------------------------------------------------------- OpenAI-compatible client


async def stream_chat(
    http: aiohttp.ClientSession,
    model: dict,
    messages: list[dict],
    on_piece=None,
    *,
    max_tokens: int | None = None,
    timeout_s: float = 900,
    tools: list | None = None,
    calls: list | None = None,
) -> tuple[str, str, dict | None]:
    """Stream one chat completion. Returns (text, thinking, usage or None).

    on_piece(kind, piece) is called for each streamed piece, kind being
    "text" or "thinking". Raises ChatError on connection or HTTP errors.
    With tools (OpenAI function schemas), the model may call them: each call
    {id, name, arguments (JSON text)} is appended to the calls list.
    """
    base = (model.get("base_url") or "").rstrip("/")
    if not base:
        raise ChatError(f"model {model.get('name')!r} has no base_url")
    url = base + "/chat/completions"
    body: dict = {
        "model": model.get("served_name") or model.get("name"),
        "messages": messages,
        "stream": True,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    acc: dict[int, dict] = {}  # streamed tool calls by index
    extra = model.get("extra_body")
    if isinstance(extra, dict):  # per-model request fields, e.g. chat_template_kwargs
        body.update({k: v for k, v in extra.items() if k not in ("model", "messages", "stream")})
    text_parts: list[str] = []
    think_parts: list[str] = []
    usage: dict | None = None
    splitter = ThinkSplitter()

    def piece(kind: str, s: str) -> None:
        if not s:
            return
        (text_parts if kind == "text" else think_parts).append(s)
        if on_piece is not None:
            on_piece(kind, s)

    def handle(obj: dict) -> None:
        nonlocal usage
        if isinstance(obj.get("error"), (dict, str)):
            err = obj["error"]
            raise ChatError(f"model error: {err.get('message') if isinstance(err, dict) else err}")
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
        for choice in (obj.get("choices") or [])[:1]:
            delta = choice.get("delta") or choice.get("message") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str):
                piece("thinking", reasoning)
            content = delta.get("content")
            if isinstance(content, str):
                for kind, s in splitter.feed(content):
                    piece(kind, s)
            for j, tc in enumerate(delta.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                a = acc.setdefault(tc.get("index", j) if isinstance(tc.get("index"), int) else j,
                                   {"id": "", "name": "", "arguments": ""})
                fn = tc.get("function") or {}
                if tc.get("id"):
                    a["id"] = tc["id"]
                if fn.get("name") and not a["name"]:
                    a["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    a["arguments"] += fn["arguments"]
                elif isinstance(fn.get("arguments"), dict):
                    a["arguments"] = json.dumps(fn["arguments"])

    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=15, sock_read=600)
    try:
        async with http.post(url, json=body, headers=auth_headers(model), timeout=timeout) as resp:
            if resp.status != 200:
                detail = short(await resp.text(), 400)
                raise ChatError(f"{url} answered HTTP {resp.status}: {detail}")
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype and "json" in ctype:
                handle(await resp.json(content_type=None))  # server ignored stream: true
            else:
                buf = b""
                done = False
                async for chunk in resp.content.iter_any():
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            done = True
                            break
                        try:
                            obj = json.loads(payload)
                        except ValueError:
                            continue
                        if isinstance(obj, dict):
                            handle(obj)
                    if done:
                        break
    except ChatError:
        raise
    except asyncio.TimeoutError:
        raise ChatError(f"{url} timed out") from None
    except (aiohttp.ClientError, OSError, ValueError) as e:
        raise ChatError(f"could not reach {url}: {e}") from None
    for kind, s in splitter.flush():
        piece(kind, s)
    if calls is not None:
        calls.extend(a for _, a in sorted(acc.items()) if a["name"])
    return "".join(text_parts), "".join(think_parts), usage


# ---------------------------------------------------------------- mock replies

_GREETING = re.compile(r"^\s*(hi|hii+|hello|hey|namaste|yo|hola|good (morning|afternoon|evening))\b", re.I)
_TASKY = re.compile(
    r"^\s*(please\s+|can you\s+|could you\s+)?(build|write|make|create|find|search|survey|summari[sz]e|"
    r"test|review|fix|implement|train|benchmark|plan|research|compare|analy[sz]e|draft|design)\b", re.I)


def mock_reply(agent: dict, model: dict | None, text: str, turn: int, rng: random.Random) -> tuple[str, str]:
    """(thinking, reply) for a mock model in Talk."""
    name = agent.get("name") or "Agent"
    role = agent.get("role") or "help out"
    model_name = (model or {}).get("name") or "a demo model"
    phrase, _, keys = topic_of(text)
    quote = short(text, 80)
    thinking = (f"The human wrote “{quote}”. I'm {name} and my job is: {role}. "
                "Keep it short, offer a concrete next step, and be honest that this is a scripted demo reply.")
    if _GREETING.match(text):
        body = rng.choice([
            f"Hi! I'm {name}. My job in this crew: {role}. Ask me anything, or give me a task and "
            "send it as a mission, then you can watch every step.",
            f"Hello! {name} here ({role}). Tell me what you are working on and I'll suggest how to split it up.",
        ])
    elif _TASKY.match(text):
        body = rng.choice([
            f"That sounds like a job for a mission: “{quote}”. Dispatch it and I'll get to work "
            "on it. You'll see the plan, every tool call and the final report, and I'll post the result "
            "back in this thread when I'm done.",
            f"I can take that on. My angle as {name} ({role}): start with the smallest version of "
            f"{phrase} that can be checked, then grow it. Send it as a mission to watch it run.",
        ])
    elif "?" in text:
        body = rng.choice([
            f"Good question about {phrase}. If I were answering for real, I'd first pin down what "
            f"“{keys[0]}” means here, then check it with a source or a quick experiment instead of guessing.",
            f"On {phrase}: the honest answer depends on details I would want to check first. "
            "A short mission could gather the facts and report back.",
        ])
    else:
        body = rng.choice([
            f"Noted: “{quote}”. As {name} I'd turn that into concrete next steps around {phrase}: "
            "one small check first, then the bigger piece.",
            f"Got it. Keeping “{short(text, 50)}” in mind. When you're ready, give me a task and I'll run it as a mission.",
        ])
    if turn > 1 and rng.random() < 0.4:
        body = "Still with you. " + body
    tail = (f"\n\n(I'm running on {model_name}, a demo model with no GPU behind it, so this reply is "
            f"scripted. Give {name} a real model for real answers.)")
    return thinking, body + tail


# ---------------------------------------------------------------- threads


def thread_public(thread: dict) -> dict:
    return {k: v for k, v in thread.items() if k != "messages"}


class TalkManager:
    def __init__(self, hub):
        self.hub = hub
        self.store = hub.store
        self.bus = hub.bus
        self.tasks: dict[str, asyncio.Task] = {}
        self.no_tools: set[str] = set()  # model ids whose server refused the tools field (then: plain chat)

    def recover(self) -> None:
        for t in self.store.threads.values():
            t["busy"] = False
            t.setdefault("messages", [])

    async def close(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.wait(list(self.tasks.values()), timeout=5)

    def get(self, thread_id: str) -> dict:
        thread = self.store.threads.get(thread_id)
        if thread is None:
            raise ApiError(404, "thread not found")
        return thread

    def _updated(self, thread: dict) -> None:
        self.bus.emit("thread.updated", {"thread": thread_public(thread)},
                      thread_id=thread["id"], agent_id=thread.get("agent_id"))
        self.store.mark_dirty()

    def append(self, thread: dict, role: str, content: str, *, thinking: str = "",
               mission_id: str | None = None, msg_id: str | None = None, steps: list | None = None) -> dict:
        msg = {"id": msg_id or new_id("msg"), "role": role, "content": content, "thinking": thinking,
               "ts": now(), "mission_id": mission_id}
        if steps:
            msg["steps"] = steps
        thread["messages"].append(msg)
        thread["updated_ts"] = msg["ts"]
        self.bus.emit("thread.message", {"message": msg}, thread_id=thread["id"],
                      agent_id=thread.get("agent_id"), mission_id=mission_id)
        self.store.mark_dirty()
        return msg

    def create(self, body: dict) -> dict:
        agent_id = body.get("agent_id")
        if not isinstance(agent_id, str) or agent_id not in self.store.agents:
            raise ApiError(400, "agent_id must name an existing agent")
        agent = self.store.agents[agent_id]
        ts = now()
        thread = {"id": new_id("t"), "agent_id": agent_id, "title": f"Talk with {agent['name']}",
                  "created_ts": ts, "updated_ts": ts, "busy": False, "messages": []}
        self.store.threads[thread["id"]] = thread
        self._updated(thread)
        return thread

    async def delete(self, thread_id: str) -> None:
        self.get(thread_id)
        await self.stop(thread_id)
        self.store.threads.pop(thread_id, None)
        self.bus.emit("thread.removed", {"id": thread_id}, thread_id=thread_id)
        self.store.mark_dirty()

    async def stop(self, thread_id: str) -> None:
        self.get(thread_id)
        task = self.tasks.get(thread_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)

    def add_card(self, thread_id: str, mission_id: str, content: str) -> None:
        """A mission card (dispatch notice or final report) in a thread."""
        thread = self.store.threads.get(thread_id)
        if thread is None:
            return
        if len(content) > CARD_MAX_CHARS:
            content = content[:CARD_MAX_CHARS].rstrip() + "\n\n... (full report in the mission)"
        self.append(thread, "system", content, mission_id=mission_id)
        self._updated(thread)

    async def post(self, thread_id: str, body: dict) -> dict:
        thread = self.get(thread_id)
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ApiError(400, "content must be a non-empty string")
        if thread.get("busy"):
            raise ApiError(409, "the agent is still replying; stop it or wait")
        first = not any(m["role"] == "user" for m in thread["messages"])
        user = self.append(thread, "user", content)
        if first and thread["title"].startswith("Talk with"):
            thread["title"] = short(content, 48)
        assistant_id = new_id("msg")
        thread["busy"] = True
        self._updated(thread)
        self.tasks[thread_id] = asyncio.create_task(self._reply(thread_id, assistant_id))
        return {"user": user, "assistant_id": assistant_id}

    def _system(self, thread: dict, agent: dict) -> str:
        """Blocking (builds the memory catalogue): call through asyncio.to_thread."""
        persona = (agent.get("persona") or "").strip()
        recall = getattr(self.hub, "recall", None)
        overview = recall.overview(exclude=f"chat:{thread['id']}") if recall else "(memory is not available)"
        taste = self.hub.taste.load() if getattr(self.hub, "taste", None) else {}
        tl = (f"THE OWNER'S RESEARCH TASTE: {short(taste['taste'], 700)}\n" if taste.get("taste") else "")
        return TALK_RULES.format(
            name=agent.get("name") or "an agent", role=f"Your role in the team: {agent['role']}." if agent.get("role") else "",
            persona=f"Your own angle: {persona}\n" if persona else "", overview=overview, taste=tl,
            today=time.strftime("%Y-%m-%d"))

    def _history(self, thread: dict, agent: dict, model: dict, system: str | None = None) -> list[dict]:
        if system is None:
            persona = (agent.get("persona") or "").strip()
            system = persona or f"You are {agent.get('name')}."
            if agent.get("role"):
                system += f"\n\nYour name is {agent.get('name')}. Your role: {agent['role']}."
        msgs: list[dict] = []
        budget = max(4000, int((model.get("context_window") or 8192) * 1.2))  # chars; leaves room for tool results
        used = 0
        for m in reversed(thread["messages"]):
            if m["role"] == "system":
                role, text = "user", f"[Sabha notice] {m['content']}"
            else:
                role, text = m["role"], m["content"]
            if not text:
                continue
            used += len(text)
            if used > budget and msgs:
                break
            msgs.append({"role": role, "content": text})
        msgs.reverse()
        merged: list[dict] = []
        for m in msgs:  # some chat templates reject two turns in a row from one role
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n\n" + m["content"]
            else:
                merged.append(dict(m))
        while merged and merged[0]["role"] == "assistant":
            merged.pop(0)
        return [{"role": "system", "content": system}] + merged

    async def _reply(self, thread_id: str, msg_id: str) -> None:
        thread = self.store.threads.get(thread_id)
        if thread is None:
            return
        agent = self.store.agents.get(thread.get("agent_id"))
        model = self.store.models.get(agent.get("model_id")) if agent else None
        text_parts: list[str] = []
        think_parts: list[str] = []
        error = ""
        stopped = False

        steps: list[dict] = []

        def piece(kind: str, s: str) -> None:
            (text_parts if kind == "text" else think_parts).append(s)
            self.bus.emit("chat.delta", {"message_id": msg_id, kind: s}, thread_id=thread_id,
                          agent_id=thread.get("agent_id"), ephemeral=True)

        def on_step(step: dict) -> None:
            self.bus.emit("chat.delta", {"message_id": msg_id, "step": step}, thread_id=thread_id,
                          agent_id=thread.get("agent_id"), ephemeral=True)

        try:
            if agent is None:
                error = "This thread's agent was deleted."
            elif model is None:
                error = f"{agent['name']} has no model assigned. Pick one in the agent's settings."
            elif model.get("kind") == "mock":
                await self._mock_stream(thread, agent, model, piece)
            else:
                system = await asyncio.to_thread(self._system, thread, agent)
                pre = await asyncio.to_thread(self._prefetch, thread)
                if pre:  # memory that matches the owner's message, handed over before the model even asks
                    system += pre[0]
                    steps.append(pre[1])
                    on_step(pre[1])
                msgs = self._history(thread, agent, model, system)
                await self._converse(thread, model, msgs, piece, on_step, steps, text_parts)
        except asyncio.CancelledError:
            stopped = True
        except ChatError as e:
            error = str(e)
        except Exception as e:  # keep the thread usable whatever happened
            log.exception("chat reply failed")
            error = f"unexpected error: {e}"
        finally:
            self.tasks.pop(thread_id, None)
        if thread_id not in self.store.threads:
            return
        content = "".join(text_parts).strip()
        if error:
            note = f"Could not get a reply: {error}"
            content = f"{content}\n\n{note}" if content else note
        elif stopped and not content:
            content = "(stopped)"
        self.bus.emit("chat.done", {"message_id": msg_id}, thread_id=thread_id,
                      agent_id=thread.get("agent_id"), ephemeral=True)
        self.append(thread, "assistant", content, thinking="".join(think_parts).strip(), msg_id=msg_id, steps=steps)
        thread["busy"] = False
        self._updated(thread)

    async def _converse(self, thread: dict, model: dict, msgs: list[dict], piece, on_step, steps: list, text_parts: list) -> None:
        """One Talk reply: stream, run the tool calls the model asks for, feed the results back, repeat (MAX_HOPS)."""
        use_tools = model.get("id") not in self.no_tools
        hop = nudged = 0
        while hop <= MAX_HOPS:
            calls: list[dict] = []
            try:
                text, _, _ = await stream_chat(self.hub.http, model, msgs, piece, calls=calls,
                                               tools=TOOLS if use_tools and hop < MAX_HOPS else None)
            except ChatError as e:
                if use_tools and hop == 0 and not text_parts and re.search(r"HTTP 400.*tool", str(e), re.I | re.S):
                    log.info("model %s refused tools; plain chat from now on: %s", model.get("name"), short(str(e), 160))
                    self.no_tools.add(model.get("id"))
                    use_tools = False
                    continue
                raise
            if not calls:
                tail = (text or "").strip()[-300:]
                if use_tools and not nudged and hop < MAX_HOPS and tail and not tail.endswith("?") and PROMISE.search(tail):
                    nudged = 1
                    msgs += [{"role": "assistant", "content": text}, {"role": "user", "content": NUDGE}]
                    piece("text", "\n\n")
                    hop += 1
                    continue
                return
            ids = [c["id"] or f"call_{hop}_{i}" for i, c in enumerate(calls)]
            msgs.append({"role": "assistant", "content": text or "", "tool_calls": [
                {"id": ids[i], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for i, c in enumerate(calls)]})
            for i, c in enumerate(calls):
                try:
                    args = json.loads(c["arguments"] or "{}")
                except ValueError:
                    args = {}
                result, step = await asyncio.to_thread(self._tool, c["name"], args if isinstance(args, dict) else {}, thread["id"])
                steps.append(step)
                on_step(step)
                msgs.append({"role": "tool", "tool_call_id": ids[i], "content": result})
            if "".join(text_parts).strip() and not "".join(text_parts).endswith("\n"):
                piece("text", "\n\n")
            hop += 1

    def _prefetch(self, thread: dict) -> tuple[str, dict] | None:
        """Search memory for the owner's last message (blocking). Returns (prompt block, step) or None."""
        recall = getattr(self.hub, "recall", None)
        last = next((m["content"] for m in reversed(thread["messages"]) if m["role"] == "user"), "")
        if recall is None or len(last.split()) < 3:  # small talk needs no memory
            return None
        hits = [h for h in recall.search(last, "", 6, exclude=f"chat:{thread['id']}") if h["score"] >= 1.5]
        if not hits:
            return None
        block = ("\n\nMEMORY THAT MATCHES THE OWNER'S LAST MESSAGE (found automatically; read_memory an id for the full text, "
                 "search_memory for more):\n" + "\n".join(f"[{h['id']}] {h['title']}: {h['snippet']}" for h in hits))
        return block, {"tool": "search_memory", "label": "looked in memory for your message", "result": f"{len(hits)} found"}

    def _tool(self, name: str, args: dict, thread_id: str) -> tuple[str, dict]:
        """Run one read-only Talk tool. Returns (text for the model, step for the app). Blocking."""
        recall = self.hub.recall
        if name == "search_memory":
            q = str(args.get("query") or "").strip()
            hits = recall.search(q, str(args.get("kind") or ""), 8, exclude=f"chat:{thread_id}") if q else []
            step = {"tool": name, "label": f"searched memory for “{short(q, 60)}”", "result": f"{len(hits)} found" if hits else "nothing found"}
            if not hits:
                return "No matches in memory. Try other words, or another kind.", step
            return "\n\n".join(f"[{h['id']}] {h['title']}\n{h['snippet']}" for h in hits), step
        if name == "read_memory":
            r = recall.read(str(args.get("id") or "").strip().strip("[]"), args.get("part") or 1)
            if r is None:
                return f"No memory document has the id {args.get('id')!r}. Use an id from search_memory.", \
                    {"tool": name, "label": f"read {short(str(args.get('id')), 60)}", "result": "not found"}
            return (f"[{r['id']}] {r['title']} (part {r['part']} of {r['parts']})\n\n{r['text']}",
                    {"tool": name, "label": f"read {short(r['title'], 70)}", "result": f"part {r['part']} of {r['parts']}", "id": r["id"]})
        if name == "web_search":
            q = str(args.get("query") or "").strip()
            step = {"tool": name, "label": f"searched the web for “{short(q, 60)}”"}
            try:
                hits, engine = webtools.search(q, 6)
            except Exception as e:  # noqa: BLE001 (any engine failure is a message for the model, not a crash)
                return f"Web search failed: {short(str(e), 300)}", {**step, "result": "failed"}
            return ("\n\n".join(f"{h.get('title')}\n{h.get('url')}\n" + (f"Published: {h['date']}\n" if h.get("date") else "")
                                + (h.get("snippet") or "") for h in hits),
                    {**step, "result": f"{len(hits)} results ({engine})"})
        if name == "read_page":
            url = str(args.get("url") or "").strip()
            if webtools.ARXIV_ID.fullmatch(url):
                url = f"https://arxiv.org/abs/{url}"
            find = str(args.get("find") or "").strip()
            try:
                title, text = webtools.page_text(url)
            except Exception as e:  # noqa: BLE001
                return f"Could not read {url}: {short(str(e), 300)}", {"tool": name, "label": f"read {short(url, 70)}", "result": "failed"}
            i = text.lower().find(find.lower()) if find else -1
            start = max(0, i - 1500) if i >= 0 else 0
            pub = re.match(r"Published: ([^\n]+)", text)
            head = f"{title}\n{url}\n" + (f"Published: {pub.group(1)}\n" if pub and start > 0 else "")
            return (f"{head}\n{text[start:start + 6000]}",
                    {"tool": name, "label": f"read {short(title or url, 70)}",
                     "result": (f"published {pub.group(1).split(' (')[0]} · " if pub else "") + f"{len(text) // 1000}k characters", "url": url})
        return f"There is no tool named {name}.", {"tool": name, "label": f"unknown tool {short(name, 40)}", "result": "failed"}

    async def _mock_stream(self, thread: dict, agent: dict, model: dict, piece) -> None:
        user_msgs = [m for m in thread["messages"] if m["role"] == "user"]
        last = user_msgs[-1]["content"] if user_msgs else ""
        rng = random.Random()
        thinking, reply = mock_reply(agent, model, last, len(user_msgs), rng)
        speed = mock_speed()
        words = re.findall(r"\S+\s*", thinking)
        for i in range(0, len(words), 3):
            piece("thinking", "".join(words[i:i + 3]))
            await asyncio.sleep(rng.uniform(0.04, 0.08) / speed)
        await asyncio.sleep(0.25 / speed)
        for word in re.findall(r"\s*\S+", reply):
            piece("text", word)
            await asyncio.sleep(rng.uniform(0.04, 0.08) / speed)
