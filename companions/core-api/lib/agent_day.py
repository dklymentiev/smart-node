"""What one agent did, and what was addressed to it, over a window.

This is the facts layer behind the "your day" block of an agent's prompt. A new
session has no memory of the last one, and the only things it was told were a
dozen cross-channel lines and a few minutes of its own events. Asked "what did you
work on today" it answered from that, with confidence, and was wrong: it named the
previous evening's tasks and left out the two it had actually worked.

The records to answer it already exist, in three places, and nobody read them
together: the event journal says which planner tasks the agent touched, the task
comments say what it wrote, the internal mail says who woke it and who told it
what. The planner can also be asked which open tasks name the agent as the doer
(the filer writes "Assignee=<name>" in the description) and have not been
started, the case an approved task with nobody assigned produces.

Contract, as for the project state collector next to it:

- Deterministic: no model in this module. The caller writes the words.
- The planner is read through its HTTP surface only. The event journal and the
  internal mail are core-api's own tables and are read here directly.
- One failing source is named in `notes` and the rest is returned. A prompt
  block must never cost a turn, so nothing here raises to the caller.
- `assemble_card` is pure. All reading happens in `collect`, so the rules about
  what counts as done, waiting or not started can be tested without a database.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from lib import project_state as _ps

log = logging.getLogger("core-api.agent_day")

SCHEMA_VERSION = 1

# Caps keep the block small: it is folded into the prompt of every wake.
MAX_DONE = 12
MAX_OPEN = 8
MAX_NOT_STARTED = 6
MAX_MAIL = 8
# Tasks beyond MAX_DONE are not dropped: they are listed one short line each,
# grouped by what was done to them, so a day of thirty touches still shows all.
MAX_TAIL = 40
_TAIL_TITLE = 40
_COMMENT_SNIPPET = 110
_MAIL_SNIPPET = 110
_EVENTS_CAP = 3000
_MAIL_CAP = 500

# The planner operations that change a task. Reads (get, list, search) say the
# agent looked, not that it worked.
_WRITE_OPS = ("planner.comment", "planner.update", "planner.create")

_TASK_IN_PATH = re.compile(r"tasks/(\d+)")
_TASK_IN_TEXT = re.compile(r"#(\d{3,6})")


def _snip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return (s[:n] + "...") if len(s) > n else s


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def assemble_card(
    agent: str,
    since: datetime,
    until: datetime,
    *,
    events: list[dict],
    mail: list[dict],
    tasks: dict[int, dict],
    own_comments: dict[int, list[dict]],
    open_assigned: list[dict],
    candidates: list[dict],
    notes: list[str] | None = None,
) -> dict:
    """The facts, ordered and capped. Pure: every input is already read.

    events:        {"ts": datetime, "op": str, "path": str} of the agent's tool calls
    mail:          {"ts", "sender", "recipient", "content"} internal messages to or
                   from the agent, the system's wake notices included
    tasks:         task id -> {"title", "status", "assignee"} for the touched tasks
    own_comments:  task id -> the agent's own comments inside the window
    open_assigned: {"id", "title", "status", "updated_at"} tasks assigned to the agent
    candidates:    {"id", "title", "status", "assignee", "created_at"} open tasks that
                   name the agent as the intended doer
    """
    agent = agent.lower()
    touched: dict[int, dict] = {}
    for e in events:
        if e.get("op") not in _WRITE_OPS:
            continue
        m = _TASK_IN_PATH.search(e.get("path") or "")
        if not m:
            continue
        tid = int(m.group(1))
        d = touched.setdefault(tid, {"first": e["ts"], "last": e["ts"], "moves": 0, "comment_ops": 0})
        d["first"] = min(d["first"], e["ts"])
        d["last"] = max(d["last"], e["ts"])
        if e["op"] == "planner.update":
            d["moves"] += 1
        elif e["op"] == "planner.comment":
            d["comment_ops"] += 1

    # Wake notices from the system name the task; a message from a colleague that
    # mentions the task is who handed it over.
    woken: dict[int, int] = {}
    ordered_by: dict[int, dict] = {}
    for m in sorted(mail, key=lambda x: x["ts"]):
        text = m.get("content") or ""
        sender = (m.get("sender") or "").lower()
        recipient = (m.get("recipient") or "").lower()
        ids = {int(x) for x in _TASK_IN_TEXT.findall(text)}
        if sender == "system" and recipient == agent:
            for tid in ids:
                woken[tid] = woken.get(tid, 0) + 1
        elif recipient == agent and sender not in ("system", agent):
            for tid in ids:
                ordered_by.setdefault(tid, {"sender": m.get("sender"), "ts": m["ts"]})

    order = sorted(touched, key=lambda t: touched[t]["last"], reverse=True)
    done = []
    for tid in order[:MAX_DONE]:
        d = touched[tid]
        info = tasks.get(tid) or {}
        cs = sorted(own_comments.get(tid) or [], key=lambda c: c["created_at"])
        done.append({
            "id": tid,
            "title": info.get("title"),
            "status": info.get("status"),
            "assignee": info.get("assignee"),
            "comments": len(cs) if cs else d["comment_ops"],
            "moves": d["moves"],
            "first": _iso(d["first"]),
            "last": _iso(d["last"]),
            "woken": woken.get(tid, 0),
            "ordered_by": ({"sender": ordered_by[tid]["sender"], "ts": _iso(ordered_by[tid]["ts"])}
                           if tid in ordered_by else None),
            "last_comment": _snip(cs[-1]["content"], _COMMENT_SNIPPET) if cs else None,
        })

    # The rest of the day, grouped by what was done: a batch (nine approvals in a
    # row, eight duplicates closed) must stay visible as a batch, and it is
    # exactly the part a list cut by recency drops.
    groups: dict[str, list[dict]] = {"closed": [], "updated": [], "commented": []}
    for tid in order[MAX_DONE:MAX_DONE + MAX_TAIL]:
        d = touched[tid]
        info = tasks.get(tid) or {}
        if (info.get("status") or "") == "closed":
            kind = "closed"
        elif d["moves"]:
            kind = "updated"
        else:
            kind = "commented"
        groups[kind].append({"id": tid, "title": _snip(info.get("title"), _TAIL_TITLE),
                             "status": info.get("status"), "assignee": info.get("assignee")})
    others = [{"kind": k, "tasks": v} for k, v in groups.items() if v]
    others_total = max(len(touched) - MAX_DONE, 0)

    not_started = []
    for t in sorted(candidates, key=lambda t: t.get("created_at") or since, reverse=True):
        if t["id"] in touched:
            continue
        if (t.get("status") or "") == "closed":
            continue
        a = (t.get("assignee") or "").lower()
        if a and a != agent:
            continue
        not_started.append({
            "id": t["id"], "title": t.get("title"), "status": t.get("status"),
            "assignee": t.get("assignee"), "woken": woken.get(t["id"], 0),
            "created_at": _iso(t.get("created_at")),
        })
    not_started = not_started[:MAX_NOT_STARTED]

    # Open work assigned to the agent that is neither done today nor already
    # listed as not started: it waits on someone else, or on a later wake.
    listed = set(touched) | {t["id"] for t in not_started}
    open_rows = [
        {"id": t["id"], "title": t.get("title"), "status": t.get("status"),
         "updated_at": _iso(t.get("updated_at"))}
        for t in sorted(open_assigned, key=lambda t: t.get("updated_at") or since, reverse=True)
        if t["id"] not in listed
    ][:MAX_OPEN]

    people = [m for m in mail if (m.get("sender") or "").lower() != "system"]
    people.sort(key=lambda m: m["ts"])
    mail_rows = [
        {"ts": _iso(m["ts"]), "sender": m.get("sender"), "recipient": m.get("recipient"),
         "preview": _snip(m.get("content"), _MAIL_SNIPPET)}
        for m in people[-MAX_MAIL:]
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "agent": agent,
        "window_from": _iso(since),
        "window_to": _iso(until),
        "done": done,
        "done_total": len(touched),
        "others": others,
        "others_total": others_total,
        "open_assigned": open_rows,
        "not_started": not_started,
        "mail": mail_rows,
        "notes": list(notes or []),
    }


# ----------------------------------------------------------------------- reading

def _read_events(agent: str, since: datetime, until: datetime) -> list[dict]:
    from db import cursor
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "SELECT ts, payload->>'op' AS op, payload->>'path' AS path "
            "FROM sn_core.events "
            "WHERE actor = %s AND event_type = 'tool.invoked' AND ts >= %s AND ts < %s "
            "ORDER BY ts LIMIT %s",
            (f"agent:{agent}", since, until, _EVENTS_CAP),
        )
        return [dict(r) for r in cur.fetchall()]


def _read_mail(agent: str, since: datetime, until: datetime) -> list[dict]:
    from db import cursor
    with cursor() as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            "SELECT received_at AS ts, sender, recipient, content "
            "FROM sn_core.messages "
            "WHERE channel_type = 'internal' "
            "  AND (lower(sender) = %s OR lower(recipient) = %s) "
            "  AND received_at >= %s AND received_at < %s "
            "ORDER BY received_at LIMIT %s",
            (agent, agent, since, until, _MAIL_CAP),
        )
        return [dict(r) for r in cur.fetchall()]


def _as_list(x: Any) -> list:
    return x if isinstance(x, list) else []


def collect(agent: str, since: datetime, until: datetime | None = None) -> dict:
    """Read the sources and assemble the card. Never raises."""
    t0 = time.monotonic()
    agent = (agent or "").strip().lower()
    until = until or datetime.now(timezone.utc)
    notes: list[str] = []

    events: list[dict] = []
    mail: list[dict] = []
    try:
        events = _read_events(agent, since, until)
        if len(events) >= _EVENTS_CAP:
            notes.append(f"event journal read stopped at {_EVENTS_CAP} rows; older calls in the window are not shown")
    except Exception as ex:  # a missing source is a note, not a failure
        log.warning("agent_day: events read failed for %s: %s", agent, ex)
        notes.append("event journal not read")
    try:
        mail = _read_mail(agent, since, until)
    except Exception as ex:
        log.warning("agent_day: mail read failed for %s: %s", agent, ex)
        notes.append("internal mail not read")

    touched_ids: list[int] = []
    for e in events:
        if e.get("op") in _WRITE_OPS:
            m = _TASK_IN_PATH.search(e.get("path") or "")
            if m and int(m.group(1)) not in touched_ids:
                touched_ids.append(int(m.group(1)))
    # Newest touched first; only the capped ones are described in detail.
    last_seen: dict[int, datetime] = {}
    for e in events:
        m = _TASK_IN_PATH.search(e.get("path") or "")
        if m and e.get("op") in _WRITE_OPS:
            last_seen[int(m.group(1))] = e["ts"]
    ranked = sorted(touched_ids, key=lambda t: last_seen[t], reverse=True)
    detail_ids = ranked[:MAX_DONE]
    tail_ids = ranked[MAX_DONE:MAX_DONE + MAX_TAIL]

    tasks: dict[int, dict] = {}
    own_comments: dict[int, list[dict]] = {}
    planner_ok = True
    for tid in detail_ids:
        t = _ps._planner_get(f"api/v1/tasks/{tid}")
        if isinstance(t, dict):
            tasks[tid] = {"title": t.get("title"), "status": t.get("status"), "assignee": t.get("assignee")}
        else:
            planner_ok = False
        cs = _ps._planner_get(f"api/v1/tasks/{tid}/comments")
        mine = []
        for c in _as_list(cs):
            if not isinstance(c, dict) or (c.get("author") or "").lower() != agent:
                continue
            ts = _ps._parse_ts(c.get("created_at"))
            if ts and since <= ts < until:
                mine.append({"created_at": ts, "content": c.get("content")})
        own_comments[tid] = mine
    if tail_ids:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as pool:
            for tid, t in zip(tail_ids, pool.map(lambda i: _ps._planner_get(f"api/v1/tasks/{i}"), tail_ids)):
                if isinstance(t, dict):
                    tasks[tid] = {"title": t.get("title"), "status": t.get("status"),
                                  "assignee": t.get("assignee")}
                else:
                    planner_ok = False
    if (detail_ids or tail_ids) and not planner_ok:
        notes.append("some task titles could not be read from the planner")

    open_assigned: list[dict] = []
    rows = _ps._planner_get("api/v1/tasks?" + urllib.parse.urlencode({"assignee": agent, "limit": 100}))
    if rows is None:
        notes.append("planner task list not read")
    for t in _as_list(rows):
        if isinstance(t, dict) and (t.get("status") or "") != "closed":
            open_assigned.append({"id": t["id"], "title": t.get("title"), "status": t.get("status"),
                                  "updated_at": _ps._parse_ts(t.get("updated_at"))})

    candidates: list[dict] = []
    hits = _ps._planner_get("api/v1/tasks/search?" + urllib.parse.urlencode(
        {"q": f"Assignee={agent}", "limit": 40}))
    if hits is None:
        notes.append("planner search not read")
    horizon = until - timedelta(days=3)
    for t in _as_list(hits):
        if not isinstance(t, dict):
            continue
        created = _ps._parse_ts(t.get("created_at"))
        if created and created >= horizon:
            candidates.append({"id": t["id"], "title": t.get("title"), "status": t.get("status"),
                               "assignee": t.get("assignee"), "created_at": created})

    card = assemble_card(
        agent, since, until, events=events, mail=mail, tasks=tasks,
        own_comments=own_comments, open_assigned=open_assigned, candidates=candidates, notes=notes)
    card["built_ms"] = round((time.monotonic() - t0) * 1000)
    return card
