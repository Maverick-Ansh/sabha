"""Missions: lifecycle, plan approval, notes and resume, stats, todo -> plan.

Status machine (PROTOCOL.md section 2):

    queued -> planning -> awaiting_approval -(approve)-> running
    queued ---------------------------------------------> running
    running -> done | failed | stopped
    done | failed | stopped -(resume / undelivered note)-> running

Each mission that is running has one asyncio task (`_drive`) that invokes the
harness, possibly several times in a row: when a run ends and notes are
waiting, the next run starts on the same session with the notes as its task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .harness import RunContext, get_harness
from .harness.mock import mock_plan
from .talk import ChatError, stream_chat, strip_think
from .util import ApiError, mock_speed, new_id, now, short

log = logging.getLogger("sabha.missions")

ACTIVE = ("queued", "planning", "running")
FINISHED = ("done", "failed", "stopped")
STATS_EVERY_S = 1.0

PLAN_PROMPT = (
    "Task:\n{task}\n\n"
    "Write a short plan for this task: 3 to 7 steps, each one short and checkable. "
    'Reply with ONLY a JSON array of strings, for example ["read the code", "write the fix", "run the tests"].'
)

_TODO_STATUS = {
    "pending": "todo", "todo": "todo", "not_started": "todo", "open": "todo",
    "in_progress": "doing", "doing": "doing", "active": "doing", "running": "doing", "started": "doing",
    "completed": "done", "complete": "done", "done": "done", "finished": "done",
    "cancelled": "skipped", "canceled": "skipped", "skipped": "skipped",
}


def default_title(prompt: str) -> str:
    text = " ".join(prompt.split())
    if len(text) <= 60:
        return text
    cut = text[:60].rsplit(" ", 1)[0] or text[:60]
    return cut.rstrip(",.;:") + "…"


def usage_tokens(usage) -> tuple[int, int]:
    """(prompt, completion) tokens from a usage dict, whatever its key style."""
    if not isinstance(usage, dict):
        return 0, 0

    def first(*keys) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
        return 0

    return (first("input", "prompt_tokens", "inputTokens", "input_tokens", "promptTokens"),
            first("output", "completion_tokens", "outputTokens", "output_tokens", "completionTokens"))


def todo_plan(tool, payload) -> list[dict] | None:
    """Map a todo tool call's input to plan steps, or None if it is not one."""
    if "todo" not in str(tool or "").lower():
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict):
        return None
    items = None
    for value in payload.values():
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            items = value
            break
    if items is None:
        return None
    plan = []
    for item in items:
        text = next((item[k] for k in ("content", "text", "title", "task", "description")
                     if isinstance(item.get(k), str) and item[k].strip()), "")
        if not text:
            continue
        status = _TODO_STATUS.get(str(item.get("status", "")).strip().lower(), "todo")
        plan.append({"id": f"p{len(plan) + 1}", "text": text.strip(), "status": status})
    return plan or None


def parse_plan(text: str) -> list[str]:
    """Plan steps from a model reply: a JSON array if there is one, else lines."""
    text = strip_think(text or "").strip()
    i, j = text.find("["), text.rfind("]")
    if 0 <= i < j:
        try:
            arr = json.loads(text[i:j + 1])
            steps = []
            for x in arr if isinstance(arr, list) else []:
                s = (x.get("text") or x.get("step") or "") if isinstance(x, dict) else x
                s = str(s).strip()
                if s:
                    steps.append(s)
            if steps:
                return steps[:10]
        except ValueError:
            pass
    steps = []
    for line in text.splitlines():
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)]|step\s*\d+[:.)]?)\s*", "", line, flags=re.I).strip()
        s = s.strip('",').strip()
        if s and not s.startswith("```") and s not in ("[", "]"):
            steps.append(s)
    return steps[:10]


def plan_steps(texts: list[str]) -> list[dict]:
    return [{"id": f"p{i}", "text": t, "status": "todo"} for i, t in enumerate(texts, 1)]


