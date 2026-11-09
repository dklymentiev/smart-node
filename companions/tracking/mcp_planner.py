#!/usr/bin/env python3
"""The node planner as an MCP server (stdio) -- the transport the planner never had.

The node already ships the planner BACKEND (smart-node-planner, REST on :8080) and
core-api already proxies it at /api/planner/... , where agent identity is checked
and the audit row. What was missing is a way for an agent to CALL it. The catalog
carried api-kind rows `planner` and `planner_tasks`, but the CLI runtime only sees
tools listed in .mcp.json, so those rows were a contract with no way to execute for
any agent without a shell. This server closes that gap over endpoints that already
exist: no new endpoint, no new state, no new port, no new container.

Identity is NOT a parameter. The caller is taken from SN_AGENT_SLUG, which the gateway
sets from the agent's own folder name, falling back to the working directory name
(claude runs with cwd = /srv/agents/<slug>). The model can therefore neither forget
nor spoof who it is filing as, and `assignee` is that same slug. That is the whole
reason this is stdio and not a shared HTTP sidecar: a rendered .mcp.json http entry
carries no headers and no env, so one shared sidecar would be anonymous and every
agent's tasks would belong to the same nobody.

WHY THE SOCKET AND NOT THE ADDRESS.
core-api answers on both doors, and on the TCP door an agent identity is refused
outright: a Remote-User there is an assertion anything on the node could make, so
core-api answers 403 and names the socket in the detail. On the socket SO_PEERCRED
names the caller, which the caller never sends and therefore cannot forge. This
server is spawned per agent session as that agent's OS user, and that user is in
`smart-node-agents`, which is exactly who the socket admits.

Transport: stdio, spawned per agent session by the CLI runtime. Registered once per
node by companions/tracking.sh under MCP-server slug `planner`; an agent gets it by
being granted `planner`, which renders it into that agent's .mcp.json.

Usage:
    python mcp_planner.py        # stdio (the only transport)
"""

import asyncio
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import httpx

# The MCP SDK renamed its high-level server between majors and nodes are not all on
# the same one: 2.x ships mcp.server.MCPServer, 1.x shipped mcp.server.fastmcp.FastMCP.
# Both expose the .tool() / .run() surface used here, so bind whichever this node has
# instead of pinning a version.
try:  # SDK 2.x
    from mcp.server import MCPServer as _McpServer
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

CORE_API_SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
# For a test bed with no socket, and nothing else. Deliberately NOT a fallback:
# retrying over TCP answers 403, and a 403 rendered as an empty task list reads to the
# model as "nothing to do", which is precisely how the messaging gap stayed invisible
# for a whole release cycle.
CORE_API_URL = os.environ.get("SN_CORE_API_URL", "")
TIMEOUT = float(os.environ.get("SN_PLANNER_TIMEOUT", "30"))

# core-api owns the address of the planner. This server never learns it: it speaks to
# the proxy, which is what makes every call identity-checked and audited.
BASE_PATH = "/api/planner/api/v1"

mcp = _McpServer("smart-node-planner")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in companions/tracking/tests/test_mcp_planner.py)
# ---------------------------------------------------------------------------

# Priority order used by the next-task picker. Lower index = higher priority.
# This is the ONE place the ordering lives so the console, the agent-facing
# tool and tests all read the same taxonomy.
_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _priority_rank(priority: object) -> int:
    """Return a sortable rank for a priority string. Unknown -> last."""
    if isinstance(priority, str):
        return _PRIORITY_ORDER.get(priority.lower(), len(_PRIORITY_ORDER))
    return len(_PRIORITY_ORDER)


def _primary_sort_key(task: dict) -> tuple:
    """Primary key: priority asc (critical first), eta nulls last asc, due_date nulls last asc."""
    eta = task.get("eta")
    due = task.get("due_date")
    return (
        _priority_rank(task.get("priority")),
        (eta is None, eta or ""),       # nulls last on eta
        (due is None, due or ""),       # nulls last on due_date
    )


def _sort_tasks(tasks: list[dict]) -> list[dict]:
    """Deterministic sort: priority, eta, due_date, then updated_at desc.

    Implemented as a two-pass stable sort because Python has no way to say
    "ascending here, descending there" in one key. Sort by updated_at DESC
    first (so later-touched rows come first within a tie), then stable-sort by
    the primary key, which preserves the updated_at ordering within equal
    primaries.
    """
    out = list(tasks)
    out.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    out.sort(key=_primary_sort_key)
    return out


def _state(task: dict) -> str:
    """The three-valued status of a task: queued, in_progress or closed.

    This server asks the planner for its three statuses (``vocabulary=new``), so
    ``status`` is already one of them; ``state`` carries the same on every
    planner since migration 0010, whatever a reader asked for.
    """
    return task.get("state") or task.get("status") or ""


def _in_review(task: dict) -> bool:
    """A queued task waiting for its reviewer (wait_reason ``review``)."""
    return _state(task) == "queued" and task.get("wait_reason") == "review"


def _is_executor_only_review(task: dict, actor: str) -> bool:
    """True iff ``task`` waits for review and the actor is only the executor.

    "Executor only" means the actor is the assignee but is NOT the reviewer.
    These rows wait on the reviewer, not on the actor, and must not appear in
    the actor's working queue. If the actor fills both roles the task is NOT
    executor-only (it is actionable in the reviewer role).
    """
    if not _in_review(task):
        return False
    reviewer = task.get("reviewer")
    assignee = task.get("assignee")
    return assignee == actor and reviewer != actor


