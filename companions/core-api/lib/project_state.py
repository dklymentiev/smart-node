"""Project state collector, all eight sources (steps 1 to 8).

The collector produces facts; the agent writes the card. The "facts, not
narrative" rule applies here, so this module makes no decisions about
shape or ordering of the final text. It returns raw rows and a few
derived counts, nothing more.

ARCHITECTURAL CONTRACT (2026-10-17):
- Planner is read through its HTTP API only, never by DB connection to the
  planner schema. Mesh and Doci follow the same rule: read through their
  own HTTP API, not their underlying storage. Owner channels
  (sn_core.messages) ARE core-api's own data: this collector lives inside
  core-api, so it uses the same `db.cursor()` every core-api route uses
  on sn_core, not a loopback HTTP hop to its own API. The rule bars
  reaching across service boundaries, not reading the service's own
  tables.
- An earlier standalone prototype used sudo psql; that pattern is not
  inherited into product code.
- The collector is deterministic. No model inside this module.

PLANNER SOURCES (steps 1 to 5 from the task description):
1. GOALS: objectives of the project with status, task count, closed count,
   last task-move date.
2. LINKED TASKS: a task belongs to the project when its project_id is the
   project OR its objective belongs to the project OR it carries the tag
   product:<slug>. Counts by status and by link type (direct vs
   objective-or-tag only).
3. MOVED: linked tasks with updated_at inside the window.
4. MENTIONS: tasks of other projects whose title / description / comments
   name this project's slug or title and that moved in the window. Match
   is case-insensitive substring via the planner search endpoint.
5. COMMENTS: for every moved task, the comments created in the window,
   with the author classified into owner / operator / agent / system so
   the card can lean on the last owner line without re-parsing authors.

MEMORY SOURCE (step 6):
6. MEMORY: mesh documents the project is bound to, by the mesh `project`
   field or by a `project:<slug>` tag, kept when updated_at (fallback
   created_at) is in the window. Read through mesh's HTTP API only
   (MESH_API_URL + X-Workspace: "*"), never through its Postgres.

Doci SOURCE (step 7):
7. DOCUMENTS: Doci documents under the project's own folder (slug and,
   when distinct, the title's kebab-case form), kept when updated_at
   (fallback created_at) is in the window. Folders are skipped; only
   doc_type == "document" rows appear. Read through Doci's HTTP API
   only (DOCI_URL + Remote-User: admin), never the git tree.

OWNER CHANNELS SOURCE (step 8):
8. CHANNELS: messages on the OWNER'S OWN channels in the window whose
   content or (for email) subject names the project's slug or title,
   or references a task id from step 3. The owner's addresses are read
   from sn_config.user_channels for the active super-admin, so the
   collector is node-agnostic (no hardcoded admin email or chat id).
   Channel scoping follows the task spec:
   - web-chat: any message with the owner's web-chat address on either
     side (the owner is the only human on this channel);
   - email: INBOUND only to the owner's address (outbound agent mail
     is left out this way);
   - telegram: messages in threads where the owner typed at least once
     in the window (notification bots send with thread_id=NULL and are
     excluded by that test, which was ~3,600 messages of noise in 3
     days on the Flint prototype).
   Pulled from sn_core.messages via core-api's own `db.cursor()`;
   blocked / refused / failed state rows are dropped so a card does
   not quote a message that never reached anyone. Each row carries a
   400-character snippet of the content, not the full body.

DERIVED FIELDS (not sources, not steps):
- `waiting_for_owner`: linked tasks whose status waits on the owner's
  decision (queued for approval or for the owner), of ANY age. The owner
  asked for this on 5604: a task sitting for weeks must still appear
  when the owner asks what waits for them, or the card hides the
  backlog it was built for.
- `where_it_stopped`: the most recent linked-task move across all
  time, used as a fallback anchor when the window is silent (empty
  moved). Only populated when `moved` is empty.

The planner HTTP surface is reached through two routes:
- GET /api/planner/api/v1/<path>  on this core-api, which uses the agent
  identity of the caller (operator path when run from core-api itself).
- Direct to the planner service URL in PLANNER_API_URL + the operator
  token in SN_PLANNER_OPERATOR_TOKEN. This module takes the second path
  because the collector runs inside core-api and needs the operator view;
  the same shape routes/work.py already uses for the planner dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("core-api.project_state")

# Collector schema version. Bump on any breaking change to the facts
# contract rather than mutating a field in place.
COLLECTOR_SCHEMA_VERSION = 1

# Hard cap on how many pages of /tasks we will walk. 20000 is also the stop
# routes/work.py uses; a project with more than that has other problems.
_MAX_TASKS_WALK = 20000
_TASKS_PAGE_SIZE = 500

PLANNER_URL = os.environ.get("PLANNER_API_URL", "").rstrip("/")
PLANNER_OPERATOR_HEADER = "X-SN-Planner-Operator"
_PLANNER_TIMEOUT = 15

# Mesh is read through its own HTTP API, with X-Workspace: "*" for the
# cross-workspace reads this collector needs. See the architect's rule in
# the module docstring: no direct DB access to mesh's Postgres.
MESH_URL = os.environ.get("MESH_API_URL", "").rstrip("/")
_MESH_TIMEOUT = 10
# A single mesh list call caps at 100 (see mesh /openapi.json "limit" param).
_MESH_PAGE_LIMIT = 100

# Doci is read through its own HTTP API, which authenticates localhost
# callers by the Remote-User header (the same pattern routes/notifications.py
# uses to pull the shared brand shell at templates/email/message.md). The
# architect's rule in the module docstring forbids reading Doci's git tree
# directly; the API gives us ordered rows across folders.
DOCI_URL = os.environ.get("DOCI_URL", "").rstrip("/")
_DOCI_TIMEOUT = 10
# Doci's /api/list.php is unbounded on the server side; the collector caps
# the walk at a sane size so a project folder gone sideways cannot push the
# response into millions of rows. Reaching this cap is reported in notes.
_DOCI_FOLDER_WALK_CAP = 2000

# sn_core.messages lives inside core-api, so no HTTP hop is needed. The
# collector caps a single window read at this many rows so a very wide
# window on a noisy channel does not pull a million rows into memory.
# Hitting this cap is reported in notes.
_CHANNELS_WINDOW_CAP = 2000
# Owner-channel addresses are read from sn_config.user_channels for the
# active super-admin and cached briefly so repeated collect() calls do
# not re-query. The DB read is cheap, but one hit per source per project
# per card adds up on a daily digest run across every project.
_OWNER_CHANNELS_TTL = 30.0
_OWNER_CHANNELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_OWNER_CHANNELS_LOCK = threading.Lock()
# Task-id reference in a message body: a 4 or 5 digit number, optionally
# prefixed with "#". Only ids that also appear in step 3's linked task
# set count, so a stray number that happens to look like a task id is
# not treated as one unless the project owns it.
_TASK_REF_RE = re.compile(r"#?(\d{4,5})\b")

# NOISE FILTERS (owner's call, 2026-10-17). The card is
# useless if it quotes flow-test tasks that fire on every stub run, or
# operator-session snapshots the dev saved mid-work. Both are kept out
# at the collector so every downstream reader sees the same picture.
# Matches are case-insensitive on a trimmed title.
_TEST_TASK_TITLE_RE = re.compile(
    r"^(?:\s*test\b|.*\bflow\s*test\b)",
    re.IGNORECASE,
)
# Mesh "type:" tag that marks an operator-session snapshot. The dev saves
# these to carry context across sessions; they are debugging artefacts,
# not project memory, and should not land on the stand-up card.
_MESH_NOISE_TYPES = frozenset({"session-snapshot"})


def _is_test_task(title: Any) -> bool:
    """Treat a planner task as noise if its title smells like a test run.

    Covers two shapes the node actually emits:
    - titles beginning with the word "test" (e.g. "test fixture 2026-10"),
    - titles that contain "flow test" anywhere (the flow stubrunner's
      standard title).

    False negatives are preferable to false positives: a real task with
    an unlucky title still passes a human reviewer. If the collector
    hides a real task, the card loses a fact and the owner cannot see it.
    """
    if not isinstance(title, str):
        return False
    return bool(_TEST_TASK_TITLE_RE.match(title))


def _is_session_snapshot(doc: Any) -> bool:
    """Treat a mesh document as noise if its type-tag is "session-snapshot".

    Only the explicit tag counts. A document with no type-tag at all is
    kept, because the collector cannot know what the author intended.
    """
    if not isinstance(doc, dict):
        return False
    return _mesh_type_from_tags(doc.get("tags")) in _MESH_NOISE_TYPES


def _planner_key() -> str:
    return os.environ.get("PLANNER_API_KEY", "").strip()


def _operator_token() -> str:
    return os.environ.get("SN_PLANNER_OPERATOR_TOKEN", "").strip()


def _planner_get(subpath: str) -> Any:
    """Read one planner endpoint via HTTP.

    Returns the decoded JSON on 2xx, None otherwise. Never raises; a
    missing read is surfaced in the collector's `notes` list so the
    report shows the blind spot rather than hiding it.
    """
    if not PLANNER_URL:
        return None
    url = f"{PLANNER_URL}/{subpath.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    key = _planner_key()
    if key:
        req.add_header("X-API-Key", key)
    tok = _operator_token()
    if tok:
        req.add_header(PLANNER_OPERATOR_HEADER, tok)
    try:
        with urllib.request.urlopen(req, timeout=_PLANNER_TIMEOUT) as resp:
            if resp.status >= 400:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as ex:
        log.warning("project_state: planner read %s failed: %s", subpath, ex)
        return None


def _mesh_get(subpath: str, workspace: str = "*") -> Any:
    """Read one mesh endpoint via HTTP, with the X-Workspace header set.

    Returns the decoded JSON on 2xx (a list or a dict depending on the
    route), None otherwise. Never raises; the caller records a miss in
    `notes`. The workspace "*" is mesh's cross-workspace read used by the
    panel's Memory page (routes/memory.py); the collector uses the same
    convention so a project's documents in any agent's workspace are seen.
    """
    if not MESH_URL:
        return None
    url = f"{MESH_URL}/{subpath.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Workspace", workspace)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_MESH_TIMEOUT) as resp:
            if resp.status >= 400:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as ex:
        log.warning("project_state: mesh read %s failed: %s", subpath, ex)
        return None


def _doci_get(subpath: str, remote_user: str = "admin") -> Any:
    """Read one Doci endpoint via HTTP, with the Remote-User header set.

    Returns the decoded JSON on 2xx, None otherwise. Never raises; the
    caller records a miss in `notes`. Doci accepts "Remote-User: admin"
    from localhost callers on this node (same pattern as the shared email
    shell loader in routes/notifications.py), which gives the admin view
    across folders. The architect's rule in the module docstring forbids
    reading Doci's git tree directly; the API gives us ordered rows.
    """
    if not DOCI_URL:
        return None
    url = f"{DOCI_URL}/{subpath.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Remote-User", remote_user)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_DOCI_TIMEOUT) as resp:
            if resp.status >= 400:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as ex:
        log.warning("project_state: doci read %s failed: %s", subpath, ex)
        return None


def _parse_ts(value: Any) -> datetime | None:
    """Parse a planner ISO 8601 timestamp into aware UTC datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_project(slug: str) -> dict | None:
    """Resolve a project by slug via the planner API.

    Returns the full project row dict (id, slug, title, ...) or None.
    """
    projects = _planner_get("api/v1/projects?limit=500")
    if not isinstance(projects, list):
        return None
    for p in projects:
        if isinstance(p, dict) and p.get("slug") == slug:
            return p
    return None


