"""Commands from the phone: hub -> daemon, through one file per mission.

The hub appends one JSON line per command to missions/<id>/state/inbox.jsonl (submit).
The daemon's housekeeping loop calls poll(daemon) every ~15 s. Each command is applied once,
its result goes to state/inbox_applied.json and to the event log as a `command` event.

  instruct {track | "all", text}   a standing instruction. It joins the memory packet of that track
                                   (memory.packet), so every read, search and consolidation sees it.
  focus    {track | "all", topic}  a priority topic: scouting is told to look for it and matching
                                   papers move up the reading queue.
  clear    {track | "all"}         clear the agent's context: drop its standing instructions and
                                   priority topics, rebuild its digest from the ledger, reset its
                                   saturation and low-yield counters. Cards and the ledger are kept.
  pause / resume {track | "all"}   weight 0 (no new papers, no scouting) / the weight it had before.
  mission  {title, brief, mode, hours}   write missions/<NNN-slug>/brief.md. mode queue: it starts when
                                   the current mission ends. mode preempt: the daemon finishes the
                                   papers in hand and switches to it.

State, all under missions/<id>/state/:
  inbox.jsonl          commands, appended by the hub (never rewritten)
  inbox_applied.json   {"ids": [...], "items": [last 60 results]}, written by the daemon
  directives.json      {"instructions": [...], "focus": [...], "paused": {tid: weight before}}, written by the daemon

This module imports only the standard library at the top, so the hub can use clean / submit /
pending / directives / new_mission without loading the pipeline.
"""
from __future__ import annotations

import os
import re
import secrets

from . import live
from .memory import append, now, read_json, read_jsonl, write, write_json

CMDS = ("instruct", "focus", "clear", "pause", "resume", "mission", "finish")
MAX_TEXT, MAX_TOPIC, MAX_TITLE, MAX_BRIEF = 600, 120, 80, 60000
KEEP_INSTRUCTIONS, KEEP_FOCUS, KEEP_RESULTS = 40, 30, 60


# ---------------------------------------------------------------- shared by hub and daemon