def _dedupe(tasks: Iterable[dict]) -> list[dict]:
    """De-duplicate a task iterable by id, preserving first-seen order."""
    seen: dict[int, dict] = {}
    for t in tasks:
        tid = t.get("id")
        if tid is None or tid in seen:
            continue
        seen[tid] = t
    return list(seen.values())


def _me() -> str:
    """The calling agent's slug, from the runtime -- never from the model."""
    slug = (os.environ.get("SN_AGENT_SLUG") or "").strip()
    if not slug:
        slug = Path.cwd().name
    return slug


async def _call(method: str, path: str, body: Optional[dict] = None,
                params: Optional[dict] = None) -> str:
    # The socket FIRST, and the reason is not preference. SN_CORE_API_URL is set
    # node-wide in /etc/smart-node/core-api.env and the gateway passes its whole
    # environment to the agents it launches, so on a real node the variable is
    # always present. Asking for it first therefore sent every agent over TCP,
    # where core-api refuses an agent identity outright: the kernel cannot prove
    # a caller there. The comments in this file already said the variable is for
    # a test bed with no socket and is "deliberately NOT a fallback" -- this is
    # the code finally saying the same. mcp_secrets.py has always had it this way.
    if os.path.exists(CORE_API_SOCKET):
        transport, base = httpx.AsyncHTTPTransport(uds=CORE_API_SOCKET), "http://localhost"
    elif CORE_API_URL:
        transport, base = None, CORE_API_URL.rstrip("/")
    else:
        # Name the missing door rather than falling through to the address, which
        # would produce a 403 the model reads as an empty queue.
        return json.dumps(
            {"error": "no core-api socket",
             "detail": f"{CORE_API_SOCKET} does not exist, and an agent identity is "
                       f"accepted only there. SN_CORE_API_URL is for a test bed with "
                       f"no socket."},
            ensure_ascii=False,
        )
    # The planner's three statuses on every answer, whatever the node default:
    # core-api passes the query string through to the planner unchanged.
    params = {**(params or {}), "vocabulary": "new"}
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        r = await client.request(
            method,
            f"{base}{BASE_PATH}{path}",
            headers={
                "Remote-User": _me(),
                "Content-Type": "application/json",
                **({"X-SN-Planner-Operator": os.environ["SN_PLANNER_OPERATOR_TOKEN"]}
                   if os.environ.get("SN_PLANNER_OPERATOR_TOKEN") else {}),
            },
            params=params,
            json=body,
        )
        if r.status_code >= 400:
            return json.dumps(
                {"error": f"HTTP {r.status_code}", "detail": r.text[:400]},
                ensure_ascii=False,
            )
        return json.dumps(r.json() if r.content else {}, ensure_ascii=False)


# Rows asked of the planner per request; 500 is the most its listing gives.
_PAGE_SIZE = 500


#: The work queue, in the order it is worked: what you already started, then
#: reviews waiting on you, then new work nobody has taken (queued, no reason).
#: A task queued for any other reason (approval, a person, a date ...) waits on
#: someone else and is not in the queue.
#:
_QUEUE_ORDER = (
    ("in_progress", lambda t: _state(t) == "in_progress"),
    ("review", _in_review),
    ("new", lambda t: _state(t) == "queued" and not t.get("wait_reason")),
)


def _work_queue(tasks: Iterable[dict], actor: Optional[str] = None) -> list[dict]:
    """The tasks of the work queue, bucketed in :data:`_QUEUE_ORDER`.

    Given ``actor``, the queue is what that actor is to START NEXT, not what they may
    see: work in hand and new work are the assignee's. A reviewer who is not the
    assignee has nothing to start on an in_progress task, which is someone else's
    turn in flight, or an orphan that needs a person to name an assignee; handing it
    to them because the reviewer role matches is how an agent wanders into another's
    work. A review waiting on you is yours as the reviewer. Without ``actor`` every
    row the actor holds in either role is listed (planner_my_tasks).
    """
    tasks = list(tasks)
    out: list[dict] = []
    for name, belongs in _QUEUE_ORDER:
        for t in tasks:
            if not belongs(t):
                continue
            if actor is not None and name != "review" and t.get("assignee") != actor:
                continue
            out.append(t)
    return out


async def _fetch_actor_tasks(actor: str, statuses: tuple[str, ...],
                             wait_reason: Optional[str] = None) -> list[dict] | str:
    """Fetch tasks across ``statuses`` where actor is assignee or reviewer.

    Returns a de-duplicated list of task dicts. On the first HTTP failure
    returns the error payload as a JSON string so the caller can propagate it
    verbatim (and the model does not misread an error as an empty queue).
    """
    tasks: dict[int, dict] = {}
    for task_status in statuses:
        fetched = await _fetch_status_tasks(task_status, wait_reason=wait_reason)
        if isinstance(fetched, str):
            return fetched
        for task in fetched:
            if task.get("assignee") == actor or task.get("reviewer") == actor:
                tasks[task["id"]] = task
    return list(tasks.values())