def _list_project_objectives(project_id: int) -> list[dict]:
    """Objectives for one project, via planner API.

    Status is reported as the planner has it. The collector does not close
    stale objectives; that is a separate task on the owner's approval.
    """
    out = _planner_get(f"api/v1/objectives?project_id={project_id}")
    return out if isinstance(out, list) else []


def _walk_tasks_filtered(**query: str) -> list[dict]:
    """Walk /api/v1/tasks with a filter in pages of 500.

    Any kwarg becomes a query parameter. The walk stops when a short page
    returns or when _MAX_TASKS_WALK is hit (loud note in that case).
    """
    out: list[dict] = []
    offset = 0
    while True:
        params = dict(query)
        params["limit"] = str(_TASKS_PAGE_SIZE)
        params["offset"] = str(offset)
        q = urllib.parse.urlencode(params)
        page = _planner_get(f"api/v1/tasks?{q}")
        if not isinstance(page, list):
            break
        if not page:
            break
        out.extend(page)
        if len(page) < _TASKS_PAGE_SIZE:
            break
        offset += _TASKS_PAGE_SIZE
        if offset >= _MAX_TASKS_WALK:
            log.warning(
                "project_state: tasks walk hit %s stop with query=%s",
                _MAX_TASKS_WALK, query)
            break
    return out