def _line(s, limit: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def clean(body: dict, track_ids: list[str]) -> dict:
    """Validate one command. Returns the record to append; raises ValueError with a plain message."""
    cmd = body.get("cmd")
    if cmd not in CMDS:
        raise ValueError(f"cmd must be one of {', '.join(CMDS)}")
    rec: dict = {"id": "c_" + secrets.token_hex(5), "cmd": cmd, "by": _line(body.get("by") or "phone", 30)}
    if cmd == "mission":
        title = _line(body.get("title"), MAX_TITLE)
        brief = str(body.get("brief") or "").strip()[:MAX_BRIEF]
        if not title:
            raise ValueError("a mission needs a title")
        if len(brief) < 20:
            raise ValueError("a mission needs a brief: what to find out, in a few sentences or more")
        mode = body.get("mode") or "queue"
        if mode not in ("queue", "preempt"):
            raise ValueError("mode must be queue or preempt")
        try:
            hours = float(body.get("hours") or 12)
        except (TypeError, ValueError):
            raise ValueError("hours must be a number") from None
        rec.update(title=title, brief=brief, mode=mode, hours=min(max(hours, 0.5), 12.0))
        return rec
    track = body.get("track") or "all"
    if track != "all" and track not in track_ids:
        raise ValueError(f"unknown agent {track!r}; this mission has {', '.join(track_ids) or 'no agents yet'}")
    rec["track"] = track
    if cmd == "instruct":
        rec["text"] = _line(body.get("text"), MAX_TEXT)
        if not rec["text"]:
            raise ValueError("the instruction is empty")
    elif cmd == "focus":
        rec["topic"] = _line(body.get("topic") or body.get("text"), MAX_TOPIC)
        if not rec["topic"]:
            raise ValueError("the topic is empty")
    return rec


def submit(mroot: str, rec: dict) -> dict:
    """Append one cleaned command to the mission's inbox. One open-write-close per command."""
    append(os.path.join(mroot, "state", "inbox.jsonl"), rec)
    return rec


def applied(mroot: str) -> dict:
    d = read_json(os.path.join(mroot, "state", "inbox_applied.json"), None)
    if not isinstance(d, dict):
        d = {}
    return {"ids": list(d.get("ids") or []), "items": list(d.get("items") or [])}


def pending(mroot: str) -> list[dict]:
    """Commands the daemon has not applied yet (the brief text of a mission is left out)."""
    done = set(applied(mroot)["ids"])
    return [{k: v for k, v in c.items() if k != "brief"}
            for c in read_jsonl(os.path.join(mroot, "state", "inbox.jsonl")) if c.get("id") and c["id"] not in done]


def directives(mroot: str) -> dict:
    d = read_json(os.path.join(mroot, "state", "directives.json"), None)
    if not isinstance(d, dict):
        d = {}
    return {"instructions": [x for x in d.get("instructions") or [] if isinstance(x, dict)],
            "focus": [x for x in d.get("focus") or [] if isinstance(x, dict)],
            "paused": dict(d.get("paused") or {})}


def new_mission(repo: str, title: str, brief: str, mode: str = "queue", hours: float = 12.0) -> str:
    """Write missions/<NNN-slug>/brief.md with the front matter the daemon reads. Returns the mission id."""
    root = os.path.join(repo, "missions")
    os.makedirs(root, exist_ok=True)
    nums = [int(mm.group(1)) for mm in (re.match(r"(\d+)", d) for d in os.listdir(root)) if mm]
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "mission"
    mid = f"{max(nums, default=0) + 1:03d}-{slug}"
    body = brief.strip()
    if not body.startswith("#"):
        body = f"# {title}\n\n{body}"
    front = f"---\nmode: {'preempt' if mode == 'preempt' else 'queue'}\nhours: {float(hours):g}\ntitle: {_line(title, MAX_TITLE)}\n---\n"
    write(os.path.join(root, mid, "brief.md"), front + body + "\n")
    return mid


# ---------------------------------------------------------------- daemon side


def _save(m, D: dict) -> None:
    write_json(m.p("state", "directives.json"), D)


def _sync_focus(d, D: dict) -> None:
    """Phone focus topics live in the daemon's steer dict next to the validator's priority topics."""
    kept = [p for p in d.steer.get("priority_topics", []) if p.get("src") != "phone"]
    d.steer["priority_topics"] = kept + [{"track": f.get("track", "all"), "topic": f.get("topic", ""), "boost": float(f.get("boost", 0.2)),
                                          "src": "phone", "id": f.get("id")} for f in D["focus"] if f.get("topic")]


def restore(d) -> None:
    """At mission start: bring the persisted focus topics back (instructions and pauses are read from files)."""
    try:
        _sync_focus(d, directives(d.m.root))
    except Exception as e:
        d.err(f"inbox restore: {type(e).__name__}: {str(e)[:160]}")


def _plan_weight(m, tid: str) -> float:
    try:
        return float(m.track(tid).get("weight", 1.0)) or 1.0
    except (TypeError, ValueError):
        return 1.0


def _apply(d, c: dict) -> str:
    m = d.m
    cmd = c.get("cmd")
    if cmd == "mission":
        mode = c.get("mode") or "queue"
        mid = new_mission(d.repo, c.get("title") or "mission", c.get("brief") or "", mode, c.get("hours") or 12)
        if mode == "preempt":
            d.stop = True  # the run loop joins the threads, syncs, and pick_mission prefers the preempt brief
            return f"mission {mid} written; switching to it once the papers in hand are finished"
        return f"mission {mid} written; it starts when the current mission ends"
    if cmd == "finish":  # the app's Finish now: final reports and synthesis from what is in hand
        return d.finish()
    tid = c.get("track") or "all"
    all_tids = m.track_ids()
    if tid != "all" and tid not in all_tids:
        raise ValueError(f"unknown track {tid}")
    tids = all_tids if tid == "all" else [tid]
    D = directives(m.root)
    if cmd == "instruct":
        text = _line(c.get("text"), MAX_TEXT)
        if not text:
            raise ValueError("empty instruction")
        if all(x.get("id") != c["id"] for x in D["instructions"]):
            D["instructions"] = (D["instructions"] + [{"id": c["id"], "track": tid, "text": text, "ts": now()}])[-KEEP_INSTRUCTIONS:]
            _save(m, D)
        return f"standing instruction added for {tid}"
    if cmd == "focus":
        topic = _line(c.get("topic"), MAX_TOPIC)
        if not topic:
            raise ValueError("empty topic")
        if all(x.get("id") != c["id"] for x in D["focus"]):
            D["focus"] = (D["focus"] + [{"id": c["id"], "track": tid, "topic": topic, "boost": 0.2, "ts": now()}])[-KEEP_FOCUS:]
            _save(m, D)
        _sync_focus(d, D)
        for t in tids:  # a new topic reopens a saturated track and brings the next search forward
            d.track_state.setdefault(t, {}).update(saturated=False, low_rounds=0, last_scout=0)
        return f"priority topic added for {tid}"
    if cmd == "clear":
        hit = (lambda x: True) if tid == "all" else (lambda x: x.get("track") == tid)
        n_i = sum(1 for x in D["instructions"] if hit(x))
        n_f = sum(1 for x in D["focus"] if hit(x))
        D["instructions"] = [x for x in D["instructions"] if not hit(x)]
        D["focus"] = [x for x in D["focus"] if not hit(x)]
        _save(m, D)
        before = len(d.steer.get("priority_topics", []))
        d.steer["priority_topics"] = [p for p in d.steer.get("priority_topics", []) if not hit(p)]
        n_v = before - len(d.steer["priority_topics"]) - n_f
        from . import agents as A  # the pipeline, loaded only inside the daemon
        for t in tids:
            A.render_digest(m, t)
            d.track_state.setdefault(t, {}).update(saturated=False, low_rounds=0, last_scout=0)
        return (f"context cleared for {tid}: {n_i} standing instructions and {n_f + max(n_v, 0)} priority topics dropped, "
                "digest rebuilt from the ledger, search counters reset; cards and the ledger are kept")
    if cmd in ("pause", "resume"):
        path = m.p("state", "weights.json")
        w = read_json(path, {}) or {}
        changed = []
        for t in tids:
            if cmd == "pause":
                if t in D["paused"]:
                    continue
                try:
                    prev = float(w.get(t, _plan_weight(m, t)))
                except (TypeError, ValueError):
                    prev = 1.0
                D["paused"][t] = prev if prev > 0 else _plan_weight(m, t)
                w[t] = 0.0
            else:
                if t not in D["paused"] and float(w.get(t, 1.0) or 0) > 0:
                    continue
                prev = D["paused"].pop(t, None)
                w[t] = float(prev) if prev and float(prev) > 0 else _plan_weight(m, t)
            changed.append(t)
        write_json(path, w)
        _save(m, D)
        if not changed:
            return f"{tid} was already {'paused' if cmd == 'pause' else 'running'}"
        if cmd == "pause":
            return f"paused {', '.join(changed)}: no new papers and no scouting; a paper already being read is finished"
        return f"resumed {', '.join(changed)}"
    raise ValueError(f"unknown command {cmd!r}")


def _publish(d) -> None:
    """Per-track state for the hub (weight, saturated, paused), into live.json."""
    m = d.m
    w = d.weights()
    paused = directives(m.root)["paused"]
    live.put("tracks", {t: {"weight": w.get(t, 1.0), "paused": t in paused,
                            "saturated": bool(d.track_state.get(t, {}).get("saturated")),
                            "low_rounds": int(d.track_state.get(t, {}).get("low_rounds", 0) or 0),
                            "unconsolidated": len(d.track_state.get(t, {}).get("new_cards", []) or [])}
                        for t in m.track_ids()})


def poll(d) -> int:
    """Apply every new command once. Called from the daemon's housekeeping loop; never raises."""
    m = d.m
    if m is None:
        return 0
    n = 0
    try:
        _publish(d)
        recs = read_jsonl(m.p("state", "inbox.jsonl"))
        if not recs:
            return 0
        ap = applied(m.root)
        done = set(ap["ids"])
        for c in recs:
            cid = c.get("id")
            if not cid or cid in done:
                continue
            try:
                res, ok = _apply(d, c), True
            except Exception as e:
                res, ok = f"{type(e).__name__}: {str(e)[:200]}", False
            done.add(cid)
            what = _line(c.get("text") or c.get("topic") or c.get("title") or "", 200)
            ap["ids"].append(cid)
            ap["items"] = (ap["items"] + [{"id": cid, "cmd": c.get("cmd"), "track": c.get("track"), "text": what,
                                           "ok": ok, "result": res[:300], "sent": c.get("ts"), "ts": now()}])[-KEEP_RESULTS:]
            write_json(m.p("state", "inbox_applied.json"), ap)  # after each command, so a crash never applies one twice
            m.log("command", id=cid, cmd=c.get("cmd"), track=c.get("track"), text=what, ok=ok, result=res[:300])
            print(f"[inbox] {c.get('cmd')} {c.get('track') or ''} -> {res[:160]}", flush=True)
            n += 1
        if n:
            _publish(d)
    except Exception as e:
        d.err(f"inbox: {type(e).__name__}: {str(e)[:200]}")
    return n


def status_lines(m) -> list[str]:
    """Two short sections for STATUS.md: what is standing, and the last commands."""
    try:
        D = directives(m.root)
        L = ["", "## Standing instructions (from the phone)"]
        L += [f"- {x.get('track')}: {x.get('text')}" for x in D["instructions"]] or ["- none"]
        L += [f"- {x.get('track')} priority topic: {x.get('topic')}" for x in D["focus"]]
        if D["paused"]:
            L.append("- paused: " + ", ".join(sorted(D["paused"])))
        items = applied(m.root)["items"][-8:][::-1]
        L += ["", "## Phone commands (last 8)"]
        L += [f"- {str(x.get('ts', ''))[11:16]} {x.get('cmd')} {x.get('track') or ''} "
              f"{'ok' if x.get('ok') else 'FAILED'}: {x.get('result', '')}" for x in items] or ["- none"]
        n = len(pending(m.root))
        if n:
            L.append(f"- waiting in the inbox: {n}")
        return L
    except Exception as e:
        return ["", f"## Phone commands: unreadable ({type(e).__name__})"]