async def _fetch_status_tasks(task_status: str,
                              project_id: Optional[int] = None,
                              wait_reason: Optional[str] = None,
                              close_outcome: Optional[str] = None) -> list[dict] | str:
    """Fetch every task in one status, whoever holds it, page by page.

    The planner has no count endpoint, so the only way to know how many tasks a
    status holds is to read them all. On the first HTTP failure returns the
    error payload as a JSON string, for the same reason as above.
    """
    tasks: list[dict] = []
    offset = 0
    while True:
        params = {"status_filter": task_status, "limit": _PAGE_SIZE, "offset": offset}
        if project_id is not None:
            params["project_id"] = project_id
        if wait_reason:
            params["wait_reason"] = wait_reason
        if close_outcome:
            params["close_outcome"] = close_outcome
        result = await _call("GET", "/tasks", params=params)
        page = json.loads(result)
        if not isinstance(page, list):
            return result
        tasks.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        offset += len(page)
    return tasks


@mcp.tool()
async def planner_my_tasks(status: Optional[str] = None,
                           wait_reason: Optional[str] = None) -> str:
    """Tasks you implement or review on this node.

    A task is queued (not being worked on; wait_reason says why, none means
    nobody has taken it yet), in_progress, or closed (close_outcome says how).

    On a wake call without a status: the queue lists in_progress first, then
    tasks queued for review, then queued tasks nobody has taken, matching your
    assignee OR reviewer role. Resume started work first, read reviews waiting
    on you, then take new work. A task appears once even when you fill both
    roles. Reads never change its assignee, reviewer or status.

    A task in review where you are the executor only (assignee but NOT
    reviewer) waits on someone else and is left out of the default queue. Ask
    for ``status="queued", wait_reason="review"`` to see what you handed off.

    Args:
        status: queued, in_progress or closed. Omit for the default work queue.
        wait_reason: with status queued, only tasks waiting for this reason,
                     e.g. review, approval, person, dependency, schedule, error.
    """
    actor = _me()
    if status is None:
        fetched = await _fetch_actor_tasks(actor, ("in_progress", "queued"))
        if isinstance(fetched, str):
            return fetched
        queue = [t for t in _work_queue(fetched) if not _is_executor_only_review(t, actor)]
        return json.dumps(queue, ensure_ascii=False)
    fetched = await _fetch_actor_tasks(actor, (status.strip(),), wait_reason=wait_reason)
    if isinstance(fetched, str):
        return fetched
    return json.dumps(fetched, ensure_ascii=False)


# How many next-task rows to return by default. Owner-set to 1 so the agent
# picks up one task per wake and finishes it before touching anything else.
# Raise this value (not the per-call arg) if you want to widen the default
# across all callers in one edit.
_NEXT_TASK_DEFAULT_LIMIT = 1


@mcp.tool()
async def planner_next_task(limit: int = _NEXT_TASK_DEFAULT_LIMIT) -> str:
    """The next task(s) to work on, picked deterministically.

    Sort order (first difference wins):
      1. priority: critical > high > medium > low
      2. eta: earlier first; rows without an eta sort last
      3. due_date: earlier first; rows without a due_date sort last
      4. updated_at: most recently touched first

    The caller is taken from SN_AGENT_SLUG as with every tool here. Scope:
    the work queue of planner_my_tasks (in_progress, queued for review, queued
    with nobody on it) where you are the assignee OR the reviewer. A task in
    review where you are only the executor (assignee, not reviewer) waits on
    someone else and is excluded.

    Args:
        limit: how many rows to return, top-of-queue first. Defaults to 1 so
               the agent takes one task per wake; raise it only when you need
               to see the next few at once.

    Returns a JSON array of task dicts in deterministic order, possibly empty.
    """
    actor = _me()
    fetched = await _fetch_actor_tasks(actor, ("in_progress", "queued"))
    if isinstance(fetched, str):
        return fetched
    actionable = [t for t in _work_queue(fetched, actor) if not _is_executor_only_review(t, actor)]
    ordered = _sort_tasks(actionable)
    bound = max(1, int(limit)) if limit else _NEXT_TASK_DEFAULT_LIMIT
    return json.dumps(ordered[:bound], ensure_ascii=False)


# What one row of a listing carries. A full task is mostly its description, and
# one status on a working node holds hundreds of tasks: their full rows do not
# fit in a tool result, and the model then gets an error where the answer should
# be. The text of any row is one planner_task call away.
_LIST_FIELDS = ("id", "title", "status", "wait_reason", "close_outcome", "priority",
                "assignee", "reviewer", "requester", "project_id", "objective_id",
                "updated_at")
_LIST_DEFAULT_LIMIT = 100
_LIST_MAX_LIMIT = 200


def _holds_role(value: object, wanted: Optional[str]) -> bool:
    """Does a task's assignee or reviewer ``value`` match the filter ``wanted``?

    No filter matches every task. The word "none" asks for tasks with nobody in
    the role, which is the question a manager asks most: what has no taker.
    """
    if wanted is None:
        return True
    wanted = wanted.strip()
    if wanted.lower() == "none":
        return not value
    return value == wanted