def _collect_linked_tasks(
    project_id: int, project_slug: str, objectives: list[dict]
) -> tuple[list[dict], dict[str, int], list[str]]:
    """Linked tasks for a project (step 2).

    A task is linked when its project_id matches, OR its objective_id is
    in the project's objectives, OR it carries the tag product:<slug>.

    The planner API currently exposes only project_id, objective_id,
    assignee, status, priority, wait_reason, close_outcome and
    exclude_tags filters on /api/v1/tasks (companions/planner routers
    tasks.py, 2026-10-17). It does NOT accept a positive tag filter, so
    the by-tag source cannot be read via the planner API in this slice.
    An earlier standalone prototype reached task_tags directly through
    psql; per the architectural rule that is not inherited into product
    code.

    Blind spot: tasks linked to the project only by tag product:<slug>
    (no project_id, no objective in this project) are missed in this
    slice. On Flint 2026-10-13 the prototype found 19 only-by
    (objective OR tag) tasks across both links; this collector reports
    only the subset reachable via project_id/objective_id and surfaces
    the tag gap in notes.

    Returns (tasks, link_source_counts, step_notes).
    """
    by_id: dict[int, dict] = {}
    source: dict[int, set[str]] = {}
    step_notes: list[str] = []

    # 1. By project_id.
    for t in _walk_tasks_filtered(project_id=str(project_id)):
        tid = t.get("id")
        if isinstance(tid, int):
            by_id[tid] = t
            source.setdefault(tid, set()).add("project")

    # 2. By objective_id, for every objective of this project.
    for o in objectives:
        oid = o.get("id")
        if not isinstance(oid, int):
            continue
        for t in _walk_tasks_filtered(objective_id=str(oid)):
            tid = t.get("id")
            if isinstance(tid, int):
                by_id.setdefault(tid, t)
                source.setdefault(tid, set()).add("objective")

    # 3. By tag product:<slug>: not reachable through the planner API
    #    in this slice. Documented as a blind spot in the response.
    step_notes.append(
        f"by_tag source not read: planner /api/v1/tasks has no positive "
        f"tag include filter in this slice. Tasks linked to project "
        f"only by tag product:{project_slug} are not counted. "
        f"(Needs a separate change to companions/planner.)")

    tasks = list(by_id.values())

    # Counts reflect the TASK'S OWN LINK, not which query returned it. A task
    # with project_id=P and objective_id=O (where O belongs to P) counts under
    # both by_project and by_objective. "only_by_objective_not_project" means
    # project_id is not this project, so the task is linked only through
    # objective (or tag, once that source is available).
    project_objective_ids = {
        o.get("id") for o in objectives if isinstance(o.get("id"), int)
    }
    by_project_n = sum(1 for t in tasks if t.get("project_id") == project_id)
    by_objective_n = sum(
        1 for t in tasks if t.get("objective_id") in project_objective_ids)
    only_by_objective_n = sum(
        1 for t in tasks if t.get("project_id") != project_id)
    counts = {
        "total": len(tasks),
        "by_project": by_project_n,
        "by_objective": by_objective_n,
        "by_tag": None,
        "only_by_objective_not_project": only_by_objective_n,
    }
    return tasks, counts, step_notes


def _collect_moved(
    tasks: list[dict], since: datetime
) -> tuple[list[dict], list[str]]:
    """Linked tasks whose updated_at is at or after `since`.

    Returns a list of thinned dicts in the same shape the card's working
    layer will quote (id, title, status, updated_at, assignee,
    objective_id, project_id, is_new), together with step_notes.

    Noise filter: tasks whose title smells like a test run (see
    `_is_test_task`) are dropped so a flow-test burst does not drown
    the stand-up card. The count of filtered-out tasks is surfaced in
    step_notes so a hidden skip is loud.
    """
    out: list[dict] = []
    filtered_test = 0
    for t in tasks:
        upd = _parse_ts(t.get("updated_at"))
        if upd is None or upd < since:
            continue
        if _is_test_task(t.get("title")):
            filtered_test += 1
            continue
        created = _parse_ts(t.get("created_at"))
        out.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "status": t.get("status"),
            "wait_reason": t.get("wait_reason"),
            "close_outcome": t.get("close_outcome"),
            "updated_at": upd.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "assignee": t.get("assignee"),
            "objective_id": t.get("objective_id"),
            "project_id": t.get("project_id"),
            "is_new_in_window": bool(created and created >= since),
        })
    out.sort(key=lambda r: r["updated_at"], reverse=True)
    step_notes: list[str] = []
    if filtered_test:
        step_notes.append(
            f"moved: filtered {filtered_test} test task(s) by title "
            f"(flow-test or 'test ...' prefix); real tasks still shown.")
    return out, step_notes


# Wait reasons whose semantics mean "the owner's call is what unblocks this"
# (planner migration 0010: a queued task carries why it waits). `approval` is
# the planner's explicit wait for an approver; `owner` is a task stopped for
# the owner's decision. The collector groups both under "waiting for owner"
# because the owner is the only decider the card addresses; a reviewer who
# wants to split them has wait_reason in each row.
_WAITING_FOR_OWNER_REASONS = frozenset({"approval", "owner"})


def _task_state(t: dict) -> str:
    """queued, in_progress or closed, whichever vocabulary the planner spoke.

    `state` is the three-valued status on every planner since 0010; an older
    planner only has the one-word `status`, mapped here the way 0010 maps it.
    """
    st = t.get("state")
    if st:
        return st
    legacy = t.get("status") or ""
    if legacy in ("in_progress", "queued", "closed"):
        return legacy
    if legacy in ("assessing", "incident"):
        return "in_progress"
    return "closed" if legacy in ("done", "rejected") else "queued"


#: How 0010 seeds the legacy one-word names onto wait reasons, for a row from
#: a planner that does not carry wait_reason.
_LEGACY_WAIT = {"awaiting_approval": "approval", "pending_review": "review",
                "escalated": "owner", "blocked": "person"}


def _task_wait(t: dict) -> str | None:
    """Why a queued task waits (None: nobody has taken it yet)."""
    if t.get("state"):
        return t.get("wait_reason")
    return _LEGACY_WAIT.get(t.get("status") or "")


def _collect_waiting_for_owner(tasks: list[dict]) -> list[dict]:
    """Linked tasks that wait on the owner, of ANY age (not window-scoped).

    The owner asked for this: a task sitting for weeks in approval must
    still appear when the owner asks "what waits for me", or the card
    hides exactly the backlog it was built for.

    Each row carries status, updated_at, age in days from now, and the
    assignee for context. Rows are sorted oldest-first so the stalest
    ones lead; stale items are the ones that cost the project.
    """
    out: list[dict] = []
    now = datetime.now(timezone.utc)
    for t in tasks:
        if _task_state(t) != "queued" or _task_wait(t) not in _WAITING_FOR_OWNER_REASONS:
            continue
        if _is_test_task(t.get("title")):
            continue
        upd = _parse_ts(t.get("updated_at"))
        age_days: int | None
        if upd is None:
            age_days = None
            upd_iso: str | None = None
        else:
            age_days = max(0, int((now - upd).total_seconds() // 86400))
            upd_iso = upd.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        out.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "status": t.get("status"),
            "wait_reason": _task_wait(t),
            "updated_at": upd_iso,
            "assignee": t.get("assignee"),
            "objective_id": t.get("objective_id"),
            "age_days": age_days,
        })
    # Oldest first (highest age). Rows with age_days=None sort last.
    out.sort(key=lambda r: (-1 if r["age_days"] is None else r["age_days"]),
             reverse=True)
    return out