@dataclass
class _Handle:
    task: asyncio.Task | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    user_stop: bool = False
    interrupt: bool = False


def _reason_kind(reason) -> str:
    """The `kind` of a dsh reason, which may be an object, a bare string or missing."""
    return str(reason.get("kind", "")) if isinstance(reason, dict) else str(reason or "")


@dataclass
class _RunState:
    final_kind: str = ""
    turn_end_kind: str = ""  # reason of the last `turn_end` status; real dsh reports it there
    last_error: str = ""
    step_usage_in_turn: bool = False


class MissionManager:
    def __init__(self, hub):
        self.hub = hub
        self.store = hub.store
        self.bus = hub.bus
        self.active: dict[str, _Handle] = {}
        self.plan_tasks: dict[str, asyncio.Task] = {}
        self.closing = False
        self._last_emit: dict[str, float] = {}
        self._stats_timers: dict[str, asyncio.TimerHandle] = {}

    # ------------------------------------------------------------ helpers

    def get(self, mission_id: str) -> dict:
        mission = self.store.missions.get(mission_id)
        if mission is None:
            raise ApiError(404, "mission not found")
        return mission

    def workspace(self, mission: dict) -> Path:
        return self.hub.cfg.data_dir / mission["workspace"]

    def active_count(self, harness: str | None = None) -> int:
        n = 0
        for mid in self.active:
            m = self.store.missions.get(mid)
            agent = self.store.agents.get(m["agent_id"]) if m else None
            if harness is None or (agent and agent.get("harness") == harness):
                n += 1
        return n

    def _emit_now(self, mission: dict) -> None:
        mid = mission["id"]
        timer = self._stats_timers.pop(mid, None)
        if timer:
            timer.cancel()
        if mid not in self.store.missions:
            return
        self._last_emit[mid] = time.monotonic()
        self.bus.emit("mission.updated", {"mission": mission}, mission_id=mid, agent_id=mission["agent_id"])
        self.store.mark_dirty()

    def _emit_stats(self, mission: dict) -> None:
        """Stats-only updates: at most one mission.updated per second per mission."""
        mid = mission["id"]
        wait = STATS_EVERY_S - (time.monotonic() - self._last_emit.get(mid, 0.0))
        if wait <= 0:
            self._emit_now(mission)
        elif mid not in self._stats_timers:
            loop = asyncio.get_running_loop()
            self._stats_timers[mid] = loop.call_later(wait, self._flush_stats, mid)

    def _flush_stats(self, mid: str) -> None:
        self._stats_timers.pop(mid, None)
        mission = self.store.missions.get(mid)
        if mission is not None:
            self._emit_now(mission)

    def _event(self, mission: dict, type_: str, data: dict) -> None:
        self.bus.emit(type_, data, mission_id=mission["id"], agent_id=mission["agent_id"])

    # ------------------------------------------------------------ lifecycle

    def recover(self) -> None:
        """After a restart: nothing is running any more."""
        for m in self.store.missions.values():
            m.setdefault("notes", [])
            m.setdefault("plan", [])
            m.setdefault("thread_id", None)
            m.setdefault("stats", {})
            for k in ("turns", "steps", "tool_calls", "tool_errors", "prompt_tokens", "completion_tokens"):
                m["stats"].setdefault(k, 0)
            if m.get("status") in ACTIVE:
                m["status"] = "stopped"
                m["error"] = "hub restarted"
                m["ended_ts"] = m.get("ended_ts") or now()
                self._emit_now(m)

    async def close(self) -> None:
        self.closing = True
        tasks = []
        for h in self.active.values():
            h.stop_event.set()
            if h.task:
                tasks.append(h.task)
        for t in self.plan_tasks.values():
            t.cancel()
            tasks.append(t)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=12)
            for t in pending:
                t.cancel()
        for timer in self._stats_timers.values():
            timer.cancel()

    # ------------------------------------------------------------ REST actions

    async def create(self, body: dict) -> dict:
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError(400, "prompt must be a non-empty string")
        agent_id = body.get("agent_id")
        if not isinstance(agent_id, str) or agent_id not in self.store.agents:
            raise ApiError(400, "agent_id must name an existing agent")
        title = body.get("title")
        if title is not None and not isinstance(title, str):
            raise ApiError(400, "title must be a string")
        thread_id = body.get("thread_id") or None
        if thread_id is not None and thread_id not in self.store.threads:
            raise ApiError(404, "thread not found")
        mid = new_id("ms")
        mission = {
            "id": mid,
            "title": (title or "").strip()[:120] or default_title(prompt),
            "prompt": prompt.strip(),
            "agent_id": agent_id,
            "status": "queued",
            "require_plan_approval": bool(body.get("require_plan_approval", False)),
            "plan": [],
            "created_ts": now(),
            "started_ts": None,
            "ended_ts": None,
            "result_text": "",
            "error": "",
            "session_id": "",
            "workspace": f"workspaces/{mid}",
            "runs": 0,
            "stats": {"turns": 0, "steps": 0, "tool_calls": 0, "tool_errors": 0,
                      "prompt_tokens": 0, "completion_tokens": 0},
            "notes": [],
            "thread_id": thread_id,
        }
        if mission["require_plan_approval"]:
            mission["status"] = "planning"
        self.workspace(mission).mkdir(parents=True, exist_ok=True)
        self.store.missions[mid] = mission
        self._emit_now(mission)
        if thread_id:
            self.hub.talk.add_card(thread_id, mid, f"Mission dispatched: {mission['title']}")
        if mission["require_plan_approval"]:
            self.plan_tasks[mid] = asyncio.create_task(self._plan(mid))
        else:
            self._start(mission, self._first_task(mission), resumed=False)
        return mission

    async def approve(self, mission_id: str, body: dict) -> dict:
        mission = self.get(mission_id)
        if mission["status"] != "awaiting_approval":
            raise ApiError(409, f"mission is {mission['status']}, not awaiting_approval")
        edited = body.get("plan")
        if edited is not None:
            if not isinstance(edited, list):
                raise ApiError(400, "plan must be a list of steps")
            texts = []
            for item in edited:
                text = item.get("text") if isinstance(item, dict) else item
                if not isinstance(text, str):
                    raise ApiError(400, "each plan step must be a string or {text}")
                if text.strip():
                    texts.append(text.strip())
            mission["plan"] = plan_steps(texts)
        mission["error"] = ""
        self._event(mission, "human.approved", {"steps": mission["plan"]})
        self._start(mission, self._first_task(mission), resumed=False)
        return mission

    async def stop(self, mission_id: str) -> dict:
        mission = self.get(mission_id)
        handle = self.active.get(mission_id)
        plan_task = self.plan_tasks.get(mission_id)
        if handle is not None:
            handle.user_stop = True
            handle.stop_event.set()
            self._event(mission, "human.stopped", {})
            if handle.task:
                await asyncio.wait({handle.task}, timeout=15)
        elif plan_task is not None:
            self._event(mission, "human.stopped", {})
            plan_task.cancel()
            await asyncio.wait({plan_task}, timeout=5)
        elif mission["status"] in ("queued", "awaiting_approval"):
            self._event(mission, "human.stopped", {})
            mission["status"] = "stopped"
            mission["ended_ts"] = now()
            self._emit_now(mission)
            self._card_on_end(mission)
        return mission

    async def note(self, mission_id: str, body: dict) -> dict:
        mission = self.get(mission_id)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "text must be a non-empty string")
        note = {"id": f"n{len(mission['notes']) + 1}", "ts": now(), "text": text.strip(), "delivered": False}
        mission["notes"].append(note)
        self._event(mission, "human.note", {"note": note})
        self._emit_now(mission)
        handle = self.active.get(mission_id)
        if handle is not None:
            if body.get("interrupt"):
                handle.interrupt = True
                handle.stop_event.set()
            # otherwise delivered when the current run ends
        elif mission["status"] in FINISHED:
            self._start(mission, self._followup_task(mission, ""), resumed=True)
        # queued / planning / awaiting_approval: delivered when the first run starts
        return mission

    async def resume(self, mission_id: str, body: dict) -> dict:
        mission = self.get(mission_id)
        if mission["status"] not in FINISHED or mission_id in self.active:
            raise ApiError(409, f"mission is {mission['status']}; resume works on a finished mission")
        text = body.get("text") or ""
        if not isinstance(text, str):
            raise ApiError(400, "text must be a string")
        self._start(mission, self._followup_task(mission, text.strip()), resumed=True)
        return mission

    async def delete(self, mission_id: str) -> None:
        mission = self.get(mission_id)
        handle = self.active.get(mission_id)
        if handle is not None:
            handle.user_stop = True
            handle.stop_event.set()
            if handle.task:
                await asyncio.wait({handle.task}, timeout=15)
        plan_task = self.plan_tasks.get(mission_id)
        if plan_task is not None:
            plan_task.cancel()
            await asyncio.wait({plan_task}, timeout=5)
        self.store.missions.pop(mission_id, None)
        self._last_emit.pop(mission_id, None)
        self.bus.emit("mission.removed", {"id": mission_id}, mission_id=mission_id,
                      agent_id=mission["agent_id"])
        self.store.mark_dirty()
        ws = self.workspace(mission)
        runs = self.hub.cfg.data_dir / "runs"
        await asyncio.to_thread(shutil.rmtree, ws, True)
        if runs.is_dir():
            for d in runs.glob(f"{mission_id}-r*"):
                await asyncio.to_thread(shutil.rmtree, d, True)

    # ------------------------------------------------------------ task text

    def _take_notes(self, mission: dict) -> list[str]:
        texts = []
        for n in mission["notes"]:
            if not n.get("delivered"):
                n["delivered"] = True
                texts.append(n["text"])
        return texts

    def _first_task(self, mission: dict) -> str:
        task = mission["prompt"]
        if mission["plan"]:
            steps = "\n".join(f"{i}. {s['text']}" for i, s in enumerate(mission["plan"], 1))
            task += f"\n\nApproved plan:\n{steps}"
        notes = self._take_notes(mission)
        if notes:
            task += "\n\nNotes from the human:\n" + "\n".join(f"- {t}" for t in notes)
        return task

    def _followup_task(self, mission: dict, text: str) -> str:
        parts = [text] if text else []
        notes = self._take_notes(mission)
        if notes:
            parts.append("Notes from the human:\n" + "\n".join(f"- {t}" for t in notes))
        body = "\n\n".join(parts) or "Continue where you left off and finish the task."
        if not mission.get("session_id"):
            # no harness session to resume: restate the original task
            body = f"{mission['prompt']}\n\nFollow-up:\n{body}"
        return body

    # ------------------------------------------------------------ planning

    async def _plan(self, mission_id: str) -> None:
        mission = self.store.missions.get(mission_id)
        try:
            try:
                texts = await self._propose(mission)
                error = "" if texts else "the model returned an empty plan; edit the steps or approve as is"
            except (ChatError, RuntimeError) as e:
                texts, error = [], f"plan request failed: {e}"
            if mission_id not in self.store.missions:
                return
            mission["plan"] = plan_steps(texts)
            mission["status"] = "awaiting_approval"
            mission["error"] = error
            self._event(mission, "plan.proposed", {"steps": mission["plan"]})
            self._emit_now(mission)
        except asyncio.CancelledError:
            if mission_id in self.store.missions:
                mission["status"] = "stopped"
                mission["error"] = "hub shut down" if self.closing else ""
                mission["ended_ts"] = now()
                self._emit_now(mission)
                self._card_on_end(mission)
            raise
        finally:
            self.plan_tasks.pop(mission_id, None)

    async def _propose(self, mission: dict) -> list[str]:
        agent = self.store.agents.get(mission["agent_id"]) or {}
        model = self.store.models.get(agent.get("model_id", ""))
        if model is None:
            raise RuntimeError("the agent has no model to plan with")
        if model.get("kind") == "mock":
            await asyncio.sleep(2.0 / mock_speed())
            return mock_plan(mission["prompt"], agent)
        persona = (agent.get("persona") or "").strip() or f"You are {agent.get('name', 'an assistant')}."
        messages = [{"role": "system", "content": persona},
                    {"role": "user", "content": PLAN_PROMPT.format(task=mission["prompt"])}]
        text, _, _ = await stream_chat(self.hub.http, model, messages, timeout_s=300)
        return parse_plan(text)

    # ------------------------------------------------------------ runs

    def _start(self, mission: dict, task: str, resumed: bool) -> None:
        handle = _Handle()
        self.active[mission["id"]] = handle
        mission["status"] = "running"
        mission["error"] = ""
        self._emit_now(mission)
        handle.task = asyncio.create_task(self._drive(mission["id"], task, resumed, handle))

    async def _drive(self, mission_id: str, task: str, resumed: bool, handle: _Handle) -> None:
        try:
            while True:
                mission = self.store.missions.get(mission_id)
                if mission is None:
                    return
                ok, error = await self._one_run(mission, task, resumed, handle)
                if mission_id not in self.store.missions:
                    return
                waiting = [n for n in mission["notes"] if not n.get("delivered")]
                if waiting and not handle.user_stop and not self.closing:
                    task, resumed = self._followup_task(mission, ""), True
                    handle.stop_event = asyncio.Event()
                    handle.interrupt = False
                    continue
                if ok:
                    mission["status"], mission["error"] = "done", ""
                elif handle.stop_event.is_set():
                    mission["status"] = "stopped"
                    mission["error"] = "hub shut down" if self.closing else ""
                else:
                    mission["status"], mission["error"] = "failed", error
                mission["ended_ts"] = now()
                self._emit_now(mission)
                self._card_on_end(mission)
                return
        except Exception:
            log.exception("mission %s crashed", mission_id)
            mission = self.store.missions.get(mission_id)
            if mission is not None:
                mission["status"], mission["error"] = "failed", "hub error while running (see hub log)"
                mission["ended_ts"] = now()
                self._emit_now(mission)
        finally:
            if self.active.get(mission_id) is handle:
                del self.active[mission_id]

    async def _one_run(self, mission: dict, task: str, resumed: bool, handle: _Handle) -> tuple[bool, str]:
        agent = self.store.agents.get(mission["agent_id"])
        model = self.store.models.get(agent.get("model_id", "")) if agent else None
        mission["runs"] += 1
        run_no = mission["runs"]
        mission["status"] = "running"
        mission["started_ts"] = mission.get("started_ts") or now()
        mission["ended_ts"] = None
        mission["error"] = ""
        previous = mission.get("result_text") or ""
        mission["result_text"] = ""
        self._emit_now(mission)

        state = _RunState()
        cwd = self.workspace(mission)
        cwd.mkdir(parents=True, exist_ok=True)
        ctx = RunContext(
            mission=mission,
            agent=agent or {},
            model=model,
            task=task,
            cwd=str(cwd),
            session_id=mission.get("session_id", "") if resumed else "",
            emit=lambda t, d: self._on_event(mission, state, t, d),
            stop_event=handle.stop_event,
            run=run_no,
            resumed=resumed,
            data_dir=self.hub.cfg.data_dir,
            http=self.hub.http,
            crew=[a for a in self.store.agents.values() if not agent or a["id"] != agent["id"]],
            previous_result=previous,
        )
        started = time.monotonic()
        harness = get_harness(agent.get("harness", "")) if agent else None
        if agent is None:
            ctx.emit("run.error", {"message": "the agent for this mission no longer exists"})
        elif harness is None:
            ctx.emit("run.error", {"message": f"unknown harness {agent.get('harness')!r}"})
        else:
            try:
                await harness.run(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("harness %s crashed", agent.get("harness"))
                ctx.emit("run.error", {"message": f"harness crashed: {e}"})
        code = ctx.exit_code if ctx.exit_code is not None else -1
        ok = code == 0 and state.final_kind == "completed"
        self._event(mission, "run.ended", {"run": run_no, "exit_code": code, "ok": ok,
                                           "duration_s": round(time.monotonic() - started, 2)})
        if not mission["result_text"] and previous:
            mission["result_text"] = previous
        if ok:
            error = ""
        elif state.last_error:
            error = state.last_error
        elif state.final_kind:
            error = f"run ended with reason {state.final_kind!r}"
        else:
            error = f"harness exited with code {code} before a final answer"
        return ok, error

    def _on_event(self, mission: dict, state: _RunState, type_: str, data: dict) -> None:
        """Every run event passes through here: store it, then update the mission."""
        data = data if isinstance(data, dict) else {"value": data}
        self._event(mission, type_, data)
        if mission["id"] not in self.store.missions:
            return
        stats = mission["stats"]
        structural = False
        counted = False
        if type_ == "run.started":
            sid = data.get("session_id")
            if sid and sid != mission.get("session_id"):
                mission["session_id"] = str(sid)
                structural = True
        elif type_ == "run.status":
            phase = data.get("phase")
            if phase == "turn_start":
                stats["turns"] += 1
                state.step_usage_in_turn = False
                counted = True
            elif phase == "step_start":
                stats["steps"] += 1
                counted = True
            elif phase == "turn_end":
                state.turn_end_kind = _reason_kind(data.get("reason"))
            if "usage" in data and (phase != "turn_end" or not state.step_usage_in_turn):
                p, c = usage_tokens(data["usage"])
                if p or c:
                    stats["prompt_tokens"] += p
                    stats["completion_tokens"] += c
                    counted = True
                if phase == "step_end":
                    state.step_usage_in_turn = True
        elif type_ == "run.tool_call":
            stats["tool_calls"] += 1
            counted = True
            plan = todo_plan(data.get("tool"), data.get("input"))
            if plan is not None:
                mission["plan"] = plan
                structural = True
        elif type_ == "run.tool_result":
            if data.get("status") == "error":
                stats["tool_errors"] += 1
                counted = True
        elif type_ == "run.final":
            # dsh 0.2.1-rc.5 sends `final` with reason null and puts the reason on the
            # `turn_end` status just before it, so fall back to that one.
            state.final_kind = _reason_kind(data.get("reason")) or state.turn_end_kind
            mission["result_text"] = str(data.get("text") or "")
            sid = data.get("session_id")
            if sid and not mission.get("session_id"):
                mission["session_id"] = str(sid)
            structural = True
        elif type_ == "run.error":
            state.last_error = str(data.get("message") or "harness error")
        if structural:
            self._emit_now(mission)
        elif counted:
            self._emit_stats(mission)

    def _card_on_end(self, mission: dict) -> None:
        """The "it hits back when done" loop: a final card in the Talk thread."""
        thread_id = mission.get("thread_id")
        if not thread_id:
            return
        title = mission["title"]
        if mission["status"] == "done":
            text = f"Mission done: {title}\n\n{mission.get('result_text') or '(no final text)'}"
        elif mission["status"] == "failed":
            text = f"Mission failed: {title}\n\n{mission.get('error') or 'unknown error'}"
        else:
            text = f"Mission stopped: {title}" + (f" ({mission['error']})" if mission.get("error") else "")
        self.hub.talk.add_card(thread_id, mission["id"], text)