@mcp.tool()
async def planner_list(status: str, wait_reason: Optional[str] = None,
                       close_outcome: Optional[str] = None,
                       project_id: Optional[int] = None,
                       assignee: Optional[str] = None,
                       reviewer: Optional[str] = None,
                       limit: int = _LIST_DEFAULT_LIMIT, offset: int = 0) -> str:
    """Every task in one status on this node, whoever holds it.

    planner_my_tasks shows your own queue and planner_search finds by text. This
    answers the question neither can: what is waiting in a status, across all
    assignees and reviewers. Reads never change a task.

    Rows are short on purpose: id, title, status, wait_reason, close_outcome,
    priority, assignee, reviewer, requester, project_id, objective_id,
    updated_at. Read planner_task for the text of the ones you need.

    Args:
        status: required: queued, in_progress or closed.
        wait_reason: with queued, only tasks waiting for this reason, e.g.
                     approval, review, owner, person, dependency, schedule, error.
        close_outcome: with closed, only tasks that ended this way, e.g.
                       completed, cancelled, rejected, not_possible, duplicate.
        project_id: only tasks of this project.
        assignee: only tasks assigned to this slug; "none" for tasks nobody took.
        reviewer: only tasks reviewed by this slug; "none" for tasks with no reviewer.
        limit: rows to return, 100 by default, 200 at most.
        offset: rows to skip, to read past the first page.

    Returns a JSON object: ``total`` is how many tasks match, ``tasks`` is the
    page asked for, in the same order as planner_next_task. When ``total`` is
    larger than ``offset`` plus the rows returned, call again with a higher
    offset; do not report the page as the whole.
    """
    fetched = await _fetch_status_tasks(status.strip(), project_id,
                                        wait_reason=wait_reason, close_outcome=close_outcome)
    if isinstance(fetched, str):
        return fetched
    matching = _sort_tasks([
        t for t in fetched
        if _holds_role(t.get("assignee"), assignee)
        and _holds_role(t.get("reviewer"), reviewer)
    ])
    start = max(0, int(offset))
    bound = min(max(1, int(limit)), _LIST_MAX_LIMIT)
    page = [{f: t.get(f) for f in _LIST_FIELDS} for t in matching[start:start + bound]]
    return json.dumps(
        {"total": len(matching), "offset": start, "returned": len(page), "tasks": page},
        ensure_ascii=False,
    )


@mcp.tool()
async def planner_task(task_id: int) -> str:
    """Read one task in full: what was actually asked, and its current state."""
    return await _call("GET", f"/tasks/{task_id}")


# The log of a task, bounded.
#
# A task that several agents pass work through grows a log of tens of kilobytes. The
# runtime refuses a tool answer above its inline limit and parks it in a file, which
# the reader then has to page through by hand; two sessions that could not read the
# log went to another source and reported the wrong state. So this tool never answers
# with the whole log: it answers with an index (who wrote what, when, how long) and
# the newest comments, and anything else is one more call by id. The caps below hold
# whatever the log holds; the answer says what it left out and how to fetch it.
_INDEX_ROWS = 40          # index lines in one answer
_INDEX_HEAD = 100         # characters of each comment shown in the index
_NEWEST_FULL = 3          # newest comments given in full beside the index
_NEWEST_CHARS = 1500      # ... each cut here
_ONE_COMMENT_CHARS = 20000  # one comment read by id is cut here; offset reads on
_LAST_MAX = 10            # most comments `last` returns in full
_LAST_CHARS = 3000        # ... each cut here
_SEARCH_HITS = 20         # most matches one search lists
_SNIPPET_BEFORE = 60
_SNIPPET_AFTER = 100