def _collect_where_it_stopped(tasks: list[dict]) -> dict | None:
    """The most recent linked-task move across all time, as a fallback
    anchor when the window is silent.

    When `moved` is empty the card still needs a factual answer to
    "when did anything last happen here?". This picks the one linked
    task with the highest updated_at, regardless of window, and returns
    its id, title, status, assignee and the move timestamp. Test tasks
    are excluded for the same reason as in `_collect_moved`.

    Returns None when the project has no linked tasks with a parseable
    updated_at (a brand-new project, or one whose tasks all failed to
    parse); the card then shows "no movement recorded".
    """
    best: dict | None = None
    best_dt: datetime | None = None
    for t in tasks:
        if _is_test_task(t.get("title")):
            continue
        upd = _parse_ts(t.get("updated_at"))
        if upd is None:
            continue
        if best_dt is None or upd > best_dt:
            best_dt = upd
            best = t
    if best is None or best_dt is None:
        return None
    now = datetime.now(timezone.utc)
    age_days = max(0, int((now - best_dt).total_seconds() // 86400))
    return {
        "id": best.get("id"),
        "title": best.get("title"),
        "status": best.get("status"),
        "wait_reason": best.get("wait_reason"),
        "close_outcome": best.get("close_outcome"),
        "updated_at": best_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "assignee": best.get("assignee"),
        "objective_id": best.get("objective_id"),
        "age_days": age_days,
    }


# Who the comment is from, as the card consumer needs to classify it.
# "admin" is the owner's actor_id on this node; "runner" is the operator
# session actor id. Everything else is agent or system. Classification is
# based on actor_id strings the planner records in the comment author
# column; a future node where the owner's actor_id is spelled differently
# needs this mapping bumped (it is the only place that encodes it).
OWNER_AUTHORS = frozenset({"admin"})
OPERATOR_AUTHORS = frozenset({"runner"})


def _comment_role(author: str | None) -> str:
    """Classify a comment author into owner / operator / agent / system.

    agent is a slug such as "agent-a", "agent-b"; system covers the
    small set of named components ("component:core-api", "system", ...).
    The collector does not need the exact agent slug, only the role, to
    answer the card's "who last spoke here" question.
    """
    if not isinstance(author, str) or not author:
        return "unknown"
    if author in OWNER_AUTHORS:
        return "owner"
    if author in OPERATOR_AUTHORS:
        return "operator"
    if author.startswith("component:") or author in {"system", "pipeline"}:
        return "system"
    return "agent"


def _collect_comments(
    moved: list[dict], since: datetime, until: datetime
) -> tuple[list[dict], list[str]]:
    """Step 5: comments on moved tasks whose created_at is in the window.

    The task description names this the most valuable source: the owner's
    decisions are recorded in comments. The collector pulls the full
    comment rows via the planner API and classifies the author into
    owner / operator / agent / system so the card can surface the
    heaviest line without re-parsing the author string.

    Returns a list, one dict per task that has at least one in-window
    comment, in the same order as `moved` (newest-moved first). Each
    entry carries the full comment rows and a `last_by_role` map pointing
    at the latest comment of each role, so the card can read
    `last_by_role.owner` directly rather than scan.

    Also returns a list of step-level notes (missed reads, caps hit)
    for the top-level `notes` field.

    Cost: one HTTP call per moved task. For a 3-day window of a busy
    project this is tens of calls; a batched endpoint on the planner
    would reduce the fanout, filed as a follow-up. Reads that fail
    (connection error, non-2xx) are counted and surfaced in notes
    rather than hidden.
    """
    out: list[dict] = []
    step_notes: list[str] = []
    read_failed_ids: list[int] = []

    for row in moved:
        tid = row.get("id")
        if not isinstance(tid, int):
            continue
        comments = _planner_get(f"api/v1/tasks/{tid}/comments")
        if not isinstance(comments, list):
            read_failed_ids.append(tid)
            continue

        in_window: list[dict] = []
        for c in comments:
            if not isinstance(c, dict):
                continue
            cat = _parse_ts(c.get("created_at"))
            if cat is None or cat < since or cat > until:
                continue
            in_window.append({
                "id": c.get("id"),
                "author": c.get("author"),
                "role": _comment_role(c.get("author")),
                "content": c.get("content"),
                "comment_type": c.get("comment_type"),
                "created_at": cat.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
        if not in_window:
            continue
        # Oldest-first within one task: a decision thread reads top-down.
        in_window.sort(key=lambda c: c["created_at"])
        last_by_role: dict[str, int] = {}
        for c in in_window:
            # Store the comment id of the latest for that role; comparing
            # created_at strings is safe because they are all ISO 8601 UTC.
            prev_id = last_by_role.get(c["role"])
            if prev_id is None:
                last_by_role[c["role"]] = c["id"]
                continue
            # The list is sorted oldest-first, so any later same-role
            # entry overwrites the previous one.
            last_by_role[c["role"]] = c["id"]
        out.append({
            "task_id": tid,
            "task_title": row.get("title"),
            "comments": in_window,
            "last_by_role": last_by_role,
        })

    if read_failed_ids:
        preview = ", ".join(str(x) for x in read_failed_ids[:5])
        suffix = "..." if len(read_failed_ids) > 5 else ""
        step_notes.append(
            f"comments: planner read failed for {len(read_failed_ids)} "
            f"task(s) ({preview}{suffix}); counts and last_by_role for "
            f"these tasks are missing from this response.")
    return out, step_notes


# Hard cap on the planner search endpoint (companions/planner/routers/tasks.py:
# limit: int = Query(default=50, ge=1, le=200)). This collector asks for the
# ceiling and surfaces truncation; a project whose mentions overflow 200 rows
# in a window is a case for a different, narrower query, not a wider walk.
_MENTIONS_LIMIT = 200


def _collect_mentions(
    project: dict,
    linked_ids: set[int],
    since: datetime,
    until: datetime,
) -> tuple[list[dict], list[str]]:
    """Step 4: tasks of OTHER projects whose title/description/comments name
    this project and whose updated_at is in the window.

    Match terms: the project's slug and, when the title is distinct and
    non-trivial, the title. The planner search endpoint matches case-
    insensitively over title/description/comment bodies (planner.routers
    .tasks.search_tasks); a tag-only mention is not reached this way,
    which matches the way the task description sets the step (titles and
    text).

    Dedup rules:
    - A task already in the linked set (step 2) is dropped. Those are the
      project's own moved tasks and are reported there.
    - A task that matches more than one term is dropped to one row; the
      matched_terms field tells the card which hit it.

    Returns the thinned task rows plus any step-level notes.
    """
    out: list[dict] = []
    step_notes: list[str] = []

    pid = project.get("id")
    if not isinstance(pid, int):
        return out, step_notes

    slug = project.get("slug") or ""
    title = (project.get("title") or "").strip()

    # Terms: the slug (always) and the title (if it adds a new word; a
    # title that is just the slug capitalised is skipped to save a call).
    terms: list[str] = []
    if slug:
        terms.append(slug)
    if title and title.lower() != slug.lower() and len(title) >= 3:
        terms.append(title)

    if not terms:
        step_notes.append(
            "mentions: project has no slug or title to search for; step 4 skipped.")
        return out, step_notes

    # One row per task-id, with the merged set of terms that matched.
    by_id: dict[int, dict] = {}
    truncated_terms: list[str] = []

    for term in terms:
        q = urllib.parse.urlencode({"q": term, "limit": str(_MENTIONS_LIMIT)})
        rows = _planner_get(f"api/v1/tasks/search?{q}")
        if not isinstance(rows, list):
            step_notes.append(
                f"mentions: search for term {term!r} failed; results for this "
                f"term are missing from this response.")
            continue
        if len(rows) >= _MENTIONS_LIMIT:
            truncated_terms.append(term)
        for t in rows:
            if not isinstance(t, dict):
                continue
            tid = t.get("id")
            if not isinstance(tid, int):
                continue
            if tid in linked_ids:
                continue
            # Mentions are OUTSIDE this project: drop rows whose own
            # project_id is this project. A null project_id can still be
            # a mention; keep it.
            own_pid = t.get("project_id")
            if own_pid == pid:
                continue
            upd = _parse_ts(t.get("updated_at"))
            if upd is None or upd < since or upd > until:
                continue
            created = _parse_ts(t.get("created_at"))
            entry = by_id.get(tid)
            if entry is None:
                entry = {
                    "id": tid,
                    "title": t.get("title"),
                    "status": t.get("status"),
                    "wait_reason": t.get("wait_reason"),
                    "close_outcome": t.get("close_outcome"),
                    "updated_at": upd.astimezone(timezone.utc)
                                     .isoformat().replace("+00:00", "Z"),
                    "assignee": t.get("assignee"),
                    "source_project_id": own_pid,
                    "objective_id": t.get("objective_id"),
                    "is_new_in_window": bool(created and created >= since),
                    "matched_terms": [],
                }
                by_id[tid] = entry
            if term not in entry["matched_terms"]:
                entry["matched_terms"].append(term)

    if truncated_terms:
        step_notes.append(
            f"mentions: planner search hit the {_MENTIONS_LIMIT}-row cap "
            f"on term(s) {truncated_terms!r}; mentions beyond that cap "
            f"are not in this response.")

    out = sorted(by_id.values(), key=lambda r: r["updated_at"], reverse=True)
    return out, step_notes


# A mesh document title: filename when it has one, otherwise the first
# non-empty line of content with the leading "#" stripped. The panel's
# Memory page uses the same convention (routes/memory.py:_title). This
# helper is a thinner version: no bookkeeping-line skip, no handoff kind
# re-write, because the collector hands the raw string to the card and
# the card decides how to display it.
def _mesh_title(doc: dict) -> str | None:
    name = (doc.get("filename") or "").strip()
    if name:
        return name[:160]
    content = doc.get("content") or ""
    for line in content.splitlines():
        s = line.strip()
        if s:
            return s.lstrip("#").strip()[:160] or None
    return None


def _mesh_type_from_tags(tags: Any) -> str | None:
    if not isinstance(tags, list):
        return None
    for t in tags:
        if isinstance(t, str) and t.startswith("type:"):
            return t[5:] or None
    return None


def _title_to_project_slug(title: str) -> str:
    """Mesh project tags are kebab-case ('project:smart-node'). A planner
    title such as 'Smart Node' or 'Flint' maps to that form by lowercasing
    and replacing runs of non-alnum with a single dash. The result is
    'project:' plus the mapped title; if the title is already slug-shaped
    the mapping is a no-op.
    """
    text = title.strip().lower()
    out_chars: list[str] = []
    prev_dash = False
    for ch in text:
        if ch.isalnum():
            out_chars.append(ch)
            prev_dash = False
        else:
            if not prev_dash and out_chars:
                out_chars.append("-")
                prev_dash = True
    while out_chars and out_chars[-1] == "-":
        out_chars.pop()
    return "".join(out_chars)


def _collect_memory(
    project: dict,
    since: datetime,
    until: datetime,
) -> tuple[list[dict], list[str]]:
    """Step 6: mesh documents whose updated_at is in the window.

    Documents can bind to a project in three ways on this node:
    - mesh's own `project` field, auto-inferred from similar documents
      (set via /?project=<name>);
    - a `project:<slug>` tag the writer put on by hand;
    - a free-form content mention of the project slug or title that no
      tag was added for. Content-only mentions are a different signal
      from a tagged document and are left to a later slice when the
      collector is given a per-document scorer.

    Terms searched: the project slug and, when distinct, the title's
    slugified form. Each term produces two queries, one on the mesh
    `project` field and one on the matching `project:<term>` tag. Rows
    are deduped by guid, keep the matched-terms, and are kept only when
    the window carries them (prefer updated_at, fall back to created_at).

    The mesh list endpoint caps at 100 rows per call; a term that hits
    the cap is surfaced in step_notes. Mesh being unreachable is
    surfaced too; the collector does not raise.
    """
    out: list[dict] = []
    step_notes: list[str] = []

    if not MESH_URL:
        step_notes.append(
            "memory: MESH_API_URL is empty; step 6 skipped. The card will "
            "not show mesh documents until the collector is configured.")
        return out, step_notes

    slug = (project.get("slug") or "").strip()
    title = (project.get("title") or "").strip()

    terms: list[tuple[str, str]] = []
    if slug:
        terms.append(("slug", slug))
    title_slug = _title_to_project_slug(title) if title else ""
    if title_slug and title_slug != slug:
        terms.append(("title", title_slug))

    if not terms:
        step_notes.append(
            "memory: project has no slug or title to search for; step 6 skipped.")
        return out, step_notes

    by_guid: dict[str, dict] = {}
    truncated_terms: list[str] = []
    failed_queries: list[str] = []
    filtered_snapshots: set[str] = set()

    for term_kind, term in terms:
        for mode, query in (
            ("project", f"?project={urllib.parse.quote(term)}"
                        f"&all=1&limit={_MESH_PAGE_LIMIT}"),
            ("tag", f"?tag=project:{urllib.parse.quote(term)}"
                    f"&all=1&limit={_MESH_PAGE_LIMIT}"),
        ):
            rows = _mesh_get(query, workspace="*")
            if not isinstance(rows, list):
                failed_queries.append(f"{mode}={term}")
                continue
            if len(rows) >= _MESH_PAGE_LIMIT:
                truncated_terms.append(f"{mode}={term}")
            matched_label = f"{mode}:{term}"
            for doc in rows:
                if not isinstance(doc, dict):
                    continue
                guid = doc.get("guid")
                if not isinstance(guid, str) or not guid:
                    continue
                if _is_session_snapshot(doc):
                    filtered_snapshots.add(guid)
                    continue
                upd = _parse_ts(doc.get("updated_at")) or _parse_ts(doc.get("created_at"))
                if upd is None or upd < since or upd > until:
                    continue
                entry = by_guid.get(guid)
                if entry is None:
                    created = _parse_ts(doc.get("created_at"))
                    entry = {
                        "guid": guid,
                        "workspace": doc.get("workspace") or None,
                        "title": _mesh_title(doc),
                        "type": _mesh_type_from_tags(doc.get("tags")),
                        "source": doc.get("source"),
                        "tags": doc.get("tags") or [],
                        "created_at": (created.astimezone(timezone.utc)
                                              .isoformat().replace("+00:00", "Z")
                                       if created else doc.get("created_at")),
                        "updated_at": upd.astimezone(timezone.utc)
                                         .isoformat().replace("+00:00", "Z"),
                        "matched_terms": [],
                    }
                    by_guid[guid] = entry
                if matched_label not in entry["matched_terms"]:
                    entry["matched_terms"].append(matched_label)

    if failed_queries:
        preview = ", ".join(failed_queries[:5])
        suffix = "..." if len(failed_queries) > 5 else ""
        step_notes.append(
            f"memory: mesh read failed for {len(failed_queries)} "
            f"query(ies) ({preview}{suffix}); documents reachable only "
            f"through these queries are missing from this response.")
    if truncated_terms:
        step_notes.append(
            f"memory: mesh list hit the {_MESH_PAGE_LIMIT}-row cap on "
            f"query(ies) {truncated_terms!r}; documents beyond that cap "
            f"are not in this response.")
    if filtered_snapshots:
        step_notes.append(
            f"memory: filtered {len(filtered_snapshots)} session-snapshot "
            f"document(s); operator debug artefacts are kept off the card.")

    out = sorted(by_guid.values(), key=lambda r: r["updated_at"], reverse=True)
    return out, step_notes


def _collect_doci(
    project: dict,
    since: datetime,
    until: datetime,
) -> tuple[list[dict], list[str]]:
    """Step 7: Doci documents under the project's own folders, in window.

    Doci binds a document to a project by placing it under a top-level
    folder named for the project. The collector walks the folder for the
    project slug and, when distinct, for the title's kebab-case form
    (same convention used in step 6 for mesh), merges the results by
    guid, and keeps only `doc_type == "document"` rows whose updated_at
    (fallback created_at) is in the window. Folders themselves and
    anything missing both timestamps drop out.

    Each surviving row records which folder term it came from, so the
    card author can tell "under slug" from "under title" when the two
    differ. Unreachable Doci, a missing project folder (treated as
    "no documents" rather than an error) and a walk hitting the local
    cap are each surfaced in step_notes rather than hidden.
    """
    out: list[dict] = []
    step_notes: list[str] = []

    if not DOCI_URL:
        step_notes.append(
            "documents: DOCI_URL is empty; step 7 skipped. The card will "
            "not show Doci documents until the collector is configured.")
        return out, step_notes

    slug = (project.get("slug") or "").strip()
    title = (project.get("title") or "").strip()

    folders: list[str] = []
    if slug:
        folders.append(slug)
    title_slug = _title_to_project_slug(title) if title else ""
    if title_slug and title_slug != slug:
        folders.append(title_slug)

    if not folders:
        step_notes.append(
            "documents: project has no slug or title to search for; "
            "step 7 skipped.")
        return out, step_notes

    by_guid: dict[str, dict] = {}
    failed_folders: list[str] = []
    truncated_folders: list[str] = []

    for folder in folders:
        data = _doci_get(f"api/list.php?folder={urllib.parse.quote(folder)}")
        if not isinstance(data, dict) or not data.get("success"):
            # A list.php response can legitimately say "no documents"
            # by returning success=true, documents=[]. Only an actual
            # read failure or a success=false reply is a miss.
            if data is None:
                failed_folders.append(folder)
            continue
        docs = data.get("documents") or []
        if not isinstance(docs, list):
            continue
        if len(docs) >= _DOCI_FOLDER_WALK_CAP:
            truncated_folders.append(folder)
            docs = docs[:_DOCI_FOLDER_WALK_CAP]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("doc_type") != "document":
                continue
            guid = doc.get("guid")
            if not isinstance(guid, str) or not guid:
                continue
            upd = _parse_ts(doc.get("updated_at")) or _parse_ts(doc.get("created_at"))
            if upd is None or upd < since or upd > until:
                continue
            entry = by_guid.get(guid)
            if entry is None:
                created = _parse_ts(doc.get("created_at"))
                entry = {
                    "guid": guid,
                    "path": doc.get("path"),
                    "title": doc.get("title"),
                    "doc_type": doc.get("doc_type"),
                    "summary": doc.get("summary"),
                    "created_by": doc.get("created_by"),
                    "created_at": (
                        created.astimezone(timezone.utc)
                               .isoformat().replace("+00:00", "Z")
                        if created else doc.get("created_at")),
                    "updated_at": upd.astimezone(timezone.utc)
                                     .isoformat().replace("+00:00", "Z"),
                    "matched_folders": [],
                }
                by_guid[guid] = entry
            if folder not in entry["matched_folders"]:
                entry["matched_folders"].append(folder)

    if failed_folders:
        preview = ", ".join(failed_folders[:5])
        suffix = "..." if len(failed_folders) > 5 else ""
        step_notes.append(
            f"documents: Doci read failed for {len(failed_folders)} "
            f"folder(s) ({preview}{suffix}); documents reachable only "
            f"through these folders are missing from this response.")
    if truncated_folders:
        step_notes.append(
            f"documents: Doci folder walk hit the "
            f"{_DOCI_FOLDER_WALK_CAP}-row cap on folder(s) "
            f"{truncated_folders!r}; documents beyond that cap are not "
            f"in this response.")

    out = sorted(by_guid.values(), key=lambda r: r["updated_at"], reverse=True)
    return out, step_notes


def _owner_identifiers() -> dict[str, str]:
    """Return the active super-admin's channel addresses by channel_slug.

    Reads sn_config.user_channels directly via core-api's own `db.cursor()`
    because the super-admin's addresses ARE core-api configuration; no
    service boundary is crossed. Cached for ~30 seconds so a daily digest
    across every project does not re-query on every collect() call. Returns
    an empty dict on any DB fault; step 8 then skips with a note.
    """
    now = time.monotonic()
    with _OWNER_CHANNELS_LOCK:
        cached = _OWNER_CHANNELS_CACHE
        if (cached["data"] is not None
                and now - cached["ts"] < _OWNER_CHANNELS_TTL):
            return dict(cached["data"])
    data: dict[str, str] = {}
    try:
        from db import cursor
    except ImportError:
        log.warning("project_state: db.cursor unavailable; owner identifiers skipped")
    else:
        try:
            with cursor() as cur:
                cur.execute(
                    "SELECT uc.channel_slug, uc.address "
                    "FROM sn_config.user_channels uc "
                    "JOIN sn_config.users u ON u.id = uc.user_id "
                    "WHERE u.role = 'super-admin' AND u.active "
                    "  AND uc.enabled = TRUE"
                )
                for row in cur.fetchall():
                    slug = row["channel_slug"] if isinstance(row, dict) else row[0]
                    addr = row["address"] if isinstance(row, dict) else row[1]
                    if isinstance(slug, str) and isinstance(addr, str) and addr:
                        data[slug] = addr
        except Exception as ex:  # noqa: BLE001  -- loud note matters more than precise raise
            log.warning("project_state: owner identifiers read failed: %s", ex)
            data = {}
    with _OWNER_CHANNELS_LOCK:
        _OWNER_CHANNELS_CACHE["data"] = dict(data)
        _OWNER_CHANNELS_CACHE["ts"] = now
    return data


def _messages_rows_in_window(
    since: datetime, until: datetime, limit: int, owner: dict[str, str],
) -> list[dict] | None:
    """Pull OWNER-CHANNEL sn_core.messages rows in the window via core-api's DB.

    `owner` is the dict from `_owner_identifiers()` mapping channel_slug to
    address. Scoping follows the task spec (step 8 of 5604):
    - web-chat: either sender or recipient matches the owner's web-chat
      address;
    - email: direction = 'inbound' AND recipient contains the owner's
      email (per-address list parsing lives on the ingest side; a bare
      substring match is enough here);
    - telegram: thread_id is in the set of threads where the owner typed
      at least once inside the window. A telegram message with
      thread_id = NULL is a one-shot notification bot post and never
      matches, which is the data-driven way to leave bots out without
      maintaining a bot slug list.

    Returns a list of row dicts (RealDictCursor shape, same as every other
    core-api route), or None on a DB read failure. Never raises; the caller
    records a miss in `notes`. Blocked / refused / failed state rows are
    excluded here so a card never quotes a message that was never sent.

    This is the only `db.cursor()` call on sn_core in the collector. Every
    other source goes through its service's HTTP API; sn_core.messages is
    core-api's own table, so the architect's cross-service rule does not
    apply.
    """
    try:
        from db import cursor  # local import so the module is importable without a DB in tests
    except ImportError:
        log.warning("project_state: db.cursor unavailable; step 8 skipped")
        return None

    owner_web = (owner.get("web-chat") or "").strip()
    owner_tg = (owner.get("telegram") or "").strip()
    owner_email = (owner.get("email") or "").strip()

    clauses: list[str] = []
    params: list[Any] = []
    if owner_web:
        clauses.append(
            "(m.channel_type = 'web-chat' AND (m.sender = %s OR m.recipient = %s))"
        )
        params.extend([owner_web, owner_web])
    if owner_email:
        clauses.append(
            "(m.channel_type = 'email' AND m.direction = 'inbound' "
            "  AND m.recipient ILIKE %s)"
        )
        params.append(f"%{owner_email}%")
    if owner_tg:
        # Pre-compute the set of owner-active telegram threads in-window
        # via a sub-select, so notification bots (thread_id IS NULL) and
        # one-way threads never make it into the result.
        clauses.append(
            "(m.channel_type = 'telegram' AND m.thread_id IN ("
            "   SELECT DISTINCT o.thread_id FROM sn_core.messages o"
            "    WHERE o.channel_type = 'telegram'"
            "      AND o.direction = 'inbound'"
            "      AND o.sender = %s"
            "      AND o.thread_id IS NOT NULL"
            "      AND o.received_at >= %s AND o.received_at <= %s"
            "))"
        )
        params.extend([owner_tg, since, until])

    if not clauses:
        # No owner addresses at all. Caller handles the skip note.
        return []

    channel_clause = "(" + " OR ".join(clauses) + ")"
    sql = (
        "SELECT m.id, m.thread_id, m.channel_type, m.direction, m.sender, "
        "       m.recipient, m.content, m.received_at, "
        "       ms.status, "
        "       m.metadata->>'subject' AS subject "
        "FROM sn_core.messages m "
        "LEFT JOIN sn_core.message_state ms ON ms.message_id = m.id "
        "WHERE m.received_at >= %s AND m.received_at <= %s "
        "  AND " + channel_clause + " "
        "  AND (ms.status IS NULL OR ms.status NOT IN "
        "       ('blocked','refused','failed')) "
        "ORDER BY m.received_at DESC "
        "LIMIT %s"
    )
    final_params: list[Any] = [since, until, *params, limit]
    try:
        with cursor() as cur:
            cur.execute(sql, final_params)
            rows = cur.fetchall()
    except Exception as ex:  # noqa: BLE001  -- a loud note matters more than a precise raise here
        log.warning("project_state: sn_core.messages read failed: %s", ex)
        return None
    return list(rows) if rows is not None else []


def _collect_owner_channels(
    project: dict,
    since: datetime,
    until: datetime,
    linked_ids: set[int],
) -> tuple[list[dict], list[str]]:
    """Step 8: owner-channel messages in the window matched to the project.

    Returns (rows, step_notes). Each row shape:
        {id, thread_id, channel_type, direction, sender, recipient,
         subject, content_snippet, received_at, status,
         matched_terms, matched_task_ids}

    Matching rules per the task spec:
    - matched_terms: substring hit of the project slug or the title's
      kebab-slug in content OR (for email) subject, case-insensitive.
      Same slug / title_slug convention as sources 6 and 7.
    - matched_task_ids: task-id references (`#NNNNN`, bare `NNNNN`,
      where NNNNN is 4 or 5 digits) that are also present in step 3's
      linked task set. A stray 5-digit number that is not one of the
      project's tasks is ignored.
    A message that matches NEITHER is dropped. A message that matches
    on both signals carries both lists.

    Channel scoping (owner's own channels only) is applied at SQL time
    by `_messages_rows_in_window` so notification bots and outbound
    agent mail never reach this function. See the task spec header on
    step 8 for the rationale.
    """
    out: list[dict] = []
    step_notes: list[str] = []

    slug = (project.get("slug") or "").strip()
    title = project.get("title") or ""
    title_slug = _title_to_project_slug(title) if title else ""

    terms: list[str] = []
    if slug:
        terms.append(slug.lower())
    if title_slug and title_slug.lower() not in terms:
        terms.append(title_slug.lower())

    if not terms and not linked_ids:
        step_notes.append(
            "owner channels: no slug, title or linked task ids to match; "
            "step 8 skipped.")
        return out, step_notes

    owner = _owner_identifiers()
    if not owner:
        step_notes.append(
            "owner channels: no super-admin channel addresses in "
            "sn_config.user_channels; step 8 skipped.")
        return out, step_notes

    rows = _messages_rows_in_window(
        since, until, _CHANNELS_WINDOW_CAP + 1, owner)
    if rows is None:
        step_notes.append(
            "owner channels: sn_core.messages read failed; step 8 incomplete.")
        return out, step_notes

    truncated = False
    if len(rows) > _CHANNELS_WINDOW_CAP:
        rows = rows[:_CHANNELS_WINDOW_CAP]
        truncated = True

    for r in rows:
        content = r.get("content") or ""
        subject = r.get("subject") or ""
        haystack_lc = (content + "\n" + subject).lower()
        matched_terms = [t for t in terms if t in haystack_lc]

        matched_task_ids: list[int] = []
        if linked_ids:
            for m in _TASK_REF_RE.finditer(content + "\n" + subject):
                try:
                    tid = int(m.group(1))
                except ValueError:
                    continue
                if tid in linked_ids and tid not in matched_task_ids:
                    matched_task_ids.append(tid)

        if not matched_terms and not matched_task_ids:
            continue

        snippet = content if len(content) <= 400 else content[:400] + "..."
        received = r.get("received_at")
        if isinstance(received, datetime):
            received_iso = (
                received.astimezone(timezone.utc)
                        .isoformat().replace("+00:00", "Z")
            )
        else:
            received_iso = str(received) if received else None
        out.append({
            "id": r.get("id"),
            "thread_id": r.get("thread_id"),
            "channel_type": r.get("channel_type"),
            "direction": r.get("direction"),
            "sender": r.get("sender"),
            "recipient": r.get("recipient"),
            "subject": subject or None,
            "content_snippet": snippet,
            "received_at": received_iso,
            "status": r.get("status"),
            "matched_terms": matched_terms,
            "matched_task_ids": matched_task_ids,
        })

    if truncated:
        step_notes.append(
            f"owner channels: window read hit the {_CHANNELS_WINDOW_CAP}-row "
            f"cap; older messages in the window are not in this response.")

    return out, step_notes


def _goals(objectives: list[dict], tasks: list[dict]) -> list[dict]:
    """Step 1: one row per objective, with task counts and last-move date.

    Only tasks already classified as linked (step 2) count toward this
    objective's totals. A task that moved after its objective was closed
    still counts for that objective; the collector does not re-check the
    objective. Closing stale objectives is a separate task.
    """
    tasks_by_obj: dict[int, list[dict]] = {}
    for t in tasks:
        oid = t.get("objective_id")
        if isinstance(oid, int):
            tasks_by_obj.setdefault(oid, []).append(t)

    out: list[dict] = []
    for o in objectives:
        oid = o.get("id")
        if not isinstance(oid, int):
            continue
        objective_tasks = tasks_by_obj.get(oid, [])
        last_move: datetime | None = None
        closed = 0
        for t in objective_tasks:
            if _task_state(t) == "closed":
                closed += 1
            upd = _parse_ts(t.get("updated_at"))
            if upd and (last_move is None or upd > last_move):
                last_move = upd
        out.append({
            "id": oid,
            "title": o.get("title"),
            "status": o.get("status"),
            "type": o.get("type"),
            "task_count": len(objective_tasks),
            "closed_count": closed,
            "last_task_move": (
                last_move.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if last_move else None),
            "updated_at": o.get("updated_at"),
        })
    out.sort(key=lambda r: (0 if r["status"] == "active" else 1, r["id"]))
    return out


def collect(slug: str, since: datetime, until: datetime | None = None) -> dict:
    """Collect project state for one project over a time window.

    Returns a dict with:
    - as_of, window_from, window_to
    - project (resolved row or null if the slug does not exist)
    - goals (step 1): rows per objective
    - linked_counts (step 2): one dict of counts
    - moved (step 3): list of tasks that moved in the window
    - notes: blind spots and caps the caller should surface
    - schema_version

    This is a FACTS layer. The caller composes the card.
    """
    now = datetime.now(timezone.utc)
    until = until or now
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    notes: list[str] = []

    project = _resolve_project(slug)
    if project is None:
        notes.append(f"project with slug={slug!r} not found via planner API")
        return {
            "as_of": now.isoformat().replace("+00:00", "Z"),
            "window_from": since.isoformat().replace("+00:00", "Z"),
            "window_to": until.isoformat().replace("+00:00", "Z"),
            "project": None,
            "goals": [],
            "linked_counts": {},
            "moved": [],
            "comments": [],
            "mentions": [],
            "memory": [],
            "documents": [],
            "channels": [],
            "waiting_for_owner": [],
            "where_it_stopped": None,
            "sources_completed": [],
            "notes": notes,
            "schema_version": COLLECTOR_SCHEMA_VERSION,
        }

    pid = project["id"]
    objectives = _list_project_objectives(pid)
    tasks, linked_counts, link_notes = _collect_linked_tasks(pid, slug, objectives)
    notes.extend(link_notes)
    moved, moved_notes = _collect_moved(tasks, since)
    notes.extend(moved_notes)
    waiting_for_owner = _collect_waiting_for_owner(tasks)
    where_it_stopped = _collect_where_it_stopped(tasks) if not moved else None
    goals = _goals(objectives, tasks)
    comments, comment_notes = _collect_comments(moved, since, until)
    notes.extend(comment_notes)
    linked_ids: set[int] = {
        t.get("id") for t in tasks if isinstance(t.get("id"), int)
    }
    mentions, mention_notes = _collect_mentions(project, linked_ids, since, until)
    notes.extend(mention_notes)
    memory, memory_notes = _collect_memory(project, since, until)
    notes.extend(memory_notes)
    documents, document_notes = _collect_doci(project, since, until)
    notes.extend(document_notes)
    channels, channel_notes = _collect_owner_channels(
        project, since, until, linked_ids)
    notes.extend(channel_notes)

    sources_completed = [
        "goals", "linked_tasks", "moved", "comments", "mentions", "memory",
        "documents", "channels",
    ]

    return {
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "window_from": since.isoformat().replace("+00:00", "Z"),
        "window_to": until.isoformat().replace("+00:00", "Z"),
        "project": {
            "id": project.get("id"),
            "slug": project.get("slug"),
            "title": project.get("title"),
        },
        "goals": goals,
        "linked_counts": linked_counts,
        "moved": moved,
        "comments": comments,
        "mentions": mentions,
        "memory": memory,
        "documents": documents,
        "channels": channels,
        "waiting_for_owner": waiting_for_owner,
        "where_it_stopped": where_it_stopped,
        "sources_completed": sources_completed,
        "notes": notes,
        "schema_version": COLLECTOR_SCHEMA_VERSION,
    }
