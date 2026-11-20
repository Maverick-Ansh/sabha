"""Council: several agents read one research paper in parts, then discuss it.

    fetching   the hub gets the paper (webtools.paper) and cuts it into sections
    reading    every seat reads its own part and writes a brief, all at once
    discussing the seats speak in turn for N rounds; each sees the briefs and the
               talk so far, and keeps its own part in front of it
    verdict    the chair writes what the council concluded
    done | failed | stopped

Every turn is one plain chat completion (talk.stream_chat), streamed to the UI
as ephemeral council.delta events and stored as a council.message. A speaker may
end a turn with "LOOKUP: <query>"; the hub then runs a web search and puts the
results on the table for everyone. The human can speak at any time; after the
verdict that starts one more round.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import shutil
from pathlib import Path

from . import webtools
from . import exchanges
from .talk import ChatError, stream_chat
from .util import ApiError, mock_speed, new_id, now, short

log = logging.getLogger("sabha.council")

ACTIVE = ("fetching", "reading", "discussing", "verdict")
MAX_SEATS = 6
MAX_LOOKUPS = 6
LOOKUP_RE = re.compile(r"^\W*LOOKUP\W*:\s*(.+?)\s*$", re.I | re.M)

RULES = (
    "You are {name}, a member of a council of AI agents that is reading a research paper together. "
    "Each member read a different part of the paper. Be specific and honest: give numbers, name "
    "equations, tables and figures, and say plainly when your part does not cover something. "
    "Never invent results. Write plain markdown and keep to the length you are asked for."
)
BRIEF_TASK = (
    "Write your brief for the council in under 220 words, in exactly this shape:\n"
    "**Claims** 2 to 4 bullets: what this part says, with numbers where it gives them.\n"
    "**Evidence** how the part backs that up (experiments, tables, figures, proofs).\n"
    "**Doubts** 1 to 3 things that look weak, missing or unclear.\n"
    "**Question for the council** one question that another member's part might answer."
)
SAY_TASK = (
    "This is round {round} of {rounds}. It is your turn to speak, in under 130 words. "
    "If the human said something, answer that first. Answer any question your part can answer and say "
    "where in your part the answer is. If you disagree with a member, name them and say exactly why. "
    "Do not repeat what was already said.{last}{lookup}"
)
LOOKUP_HINT = (
    " Only if one fact from outside the paper would settle a point, end with a single line "
    "'LOOKUP: <3 to 7 search keywords>' (keywords, not a question) and the hub will search the web "
    "for the council. One lookup per round, so most turns should not ask for one."
)
VERDICT_TASK = (
    "You chair this council. Write its verdict in markdown, under 380 words, with these headings:\n"
    "## What the paper claims\n## What holds up\n## What is weak or unverified\n"
    "## Where the council disagreed\n## What to do next\n"
    "Under the last heading give 2 or 3 concrete checks or experiments. Name members where they "
    "disagreed. Use only what is in the briefs and the discussion. Do not invent numbers."
)


def public(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "messages"}


def assign(sizes: list[int], n: int) -> list[list[int]]:
    """Give consecutive sections to n seats so the seats carry about the same number of characters."""
    total = sum(sizes) or 1
    seats: list[list[int]] = [[] for _ in range(n)]
    done = 0
    for i, size in enumerate(sizes):
        seats[min(n - 1, int((done + size / 2) / total * n))].append(i)
        done += size
    return seats


def _clip(text: str, limit: int) -> str:
    """Keep the head and the tail of a long text."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.65)
    return f"{text[:head].rstrip()}\n\n[... {len(text) - limit} characters cut ...]\n\n{text[-(limit - head):].lstrip()}"