def _parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def _ago(stamp: str, now: datetime) -> str:
    """How long ago a comment was written, worked out here so that the reader does
    not compare two clock times (the planner stores UTC, the owner lives in local
    time, and a model copying a UTC stamp into an answer got the day wrong).

    "just now" / "7 min ago" under an hour. From an hour on the local clock time is
    added: "5 h ago (13:41)", "1 d ago (yesterday 15:38)", "3 d ago (Oct 18 15:38)".
    """
    when = _parse_time(stamp).astimezone(now.tzinfo)
    secs = max(0, int((now - when).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    days = (now.date() - when.date()).days
    clock = when.strftime("%H:%M")
    if secs < 86400:
        rel = f"{secs // 3600} h ago"
        return f"{rel} ({clock})" if days == 0 else f"{rel} (yesterday {clock})"
    rel = f"{secs // 86400} d ago"
    if days == 1:
        return f"{rel} (yesterday {clock})"
    return f"{rel} ({when.strftime('%b')} {when.day} {clock})"


def _head(text: str, n: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[:n].rstrip() + "..."


_DECISION_HEAD = 240  # a person's decision is shown longer than other lines


def _readable(content: object) -> tuple[str, int]:
    """The text to show for a comment, and the length to cut it to.

    A signed answer from a person arrives as a JSON envelope around their choice and
    their words, with a proof token that makes it kilobytes long. Cut at the usual
    length the person's own words are lost behind the envelope, and those words are
    the instruction the next worker needs. So an answer is shown as the choice and
    the words, longer than an ordinary line.
    """
    text = str(content or "")
    if text.lstrip().startswith('{"answer"'):
        try:
            answer = json.loads(text).get("answer") or {}
            if isinstance(answer, dict) and answer.get("choice"):
                return (f"PERSON'S DECISION {answer['choice']}: {answer.get('comment') or ''}",
                        _DECISION_HEAD)
        except ValueError:
            pass
    return text, _INDEX_HEAD


def _comment_index(comments: list[dict], now: datetime, before_id: Optional[int] = None) -> dict:
    """The bounded view of a log: totals, who wrote how much, one line per recent
    comment, and the newest few in full. Pure, so the caps can be tested."""
    every = sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))
    pool = [c for c in every if before_id is None or c["id"] < before_id]
    shown = pool[-_INDEX_ROWS:]
    older = pool[:-_INDEX_ROWS] if len(pool) > _INDEX_ROWS else []
    out: dict = {
        "now": now.strftime("%Y-%m-%d %H:%M %Z").strip(),
        "comments": len(every),
        "chars": sum(len(c.get("content") or "") for c in every),
        "by_author": dict(Counter(c.get("author") for c in every).most_common()),
    }
    if every:
        out["first"] = _ago(every[0]["created_at"], now)
        out["last"] = _ago(every[-1]["created_at"], now)
    out["index"] = [
        {
            "id": c["id"],
            "by": c.get("author"),
            "when": _ago(c["created_at"], now),
            "chars": len(c.get("content") or ""),
            "head": _head(*_readable(c.get("content"))),
        }
        for c in shown
    ]
    if older:
        out["earlier_not_listed"] = {
            "count": len(older),
            "ids": f"{older[0]['id']}-{older[-1]['id']}",
            "by_author": dict(Counter(c.get("author") for c in older).most_common()),
            "page_back": f"planner_comments(task_id, before_id={shown[0]['id']})",
        }
    if before_id is None and shown:
        newest = []
        for c in shown[-_NEWEST_FULL:]:
            body, _ = _readable(c.get("content"))
            newest.append({
                "id": c["id"], "by": c.get("author"), "when": _ago(c["created_at"], now),
                "text": body[:_NEWEST_CHARS],
                **({"cut": f"{len(body)} chars; read all with comment_id={c['id']}"}
                   if len(body) > _NEWEST_CHARS else {}),
            })
        out["newest_in_full"] = newest
    out["read_one"] = "planner_comments(task_id, comment_id=ID) gives one comment in full"
    return out


def _snippet(text: str, needle: str) -> str:
    flat = " ".join(str(text or "").split())
    at = flat.lower().find(needle.lower())
    if at < 0:
        return _head(flat, _SNIPPET_BEFORE + _SNIPPET_AFTER)
    start = max(0, at - _SNIPPET_BEFORE)
    piece = flat[start:at + len(needle) + _SNIPPET_AFTER]
    return ("..." if start else "") + piece + ("..." if start + len(piece) < len(flat) else "")


@mcp.tool()
async def planner_comments(task_id: int, comment_id: Optional[int] = None,
                           before_id: Optional[int] = None, q: Optional[str] = None,
                           last: Optional[int] = None, offset: int = 0) -> str:
    """Read the log of a task. Everything anyone did to it is written here.

    With only task_id you get an INDEX, never the whole log: how many comments, who
    wrote how many, and one line for each of the latest 40 (id, author, how long ago,
    length, first words), plus the newest three in full. The log of a busy task is far
    longer than any answer can be, so what is not listed says so and says how to get it.

    Read before you act on a task: the index shows in seconds what was decided and by
    whom. Open the comments that matter with comment_id. If something you need is not
    shown, fetch it (before_id pages back, q searches inside this task, last gives the
    newest few in full); do not stand in for a log you could not read with another
    source, such as a document that happens to be in a folder.

    comment_id  one comment in full (offset reads on if it is longer than 20000 chars)
    before_id   the index of the comments older than this id
    q           the comments containing this text, with a snippet around it
    last        the newest N comments in full, at most 10, each cut at 3000 chars
    """
    base = f"/tasks/{task_id}/comments"
    now = datetime.now().astimezone()
    if comment_id is not None:
        raw = await _call("GET", base, params={"comment_id": int(comment_id)})
        data = json.loads(raw)
        if isinstance(data, dict):  # an error from the planner or the socket
            return raw
        if not data:
            return json.dumps({"error": f"comment {comment_id} is not on task {task_id}"},
                              ensure_ascii=False)
        c = data[0]
        body = str(c.get("content") or "")
        start = max(0, int(offset))
        piece = body[start:start + _ONE_COMMENT_CHARS]
        out = {"id": c["id"], "by": c.get("author"), "when": _ago(c["created_at"], now),
               "chars": len(body), "text": piece}
        if start + len(piece) < len(body):
            out["more"] = f"planner_comments(task_id, comment_id={c['id']}, offset={start + len(piece)})"
        return json.dumps(out, ensure_ascii=False)
    if q is not None and q.strip():
        raw = await _call("GET", base, params={"q": q.strip()})
        data = json.loads(raw)
        if isinstance(data, dict):
            return raw
        hits = list(reversed(data))
        return json.dumps({
            "query": q.strip(), "hits": len(hits), "listed": min(len(hits), _SEARCH_HITS),
            "matches": [
                {"id": c["id"], "by": c.get("author"), "when": _ago(c["created_at"], now),
                 "chars": len(c.get("content") or ""), "snippet": _snippet(c.get("content"), q.strip())}
                for c in hits[:_SEARCH_HITS]
            ],
        }, ensure_ascii=False)
    if last is not None:
        n = max(1, min(int(last), _LAST_MAX))
        raw = await _call("GET", base, params={"last": n})
        data = json.loads(raw)
        if isinstance(data, dict):
            return raw
        return json.dumps([
            {"id": c["id"], "by": c.get("author"), "when": _ago(c["created_at"], now),
             "chars": len(c.get("content") or ""), "text": str(c.get("content") or "")[:_LAST_CHARS],
             **({"cut": f"read all with comment_id={c['id']}"} if len(c.get("content") or "") > _LAST_CHARS else {})}
            for c in data
        ], ensure_ascii=False)
    raw = await _call("GET", base)
    data = json.loads(raw)
    if isinstance(data, dict):
        return raw
    return json.dumps(_comment_index(data, now, before_id), ensure_ascii=False)


@mcp.tool()
async def planner_comment(task_id: int, content: str) -> str:
    """Write what you did, found, decided or rejected onto the task, as you go.

    This is the rule on this node, not a courtesy: work that is not in the task did
    not happen as far as the next reader can tell, and the next session will either
    redo it or act on a stale premise. Comment each real decision, including the ones
    that changed your mind, not one summary at the end.
    """
    return await _call("POST", f"/tasks/{task_id}/comments", {"content": content})


@mcp.tool()
async def planner_advance(task_id: int, status: str,
                          wait_reason: Optional[str] = None,
                          close_outcome: Optional[str] = None,
                          comment: Optional[str] = None) -> str:
    """Move a task: start it, stop it, hand it to review, or close it.

    A task has one of three statuses:

        queued       not being worked on; wait_reason says why, none means it
                     waits for someone to take it
        in_progress  being worked on
        closed       finished; close_outcome says how

    Wait reasons this node ships with: approval (an approver decides), review
    (the reviewer checks it), owner (the owner's call), person (a named person
    must act), dependency (another task first), schedule (a date), error (a
    fault stopped it). Close outcomes: completed, duplicate, and the ones that
    are a person's call, not an agent's: cancelled, rejected, not_possible.
    The node may add its own rows; a word the planner does not know is refused.

        start work          status="in_progress"
        hand to review      status="queued", wait_reason="review"
        stop for a person   status="queued", wait_reason="person", comment=who and why
        finish              status="closed", close_outcome="completed", comment=report
        a duplicate         status="closed", close_outcome="duplicate", comment=the other id

    There is no transition table: any status may follow any other. What the
    planner refuses, and it says why:

      - closing without a comment. For completed the comment is the report: quote
        the evidence for each acceptance item.
      - closing as completed when the task has a reviewer and you are not it. A
        fix you made yourself goes to review: the confirmer is never the finder.
        A task closed by the wrong person goes back with status="queued",
        wait_reason="review".
      - a stop for owner, person, dependency, schedule or error without a
        comment that says why.
      - an agent closing as cancelled, rejected or not_possible: stop it for a
        person instead.
    """
    body: dict = {"status": status}
    if wait_reason is not None:
        body["wait_reason"] = wait_reason
    if close_outcome is not None:
        body["close_outcome"] = close_outcome
    if comment is not None:
        body["comment"] = comment
    return await _call("PATCH", f"/tasks/{task_id}", body)


@mcp.tool()
async def planner_file(title: str, description: str,
                       assignee: Optional[str] = None,
                       priority: Optional[str] = None,
                       project_id: Optional[int] = None,
                       workflow_id: Optional[int] = None,
                       recur_interval: Optional[str] = None,
                       next_run: Optional[str] = None) -> str:
    """File a task: work that is not yours to do now, or that a colleague must pick up.

    Search before you file. A duplicate splits the record in two and both halves rot.

    Args:
        title: one line naming the work, in the language the owner used.
        description: what was actually asked, in full, including the context the doer
                     will not otherwise have. Write it for someone with no memory of
                     this conversation.
        assignee: agent slug to do it. A task you file waits for the owner's approval
                  and is not assigned until then (you cannot hand work to a
                  colleague on your own authority); the slug you give is kept as the
                  proposed doer, and the owner's approval hands the task to that
                  agent, who is then woken. Omit if you do not know who should do it.
                  The one exception is a repeating task you set for yourself (see
                  recur_interval).
        priority: low | medium | high | critical. Default medium.
        project_id: the project this work belongs to. Omit only for work that
                    belongs to no project. A task filed without one is invisible
                    to anything that selects by project -- including the release
                    gate, which refuses to publish while a task in the release
                    project is unreviewed. Use planner_search or ask the
                    operator if you do not know the id.
        workflow_id: bind the task to a flow, by the flow's catalog number
                     (sn_core.workflows.id). Once the task is approved the
                     Runner starts that flow on it, and from then on the flow
                     owns the task's status. This is how a flow is started
                     from a chat. It starts real work: bind a task only when
                     that is what was asked. Omit for ordinary work.
        recur_interval: make it a recurring task. A task that comes back on a schedule is how
                     this node does a reminder, a daily check or a weekly report: one
                     task carries the whole series and each cycle is closed with a
                     report. How often: daily, weekly, monthly, yearly, or interval text
                     such as "2 weeks". Omit for a one-off task. For a reminder to a
                     person, assign it to them (or leave it for the owner). A reminder
                     or an alarm for YOURSELF (assignee = your own name, with
                     recur_interval) starts at once, no approval: it wakes only you. It
                     repeats at most every 15 minutes and you may have three open at a
                     time; end one with planner_recur when it has done its job. Any other
                     repeating task you file waits for the owner's approval like any other
                     and its cycles are skipped until the owner approves it, once, for the
                     whole series. This is for work and reminders; a command that must run
                     on a timer is a cron, not a task. Change or end a series with
                     planner_recur.
        next_run: when the first cycle opens, an ISO time with its offset
                  (2026-10-26T07:00:00-05:00). Only with recur_interval; without it
                  the first cycle opens one interval from now.
    """
    payload: dict = {"title": title, "description": description}
    if assignee:
        payload["assignee"] = assignee
    if priority:
        payload["priority"] = priority
    # Without this the field could not be set from an agent at all: the tool did
    # not expose it and the HTTP endpoint refuses agent identity over TCP, so
    # every task an agent filed landed with project_id null. project-manager hit it on the
    # 1.12.24 dispatch and four tasks had to be repaired by hand.
    if project_id is not None:
        payload["project_id"] = project_id
    # A task that carries a workflow_id is started by the Runner as that flow. The
    # field could not be set from an agent at all, so no agent could start a flow
    # without a shell write the node refuses.
    if workflow_id is not None:
        payload["workflow_id"] = workflow_id
    if recur_interval:
        payload["recur_interval"] = recur_interval
    if next_run:
        payload["next_run"] = next_run
    return await _call("POST", "/tasks", payload)


@mcp.tool()
async def planner_recur(task_id: int, recur_interval: Optional[str] = None,
                        next_run: Optional[str] = None) -> str:
    """Make a task recur, change how it recurs, or end the series.

    A task that comes back on a schedule is how this node does a reminder, a daily check, a
    weekly report: one task carries the whole series, each cycle is closed with a report and
    the reports are the record. recur_interval is how often (daily, weekly, monthly, yearly,
    or Postgres interval text such as "2 weeks" or "1 month"); next_run is when the first
    cycle opens (an ISO time with its offset, for example 2026-10-26T07:00:00-05:00), and
    without it the first cycle opens one interval from now.

    When a cycle comes due the node (every five minutes) does this to the task:
      - closed as completed: reopens it to in_progress and wakes the assignee, with a
        comment "Recurring cycle ... opened"; the owner's approval carries over;
      - still open from the last cycle: comments that the next one came due and wakes
        the assignee again (a person is told "Recurring task due");
      - closed any other way (cancelled, rejected ...): the series ends;
      - waiting for the owner's approval: nobody is woken and the cycle is skipped.
    Then next_run moves on by whole intervals, so downtime opens one cycle, not several.

    Args:
        task_id: the task.
        recur_interval: the new interval, or "off" to end the series (it clears next_run
                        too). A new interval on a task that already recurs keeps its
                        scheduled next_run.
        next_run: move the next cycle. Only on a task that recurs.

    A one-off reminder: file it with recur_interval and next_run at the time you want, and
    when the cycle opens and is dealt with, end the series with recur_interval="off".
    """
    body: dict = {}
    if recur_interval is not None:
        body["recur_interval"] = recur_interval
    if next_run is not None:
        body["next_run"] = next_run
    if not body:
        return json.dumps({"error": "give recur_interval, next_run or both"})
    return await _call("PATCH", f"/tasks/{task_id}", body)


@mcp.tool()
async def planner_search(query: str, author: str | None = None, limit: int = 20) -> str:
    """Find tasks by words, across assignees and every status.

    Type the words you remember, in any order: every word must occur somewhere in
    the task (title, description or a comment). `author` keeps only the tasks that
    name wrote a comment on or filed.

    Use it BEFORE filing. A duplicate splits the record in two and both halves rot.

    Note this is a different endpoint from the listing: search has its own path and
    takes no status filter, so it sees closed tasks too. That is what you want when
    checking whether something was already dealt with.
    """
    params = {"q": query, "limit": limit}
    if author:
        params["author"] = author
    return await _call("GET", "/tasks/search", params=params)


@mcp.tool()
async def planner_objective_create(title: str, description: str,
                                   project_id: int) -> str:
    """Create a release objective.

    project_id is required. An objective without a project cannot be tracked
    by the release gate and the call is rejected before reaching
    the API.

    Returns the new objective id and title.

    Args:
        title: short name for the objective, e.g. "1.12.25 release scope".
        description: what this objective covers and why it exists.
        project_id: the project this objective belongs to. Required.
    """
    payload = {
        "title": title,
        "description": description,
        "project_id": project_id,
        "type": "milestone",  # all agent-created objectives are release milestones
    }
    return await _call("POST", "/objectives", payload)


@mcp.tool()
async def planner_objective_attach(objective_id: int, task_id: int) -> str:
    """Add an existing task to an objective.

    Verifies both the task and the objective exist before attaching. Returns
    a confirmation with the task title and objective title.

    Args:
        objective_id: the objective to attach to.
        task_id: the task to attach.
    """
    task_raw, obj_raw = await asyncio.gather(
        _call("GET", f"/tasks/{task_id}"),
        _call("GET", f"/objectives/{objective_id}"),
    )
    task = json.loads(task_raw)
    if "error" in task:
        return json.dumps(
            {"error": f"task {task_id} not found", "detail": task.get("detail", "")},
            ensure_ascii=False,
        )
    obj = json.loads(obj_raw)
    if "error" in obj:
        return json.dumps(
            {"error": f"objective {objective_id} not found",
             "detail": obj.get("detail", "")},
            ensure_ascii=False,
        )
    result_raw = await _call("PATCH", f"/tasks/{task_id}", {"objective_id": objective_id})
    result = json.loads(result_raw)
    if "error" in result:
        return result_raw
    return json.dumps(
        {
            "ok": True,
            "task_id": task_id,
            "task_title": task.get("title", ""),
            "objective_id": objective_id,
            "objective_title": obj.get("title", ""),
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def planner_objective_detach(objective_id: int, task_id: int) -> str:
    """Remove a task from an objective.

    Verifies the task is currently a member of this objective before detaching,
    so the call cannot silently move a task that belongs to a different objective.

    Args:
        objective_id: the objective to detach from.
        task_id: the task to remove.
    """
    task_raw = await _call("GET", f"/tasks/{task_id}")
    task = json.loads(task_raw)
    if "error" in task:
        return json.dumps(
            {"error": f"task {task_id} not found", "detail": task.get("detail", "")},
            ensure_ascii=False,
        )
    if task.get("objective_id") != objective_id:
        return json.dumps(
            {
                "error": "task is not a member of this objective",
                "task_id": task_id,
                "task_objective_id": task.get("objective_id"),
                "requested_objective_id": objective_id,
            },
            ensure_ascii=False,
        )
    result_raw = await _call("PATCH", f"/tasks/{task_id}", {"objective_id": None})
    result = json.loads(result_raw)
    if "error" in result:
        return result_raw
    return json.dumps(
        {
            "ok": True,
            "task_id": task_id,
            "task_title": task.get("title", ""),
            "detached_from_objective_id": objective_id,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def planner_objective_get(objective_id: int) -> str:
    """Read an objective: its title, progress counts, and full member task list.

    Returns both the progress summary and the complete member task list in a
    single call. An objective with zero members returns counts of zero, not
    an error.

    Args:
        objective_id: the objective to read.
    """
    obj_raw, progress_raw, tasks_raw = await asyncio.gather(
        _call("GET", f"/objectives/{objective_id}"),
        _call("GET", f"/objectives/{objective_id}/progress"),
        _call("GET", "/tasks", params={"objective_id": objective_id}),
    )
    obj = json.loads(obj_raw)
    if "error" in obj:
        return obj_raw
    progress = json.loads(progress_raw)
    tasks_data = json.loads(tasks_raw)

    progress = progress if isinstance(progress, dict) else {}
    by_state: dict = progress.get("tasks_by_state") or {}
    queued_by_reason: dict = progress.get("queued_by_reason") or {}
    closed_by_outcome: dict = progress.get("closed_by_outcome") or {}
    total = progress.get("total_tasks", 0)

    task_list = tasks_data if isinstance(tasks_data, list) else []

    result: dict = {
        "objective_id": objective_id,
        "title": obj.get("title", ""),
        "description": obj.get("description", ""),
        "project_id": obj.get("project_id"),
        "status": obj.get("status", ""),
        "progress": {
            "total": total,
            "completed": closed_by_outcome.get("completed", 0),
            "by_status": by_state,
            "queued_by_reason": queued_by_reason,
            "closed_by_outcome": closed_by_outcome,
        },
        "tasks": [
            {
                "id": t.get("id"),
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "wait_reason": t.get("wait_reason"),
                "close_outcome": t.get("close_outcome"),
                "priority": t.get("priority", ""),
                "assignee": t.get("assignee", ""),
            }
            for t in task_list
        ],
    }
    if isinstance(tasks_data, dict) and "error" in tasks_data:
        result["tasks_error"] = tasks_data
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def planner_objective_close(objective_id: int, force: bool = False) -> str:
    """Close an objective by setting its status to completed.

    Refuses if any member task is not closed, unless force=True is passed. With
    force=False (the default) and unfinished tasks present, the call returns
    their list and does nothing: read it, confirm the tasks are genuinely done
    or moved, then call again with force=True.

    Args:
        objective_id: the objective to close.
        force: if True, close even when member tasks are unfinished.
               Use only after confirming those tasks are handled.
    """
    obj_raw, tasks_raw = await asyncio.gather(
        _call("GET", f"/objectives/{objective_id}"),
        _call("GET", "/tasks", params={"objective_id": objective_id}),
    )
    obj = json.loads(obj_raw)
    if "error" in obj:
        return obj_raw

    tasks_data = json.loads(tasks_raw)
    task_list = tasks_data if isinstance(tasks_data, list) else []

    unfinished = [
        {
            "id": t.get("id"),
            "title": t.get("title", ""),
            "status": t.get("status", ""),
            "wait_reason": t.get("wait_reason"),
            "assignee": t.get("assignee", ""),
        }
        for t in task_list
        if _state(t) != "closed"
    ]

    if unfinished and not force:
        return json.dumps(
            {
                "error": (
                    f"{len(unfinished)} member task(s) are not finished; "
                    "pass force=True to close anyway (only after confirming "
                    "those tasks are handled)"
                ),
                "objective_id": objective_id,
                "objective_title": obj.get("title", ""),
                "unfinished_tasks": unfinished,
            },
            ensure_ascii=False,
        )

    result_raw = await _call(
        "PATCH", f"/objectives/{objective_id}", {"status": "completed"}
    )
    result = json.loads(result_raw)
    if "error" in result:
        return result_raw

    return json.dumps(
        {
            "closed": True,
            "objective_id": objective_id,
            "objective_title": result.get("title", ""),
            "status": result.get("status", ""),
            "forced_with_unfinished": len(unfinished) if (unfinished and force) else 0,
        },
        ensure_ascii=False,
    )


@mcp.tool()
async def planner_tag(task_id: int, tag: str) -> str:
    """Record a tag on a task.

    The most common use is a reviewer recording their own verdict:
        planner_tag(task_id=4130, tag="reviewer:architect")

    The endpoint takes one tag per call. Plural is not supported by the API;
    call this once per tag rather than trying to batch them.

    Returns {} on success (the server returns 204 No Content).
    """
    return await _call("POST", f"/tasks/{task_id}/tags", {"tag": tag})


@mcp.tool()
async def planner_tags(task_id: int) -> str:
    """List all tags on a task. Returns a JSON array of strings."""
    return await _call("GET", f"/tasks/{task_id}/tags")


@mcp.tool()
async def planner_untag(task_id: int, tag: str) -> str:
    """Remove a tag from a task. Returns {} on success (the server returns 204)."""
    return await _call("DELETE", f"/tasks/{task_id}/tags/{tag}")


if __name__ == "__main__":
    mcp.run()