class CouncilManager:
    def __init__(self, hub):
        self.hub = hub
        self.store = hub.store
        self.bus = hub.bus
        self.tasks: dict[str, asyncio.Task] = {}
        self.root = Path(hub.cfg.data_dir) / "councils"

    # ------------------------------------------------------------ plumbing

    def recover(self) -> None:
        for c in self.store.councils.values():
            c.setdefault("messages", [])
            if c.get("status") in ACTIVE:
                c["status"] = "stopped"
                c["error"] = "the hub restarted while this council was in session"
                c["ended_ts"] = c.get("ended_ts") or now()
            for seat in c.get("seats", []):
                if seat.get("state") in ("reading", "speaking"):
                    seat["state"] = "idle"

    async def close(self) -> None:
        for task in list(self.tasks.values()):
            task.cancel()
        if self.tasks:
            await asyncio.wait(list(self.tasks.values()), timeout=5)

    def get(self, council_id: str) -> dict:
        c = self.store.councils.get(council_id)
        if c is None:
            raise ApiError(404, "council not found")
        return c

    def _changed(self, c: dict) -> None:
        self.bus.emit("council.updated", {"council": public(c)})
        self.store.mark_dirty()

    def _set(self, c: dict, status: str, note: str = "") -> None:
        c["status"] = status
        c["note"] = note
        self._changed(c)

    def _seat_state(self, c: dict, agent_id: str, state: str) -> None:
        for seat in c["seats"]:
            if seat["agent_id"] == agent_id:
                seat["state"] = state
        self._changed(c)

    def _append(self, c: dict, *, role: str, kind: str, content: str, agent_id: str | None = None,
                thinking: str = "", msg_id: str | None = None, meta: dict | None = None) -> dict:
        msg = {"id": msg_id or new_id("cm"), "role": role, "kind": kind, "agent_id": agent_id,
               "round": c.get("round", 0), "content": content, "thinking": thinking, "ts": now(),
               "meta": meta or {}}
        c["messages"].append(msg)
        self.bus.emit("council.message", {"council_id": c["id"], "message": msg}, agent_id=agent_id)
        self.store.mark_dirty()
        return msg

    def _paper_path(self, council_id: str) -> Path:
        return self.root / council_id / "paper.json"

    def _sections(self, council_id: str) -> list[dict]:
        try:
            return json.loads(self._paper_path(council_id).read_text("utf-8"))["sections"]
        except (OSError, ValueError, KeyError):
            return []

    def section(self, council_id: str, section_id: str) -> dict:
        c = self.get(council_id)
        metas = (c.get("paper") or {}).get("sections") or []
        texts = self._sections(council_id)
        for i, meta in enumerate(metas):
            if meta["id"] == section_id and i < len(texts):
                return {"id": section_id, "title": meta["title"], "text": texts[i]["text"]}
        raise ApiError(404, "section not found")

    # ------------------------------------------------------------ REST actions

    def create(self, body: dict) -> dict:
        source = body.get("source") if isinstance(body.get("source"), str) else ""
        text = body.get("text") if isinstance(body.get("text"), str) else ""
        if not source.strip() and not text.strip():
            raise ApiError(400, "give a source (arXiv id or link) or the paper text")
        if len(text) > 600_000:
            raise ApiError(400, "paper text is too long (limit 600,000 characters)")
        ids = body.get("agent_ids")
        if not isinstance(ids, list) or not 2 <= len(ids) <= MAX_SEATS or len(set(ids)) != len(ids):
            raise ApiError(400, f"agent_ids must list 2 to {MAX_SEATS} different agents")
        for aid in ids:
            agent = self.store.agents.get(aid) if isinstance(aid, str) else None
            if agent is None:
                raise ApiError(400, "agent_ids must name existing agents")
            if agent.get("model_id") not in self.store.models:
                raise ApiError(400, f"{agent['name']} has no model; pick one in Crew first")
        chair = body.get("chair_id") if body.get("chair_id") in ids else ids[0]
        try:
            rounds = min(4, max(1, int(body.get("rounds", 2))))
        except (TypeError, ValueError):
            raise ApiError(400, "rounds must be a number from 1 to 4") from None
        question = body.get("question") if isinstance(body.get("question"), str) else ""
        c = {
            "id": new_id("c"), "title": short(source.strip() or "Pasted paper", 80), "source": source.strip(),
            "question": question.strip()[:2000], "status": "fetching", "note": "", "error": "",
            "rounds": rounds, "round": 0, "lookups": bool(body.get("lookups", True)), "lookups_used": 0,
            "chair_id": chair, "paper": None,
            "seats": [{"agent_id": aid, "section_ids": [], "part": "", "chars": 0, "cut": False, "state": "idle"}
                      for aid in ids],
            "messages": [], "verdict": "", "created_ts": now(), "ended_ts": None,
        }
        self.store.councils[c["id"]] = c
        self._changed(c)
        self._start(c, rounds, text)
        return c

    def _start(self, c: dict, rounds: int, text: str = "") -> None:
        self.tasks[c["id"]] = asyncio.create_task(self._run(c["id"], rounds, text))

    async def say(self, council_id: str, body: dict) -> dict:
        c = self.get(council_id)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "text must be a non-empty string")
        self._append(c, role="human", kind="human", content=text.strip()[:4000])
        if c["status"] not in ACTIVE and c.get("paper") and body.get("reply", True):
            c["rounds"] = c["round"] + 1
            c["error"] = ""
            c["ended_ts"] = None
            self._set(c, "discussing")
            self._start(c, 1)
        return c

    async def stop(self, council_id: str) -> dict:
        c = self.get(council_id)
        task = self.tasks.get(council_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        return c

    async def delete(self, council_id: str) -> None:
        await self.stop(council_id)
        self.store.councils.pop(council_id, None)
        shutil.rmtree(self.root / council_id, ignore_errors=True)
        self.bus.emit("council.removed", {"id": council_id})
        self.store.mark_dirty()

    # ------------------------------------------------------------ the session

    async def _run(self, council_id: str, rounds: int, text: str = "") -> None:
        c = self.store.councils[council_id]
        try:
            if not c.get("paper"):
                await self._fetch(c, text)
                await self._wait_models(c)
                self._set(c, "reading")
                await self._read(c)
            else:
                await self._wait_models(c)
            self._set(c, "discussing")
            for _ in range(rounds):
                c["round"] += 1
                self._changed(c)
                await self._round(c)
            self._set(c, "verdict")
            await self._verdict(c)
            self._set(c, "done")
        except asyncio.CancelledError:
            self._append(c, role="system", kind="note", content="The human stopped the council.")
            self._set(c, "stopped")
        except (ChatError, webtools.WebError, ApiError) as e:
            c["error"] = str(e)
            self._set(c, "failed")
        except Exception as e:  # keep the hub alive whatever a session does
            log.exception("council %s failed", council_id)
            c["error"] = f"unexpected error: {e}"
            self._set(c, "failed")
        finally:
            self.tasks.pop(council_id, None)
            for seat in c["seats"]:
                seat["state"] = "idle"
            c["ended_ts"] = now()
            self._changed(c)

    def _model_of(self, agent_id: str) -> tuple[dict, dict]:
        agent = self.store.agents.get(agent_id)
        if agent is None:
            raise ApiError(400, "a council member was deleted")
        model = self.store.models.get(agent.get("model_id"))
        if model is None:
            raise ApiError(400, f"{agent['name']} has no model")
        return agent, model

    async def _wait_models(self, c: dict) -> None:
        """Hold the session while a member's model is still loading (at most 30 minutes)."""
        deadline = now() + 1800
        while now() < deadline:
            loading = [m["name"] for m in (self._model_of(s["agent_id"])[1] for s in c["seats"])
                       if m.get("status") == "starting"]
            if not loading:
                return
            note = "waiting for " + ", ".join(sorted(set(loading))) + " to finish loading"
            if c.get("note") != note:
                self._set(c, c["status"], note)
            await asyncio.sleep(3)
        raise ChatError("a model was still loading after 30 minutes")

    async def _fetch(self, c: dict, text: str) -> None:
        self._set(c, "fetching", "getting the paper")
        paper = await asyncio.to_thread(webtools.paper, c["source"], text)
        sections = paper["sections"]
        folder = self.root / c["id"]
        folder.mkdir(parents=True, exist_ok=True)
        self._paper_path(c["id"]).write_text(json.dumps(paper, ensure_ascii=False), encoding="utf-8")
        plan = assign([len(s["text"]) for s in sections], len(c["seats"]))
        metas = [{"id": f"s{i + 1}", "title": s["title"], "chars": len(s["text"]), "seat": None}
                 for i, s in enumerate(sections)]
        for n, (seat, idxs) in enumerate(zip(c["seats"], plan)):
            for i in idxs:
                metas[i]["seat"] = n
            seat["section_ids"] = [metas[i]["id"] for i in idxs]
            seat["chars"] = sum(metas[i]["chars"] for i in idxs)
            titles = [metas[i]["title"] for i in idxs]
            seat["part"] = (titles[0] if len(titles) == 1 else f"{titles[0]} to {titles[-1]}") if titles else "member at large"
        c["paper"] = {"title": paper["title"], "authors": paper["authors"][:12], "url": paper["url"],
                      "abstract": paper["abstract"][:2400], "chars": sum(m["chars"] for m in metas),
                      "sections": metas}
        c["title"] = short(paper["title"], 120)
        self._changed(c)

    def _header(self, c: dict, seat: dict, model: dict) -> str:
        """Paper facts plus this seat's own part. Identical on every turn, so the server can cache it."""
        p = c["paper"]
        cap = max(4000, min(30000, int((model.get("context_window") or 8192) * 1.1)))
        texts = self._sections(c["id"])
        by_id = {m["id"]: texts[i] for i, m in enumerate(p["sections"]) if i < len(texts)}
        mine = [by_id[sid] for sid in seat["section_ids"] if sid in by_id]
        head = f"PAPER: {p['title']}\nAUTHORS: {', '.join(p['authors'][:6]) or 'not given'}\n"
        head += f"ABSTRACT: {p['abstract'][:1500]}\n\n"
        if c.get("question"):
            head += f"THE HUMAN WHO CALLED THE COUNCIL WANTS TO KNOW: {c['question']}\n\n"
        if not mine:
            return head + ("YOU HAVE NO PART. You are the member at large: you saw only the abstract. "
                           "Your job is to question the other members and test their claims.\n\n")
        total = sum(len(s["text"]) for s in mine)
        cut = total > cap
        if cut != seat.get("cut"):
            seat["cut"] = cut
        body = "\n\n".join(f"### {s['title']}\n{_clip(s['text'], max(800, int(cap * len(s['text']) / total))) if cut else s['text']}"
                           for s in mine)
        return head + f"YOUR PART OF THE PAPER ({seat['part']}):\n\n{body}\n\n=== END OF YOUR PART ===\n\n"

    def _name(self, agent_id: str | None) -> str:
        agent = self.store.agents.get(agent_id) if agent_id else None
        return agent["name"] if agent else "a former member"

    def _table(self, c: dict, me: str, budget: int, briefs_full: bool = False) -> str:
        """The briefs and the discussion so far, newest kept when the budget runs out."""
        briefs, talk = [], []
        for m in c["messages"]:
            who = self._name(m["agent_id"])
            if m["kind"] == "brief":
                if m["agent_id"] != me or briefs_full:
                    seat = next((s for s in c["seats"] if s["agent_id"] == m["agent_id"]), None)
                    briefs.append(f"[{who}'s brief on \"{seat['part'] if seat else 'a part'}\"]\n"
                                  f"{_clip(m['content'], 2400 if briefs_full else 1300)}")
            elif m["kind"] == "say":
                talk.append(f"[{'YOU' if m['agent_id'] == me else who}, round {m['round']}]\n{_clip(m['content'], 900)}")
            elif m["kind"] == "lookup":
                talk.append(f"[web results for \"{m['meta'].get('query', '')}\", asked by {who}; not verified]\n"
                            f"{_clip(m['content'], 900)}")
            elif m["kind"] == "human":
                talk.append(f"[THE HUMAN]\n{_clip(m['content'], 1200)}")
        used = sum(len(b) for b in briefs)
        kept: list[str] = []
        for entry in reversed(talk):
            if used + len(entry) > budget and kept:
                kept.append("[earlier discussion left out]")
                break
            kept.append(entry)
            used += len(entry)
        out = "BRIEFS FROM THE MEMBERS:\n\n" + "\n\n".join(briefs)
        if kept:
            out += "\n\nDISCUSSION SO FAR:\n\n" + "\n\n".join(reversed(kept))
        return out

    async def _speak(self, c: dict, seat: dict, kind: str, prompt: str, max_tokens: int) -> dict | None:
        """One streamed turn. Returns the stored message, or None when the model call failed."""
        agent, model = self._model_of(seat["agent_id"])
        persona = (agent.get("persona") or "").strip()
        system = RULES.format(name=agent["name"]) + (f"\n\nAbout you: {persona}" if persona else "")
        msg_id = new_id("cm")
        base = {"council_id": c["id"], "message_id": msg_id, "mkind": kind, "round": c["round"]}
        text_parts: list[str] = []
        think_parts: list[str] = []

        def piece(k: str, s: str) -> None:
            (text_parts if k == "text" else think_parts).append(s)
            self.bus.emit("council.delta", {**base, k: s}, agent_id=agent["id"], ephemeral=True)

        seat["state"] = "reading" if kind == "brief" else "speaking"
        self._changed(c)
        self.bus.emit("council.delta", dict(base), agent_id=agent["id"], ephemeral=True)
        error = ""
        try:
            if model.get("kind") == "mock":
                await self._mock(c, seat, agent, kind, piece)
            else:
                msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
                with exchanges.recording(exchanges.path_for(self.hub.cfg.data_dir, "council", c["id"]), track=agent["name"],
                                         asked=agent["name"], step=kind, round=c["round"], title=(c.get("title") or "")[:120]):
                    await stream_chat(self.hub.http, model, msgs, piece, max_tokens=max_tokens, timeout_s=900)
        except asyncio.CancelledError:
            if "".join(text_parts).strip():
                self._append(c, role="agent", kind=kind, agent_id=agent["id"], msg_id=msg_id,
                             content="".join(text_parts).strip() + "\n\n(stopped)")
            raise
        except ChatError as e:
            error = str(e)
        finally:
            seat["state"] = "idle"
        content = "".join(text_parts).strip()
        if not content and not error and think_parts:
            error = "the model used its whole turn on thinking and said nothing"
        if error and not content:
            self._append(c, role="system", kind="note", agent_id=agent["id"],
                         content=f"{agent['name']} could not take this turn: {error}")
            self._changed(c)
            return None
        meta = {}
        if kind == "say":
            found = LOOKUP_RE.findall(content)
            if found:
                meta["lookup"] = found[-1].strip(" '\"<>")[:160]
                content = LOOKUP_RE.sub("", content).strip() or f"(asked for a lookup: {meta['lookup']})"
        msg = self._append(c, role="agent", kind=kind, agent_id=agent["id"], msg_id=msg_id, content=content,
                           thinking="".join(think_parts).strip(), meta=meta)
        self._changed(c)
        return msg

    async def _read(self, c: dict) -> None:
        async def one(seat: dict):
            _, model = self._model_of(seat["agent_id"])
            return await self._speak(c, seat, "brief", self._header(c, seat, model) + BRIEF_TASK, 700)

        results = await asyncio.gather(*(one(s) for s in c["seats"]))
        if sum(r is not None for r in results) < 2:
            raise ChatError("fewer than two members could write a brief; see the notes above")

    async def _round(self, c: dict) -> None:
        seats = c["seats"]
        start = (c["round"] - 1) % len(seats)
        looked = False  # one lookup per round: the first member to ask gets it
        for seat in seats[start:] + seats[:start]:
            _, model = self._model_of(seat["agent_id"])
            can_look = c["lookups"] and not looked and c["lookups_used"] < MAX_LOOKUPS
            task = SAY_TASK.format(
                round=c["round"], rounds=max(c["rounds"], c["round"]),
                last=" This is the last round: say what you now believe about the paper." if c["round"] >= c["rounds"] else "",
                lookup=LOOKUP_HINT if can_look else "")
            budget = max(3000, int((model.get("context_window") or 8192) * 0.8))
            prompt = self._header(c, seat, model) + self._table(c, seat["agent_id"], budget) + "\n\n" + task
            msg = await self._speak(c, seat, "say", prompt, 450)
            if msg and msg["meta"].get("lookup") and can_look:
                looked = True
                await self._lookup(c, seat["agent_id"], msg["meta"]["lookup"])

    async def _lookup(self, c: dict, agent_id: str, query: str) -> None:
        c["lookups_used"] += 1
        self._set(c, c["status"], f"searching the web: {query}")
        try:
            hits, engine, query = await asyncio.wait_for(asyncio.to_thread(webtools.lookup, query, 7), 60)
            # the paper on the table is not news to the council: drop it from its own lookups
            own = webtools.ARXIV_ID.search(c["paper"].get("url") or "")
            title = re.sub(r"\W+", " ", c["paper"]["title"]).lower().strip()
            hits = [h for h in hits if not (own and own.group(1) in h["url"])
                    and title not in re.sub(r"\W+", " ", h["title"]).lower()][:4]
            body = ("\n".join(f"- [{h['title']}]({h['url']})" + (f" ({h['date']})" if h.get("date") else "") + f": {h['snippet'][:260]}"
                              for h in hits)
                    or "Nothing relevant came back for these keywords.")
        except (webtools.WebError, asyncio.TimeoutError) as e:
            engine, hits, body = "", [], f"The search did not work: {e or 'timed out'}"
        self._append(c, role="system", kind="lookup", agent_id=agent_id, content=body,
                     meta={"query": query, "engine": engine, "results": len(hits)})
        self._set(c, c["status"], "")

    async def _verdict(self, c: dict) -> None:
        seat = next((s for s in c["seats"] if s["agent_id"] == c["chair_id"]), c["seats"][0])
        _, model = self._model_of(seat["agent_id"])
        p = c["paper"]
        budget = max(4000, int((model.get("context_window") or 8192) * 1.6))
        prompt = (f"PAPER: {p['title']}\nABSTRACT: {p['abstract'][:1500]}\n\n"
                  + (f"THE HUMAN WANTS TO KNOW: {c['question']}\n\n" if c.get("question") else "")
                  + self._table(c, seat["agent_id"], budget, briefs_full=True) + "\n\n" + VERDICT_TASK)
        msg = await self._speak(c, seat, "verdict", prompt, 1200)
        if msg is None:
            raise ChatError("the chair could not write the verdict")
        c["verdict"] = msg["content"]

    # ------------------------------------------------------------ mock members (demo, no GPU)

    async def _mock(self, c: dict, seat: dict, agent: dict, kind: str, piece) -> None:
        rng = random.Random(f"{c['id']}{agent['id']}{kind}{c['round']}")
        texts = self._sections(c["id"])
        by_id = {m["id"]: texts[i]["text"] for i, m in enumerate(c["paper"]["sections"]) if i < len(texts)}
        mine = " ".join(by_id.get(sid, "") for sid in seat["section_ids"])
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", mine) if 60 < len(s.strip()) < 260 and "|" not in s and "#" not in s and "\n" not in s.strip()]
        nums = [s for s in sents if re.search(r"\d", s)] or sents
        pick = lambda pool, k: rng.sample(pool, min(k, len(pool))) if pool else ["My part gives no detail here."]  # noqa: E731
        others = [self._name(s["agent_id"]) for s in c["seats"] if s["agent_id"] != agent["id"]]
        if kind == "brief":
            out = ("**Claims**\n" + "\n".join(f"- {s}" for s in pick(sents, 3))
                   + "\n\n**Evidence**\n" + "\n".join(f"- {s}" for s in pick(nums, 2))
                   + "\n\n**Doubts**\n- The part does not say how much this depends on the exact setup.\n"
                   + f"\n**Question for the council**\nDoes another part back up what \"{seat['part']}\" assumes?")
        elif kind == "say":
            other = rng.choice(others) if others else "the council"
            out = (f"{other}, on your question: my part ({seat['part']}) says this. {pick(nums, 1)[0]} "
                   f"I do not see a control for it in my part, so I would hold the claim as likely, not shown.")
            if c["lookups"] and c["round"] == 1 and seat is c["seats"][0]:
                out += f"\n\nLOOKUP: {c['paper']['title'][:70]} reproduction"
        else:
            briefs = [m for m in c["messages"] if m["kind"] == "brief"]
            first = lambda m: next((ln[2:] for ln in m["content"].split("\n") if ln.startswith("- ")), "")  # noqa: E731
            out = ("## What the paper claims\n" + "\n".join(f"- {first(m)}" for m in briefs[:3])
                   + "\n\n## What holds up\n- The parts agree with each other on the main result.\n"
                   + "\n## What is weak or unverified\n- No member found a control for the setup dependence.\n"
                   + f"\n## Where the council disagreed\n- {' and '.join(others[:2]) or 'Members'} read the evidence as likely, not shown.\n"
                   + "\n## What to do next\n1. Rerun the main table with two more seeds.\n2. Remove the key part and measure the drop.")
        speed = mock_speed()
        for word in re.findall(r"\s*\S+", out):
            piece("text", word)
            await asyncio.sleep(rng.uniform(0.015, 0.04) / speed)
