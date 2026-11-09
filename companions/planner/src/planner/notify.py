"""Best-effort notification hooks for the planner.

Two outbound paths:

1. ``fire_assignee_notify`` -- fires when a task's assignee changes. Pure HTTP
   call to core-api; does not touch task_notifications.

2. ``fire_status_notify`` -- fires after every status transition. Wakes the
   reviewer (when to_status is pending_review) or the assignee (all other
   statuses) via core-api, then marks the task_notifications row as fired.
   If notify_target is None (no reviewer/assignee), the wake is skipped and
   the row stays unfired; the omission is logged at ERROR so it is visible in
   the service log.

3. ``run_stale_pending_review_scanner`` -- long-running asyncio coroutine that
   periodically scans for tasks stuck in pending_review longer than the
   configured threshold and sends a report to the configured target agent.
   Run as a background task in the app lifespan.

All paths are best-effort: failures never propagate to the task operation.

Environment variables (configurable per deployment):

    PLANNER_NOTIFY_URL           -- core-api notify endpoint
    SN_OPERATOR_ACTOR            -- the node owner the notify is raised for
    SN_SERVICE_AUTH_SECRET       -- the service credential that proves the call
    PLANNER_NOTIFY_TIMEOUT_S     -- HTTP timeout for notify calls
    PLANNER_STALE_THRESHOLD_HOURS   -- hours before pending_review is stale (default 2)
    PLANNER_STALE_SCAN_INTERVAL_HOURS -- how often the scanner runs (default 4)
    PLANNER_STALE_REPORT_TARGET  -- agent slug to receive stale report (default pm)
    PLANNER_STALE_REPORT_FALLBACK -- fallback agent if primary returns 4xx (default foreman)
    PLANNER_STALE_IN_PROGRESS_HOURS -- idle hours before an in_progress task is asked about (default 1)
    PLANNER_STALE_IN_PROGRESS_MAX_REMINDERS -- reminders before escalation (default 3)
    PLANNER_STALE_IN_PROGRESS_MAX_AGE_HOURS -- idle hours past which a task is left alone (default 24)
    PLANNER_STALE_IN_PROGRESS_SCAN_MINUTES -- how often that pass runs (default 10)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

#: core-api notify endpoint. Override per deployment via ``PLANNER_NOTIFY_URL``.
NOTIFY_URL = os.environ.get("PLANNER_NOTIFY_URL", "")
if not NOTIFY_URL:
    logger.error(
        "PLANNER_NOTIFY_URL is not set. "
        "The node installer should write this to the planner service environment "
        "(companions/tracking.sh). Without it no wake is sent when a task is "
        "assigned or its status changes."
    )

#: The node owner the notify is raised for, and the credential that proves the
#: planner may raise it. core-api refuses an owner name sent over TCP with
#: nothing behind it, so both are required and neither has a default.
NOTIFY_REMOTE_USER = os.environ.get("SN_OPERATOR_ACTOR", "").strip()
NOTIFY_SERVICE_SECRET = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()


def _missing_proof_settings() -> list[str]:
    return [name for name, value in (("SN_OPERATOR_ACTOR", NOTIFY_REMOTE_USER),
                                     ("SN_SERVICE_AUTH_SECRET", NOTIFY_SERVICE_SECRET))
            if not value]


if _missing_proof_settings():
    logger.error(
        "%s not set in the planner service environment. core-api will refuse "
        "every notify, so no wake is sent when a task is assigned or its status "
        "changes. Re-run the core-api companion, then the tracking companion.",
        " and ".join(_missing_proof_settings()),
    )

#: Hard cap on the notify round-trip so a slow core-api never stalls the planner.
NOTIFY_TIMEOUT_S = float(os.environ.get("PLANNER_NOTIFY_TIMEOUT_S", "5"))

#: Hours before a pending_review task is considered stale. The default is 2
#: hours: the old 8-hour default meant a review handed over at lunchtime was
#: still "fresh" after dinner, which is exactly the gap the wake path's
#: current mechanics are meant to close.
STALE_THRESHOLD_HOURS = int(os.environ.get("PLANNER_STALE_THRESHOLD_HOURS", "2"))

#: How often the stale scanner runs.
STALE_SCAN_INTERVAL_HOURS = float(os.environ.get("PLANNER_STALE_SCAN_INTERVAL_HOURS", "4"))

#: Primary agent to receive the stale report.
STALE_REPORT_TARGET = os.environ.get("PLANNER_STALE_REPORT_TARGET", "pm")

#: Fallback agent if the primary target returns 4xx.
STALE_REPORT_FALLBACK = os.environ.get("PLANNER_STALE_REPORT_FALLBACK", "foreman")


#: Statuses in which the owner is being asked to look at the task. A wake with
#: the owner as the new assignee is a real ask only when the task has reached
#: one of these states; anywhere else the owner is a parking spot the flow
#: writes between blocks (see ``_owner_wake_suppressed_by_flow``). Kept here
#: so the notify module owns one policy for all owner wakes and the two call
#: sites in pipeline / repos.tasks consult the same set.
OWNER_DECISION_STATUSES: frozenset[str] = frozenset(
    {"pending_review", "awaiting_approval", "blocked"}
)


def _configured_owner_actors() -> frozenset[str]:
    """The actor ids that count as the node owner for notify purposes.

    Mirrors :func:`planner.repos.tasks._owner_ids` without importing it, so
    the notify module stays free of the repository/pipeline import cycle. The
    env variable ``PLANNER_OWNER_ACTOR_ID`` names the real owner on this node;
    the historical placeholders ``owner`` and ``dev`` stay in to cover
    environments where the real id has not been written yet.
    """
    configured = (
        os.environ.get("PLANNER_OWNER_ACTOR_ID", "admin").strip().lower() or "admin"
    )
    return frozenset({"owner", "dev", configured})


def _owner_wake_suppressed_by_flow(
    *,
    assignee: str | None,
    workflow_id: int | None,
    task_status: str | None,
    owner_actors: frozenset[str] | set[str] | None = None,
) -> bool:
    """True when a wake to ``assignee`` is a flow's between-blocks parking move.

    A workflow is driving the task: its block-end hand-off writes the owner
    as a safety net so a halted flow lands on him, but the next block takes
    the task back a few seconds later. Telling the owner on every such
    hand-off is a wake storm (68 mails and 59 Telegrams between 07:30
    and 09:15 on 2026-10-12). The real decisions the owner must take ride on
    status transitions into pending_review / awaiting_approval / blocked,
    which are kept out of this gate.

    Suppresses iff ALL of:
      1. the task carries a ``workflow_id`` (it is flow-managed);
      2. the status is NOT one of :data:`OWNER_DECISION_STATUSES` (the owner
         is not being asked, the flow is just running);
      3. the new ``assignee`` is an owner (``admin`` / configured owner).

    A non-owner wake is never suppressed: specialists keep being woken by
    every block. Tasks without a ``workflow_id`` are never suppressed either:
    a direct ask from a person goes through unchanged.
    """
    if workflow_id is None:
        return False
    if task_status in OWNER_DECISION_STATUSES:
        return False
    if not assignee:
        return False
    owners = owner_actors if owner_actors is not None else _configured_owner_actors()
    return assignee in owners


async def _post_notify(payload: dict) -> int | None:
    """POST one notify event. Returns HTTP status code, or None on error.
    Never raises -- logs and swallows all errors.
    """
    missing = ([] if NOTIFY_URL else ["PLANNER_NOTIFY_URL"]) + _missing_proof_settings()
    if missing:
        # Unconfigured, not a delivery failure. The module-level error already
        # said what to do; one line per call names the gap and the lost wake.
        logger.error(
            "notify NOT sent: %s not set (assignee=%s task_id=%s event=%s)",
            " and ".join(missing),
            payload.get("assignee"),
            payload.get("task_id"),
            payload.get("event"),
        )
        return None
    try:
        async with httpx.AsyncClient(timeout=NOTIFY_TIMEOUT_S) as client:
            resp = await client.post(
                NOTIFY_URL,
                json=payload,
                headers={"Remote-User": NOTIFY_REMOTE_USER,
                         "X-SN-Service": "planner",
                         "X-SN-Service-Auth": NOTIFY_SERVICE_SECRET,
                         "X-SN-On-Behalf-Of": NOTIFY_REMOTE_USER},
            )
        if resp.status_code == 200:
            logger.info(
                "notify: assignee=%s task_id=%s event=%s -> %s",
                payload.get("assignee"),
                payload.get("task_id"),
                payload.get("event"),
                resp.status_code,
            )
        else:
            # A refused notify is a wake that did not happen; core-api's answer
            # says which proof or setting it found wanting.
            logger.error(
                "notify REFUSED: assignee=%s task_id=%s event=%s -> %s %s",
                payload.get("assignee"),
                payload.get("task_id"),
                payload.get("event"),
                resp.status_code,
                resp.text[:200],
            )
        return resp.status_code
    except Exception as exc:  # noqa: BLE001 -- best-effort, must not propagate
        logger.warning(
            "notify failed (best-effort, ignored): assignee=%s task_id=%s: %s",
            payload.get("assignee"),
            payload.get("task_id"),
            exc,
        )
        return None


def _fire_targeted_notify(payload: dict) -> None:
    """Shared fire-and-forget transport for assignee/reviewer notifies.

    Both paths differ only in the ``event`` string and the recipient slug; the
    event-loop handling, logging and HTTP mechanics are identical.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        loop.create_task(_post_notify(payload))
    else:
        try:
            asyncio.run(_post_notify(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify failed (no loop, ignored): %s", exc)


def fire_assignee_notify(
    *,
    assignee: str | None,
    task_id: int,
    title: str | None,
    summary: str | None,
    workflow_id: int | None = None,
    task_status: str | None = None,
) -> None:
    """Schedule a best-effort assignee-assigned notification.

    Call this AFTER a task is committed with a (new) assignee. No-ops when
    ``assignee`` is falsy. Fire-and-forget: returns immediately, the HTTP call
    runs as a detached asyncio task. Safe to call from any async context; if no
    event loop is running the call is made synchronously (still best-effort).

    ``workflow_id`` and ``task_status`` are the task's current values and let
    this call consult :func:`_owner_wake_suppressed_by_flow`: when a flow is
    driving the task through its intermediate blocks, the owner is parked on
    the task as a safety net between blocks and must NOT be woken on every
    hand-off. The owner is still woken when the task carries a
    decision status (:data:`OWNER_DECISION_STATUSES`) or when it is not
    flow-managed at all. Callers that do not supply these fields keep the
    old behaviour; the two sites in planner.pipeline and planner.repos.tasks
    pass them so the guard applies.
    """
    if not assignee:
        return

    if _owner_wake_suppressed_by_flow(
        assignee=assignee,
        workflow_id=workflow_id,
        task_status=task_status,
    ):
        logger.info(
            "notify SKIPPED (owner parking between blocks): "
            "assignee=%s task_id=%s workflow_id=%s task_status=%s",
            assignee,
            task_id,
            workflow_id,
            task_status,
        )
        return

    payload = {
        "assignee": assignee,
        "task_id": task_id,
        "event": "assigned",
        "title": title or "",
        "summary": summary or "",
    }
    _fire_targeted_notify(payload)


def fire_mention_notify(
    *,
    actor: str | None,
    task_id: int,
    title: str | None,
    summary: str | None,
) -> None:
    """Schedule a best-effort notice that a comment on a task mentions ``actor``.

    The third of the targeted notifies, beside assignee and reviewer: same
    transport, same fire-and-forget, only the event differs. ``summary`` says
    which comment and whose (see ``planner.mentions``).
    """
    if not actor:
        return

    payload = {
        "assignee": actor,
        "task_id": task_id,
        "event": "mentioned",
        "title": title or "",
        "summary": summary or "",
    }
    _fire_targeted_notify(payload)


def fire_reviewer_notify(
    *,
    reviewer: str | None,
    task_id: int,
    title: str | None,
    summary: str | None,
) -> None:
    """Schedule a best-effort reviewer-assigned notification.

    Call this AFTER a task is committed with a (new) reviewer. Fire-and-forget
    like :func:`fire_assignee_notify`. The payload's ``assignee`` field holds
    the reviewer slug so core-api's existing /notify route (which wakes a
    single named agent) sends the wake to the right person; the ``event``
    string names what actually happened.

    Prior behaviour: swapping the reviewer on a pending_review task left the
    new reviewer uninformed and the task sitting in the wrong queue. The
    reviewer-notify path closes that gap symmetrically with the assignee one.
    """
    if not reviewer:
        return

    payload = {
        "assignee": reviewer,
        "task_id": task_id,
        "event": "reviewer_assigned",
        "title": title or "",
        "summary": summary or "",
    }
    _fire_targeted_notify(payload)


async def _do_status_notify(
    pool: asyncpg.Pool,
    *,
    notify_target: str | None,
    task_id: int,
    to_status: str,
    task_title: str,
) -> None:
    """Fire a wake to notify_target and mark the latest unfired notification row.

    If notify_target is None: skip the wake and leave fired_at null. Log the
    omission at ERROR (it means a task reached a reviewable state with no one
    to tell). Never raises.
    """
    if not notify_target:
        logger.error(
            "status_notify: no notify_target for task %s -> %s "
            "(no reviewer or assignee); wake skipped, notification row unfired",
            task_id,
            to_status,
        )
        return

    payload = {
        "assignee": notify_target,
        "task_id": task_id,
        "event": "status_changed",
        "title": task_title,
        "summary": f"moved to {to_status}",
    }
    status_code = await _post_notify(payload)

    if status_code is not None:
        # Mark the latest unfired status notification for this task as fired.
        # Best-effort: a failure here is non-fatal; the row stays unfired which
        # is a valid (auditable) state -- it means we woke the target but could
        # not record the acknowledgement.
        try:
            await pool.execute(
                """
                UPDATE task_notifications SET fired_at = now()
                WHERE id = (
                    SELECT id FROM task_notifications
                    WHERE task_id = $1
                      AND fired_at IS NULL
                      AND event_type LIKE 'status:%'
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                """,
                task_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "status_notify: could not update fired_at for task %s: %s",
                task_id,
                exc,
            )


def fire_status_notify(
    pool: asyncpg.Pool,
    *,
    notify_target: str | None,
    task_id: int,
    to_status: str,
    task_title: str,
) -> None:
    """Schedule a best-effort status-change notification.

    Call this AFTER the status change transaction has committed. Wakes the
    notify_target (reviewer for pending_review, assignee for other transitions)
    and updates ``fired_at`` on the task_notifications row.

    If notify_target is None: logs at ERROR and returns; the notification row
    stays with fired_at NULL so the event is auditable.

    Fire-and-forget: does not block the caller.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    coro = _do_status_notify(
        pool,
        notify_target=notify_target,
        task_id=task_id,
        to_status=to_status,
        task_title=task_title,
    )

    if loop is not None:
        loop.create_task(coro)
    else:
        try:
            asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            logger.warning("status_notify failed (no loop, ignored): %s", exc)


async def run_stale_pending_review_scanner(pool: asyncpg.Pool) -> None:
    """Long-running coroutine: periodically scan for stale pending_review tasks.

    Runs every PLANNER_STALE_SCAN_INTERVAL_HOURS hours. On each tick, queries
    for tasks in pending_review whose updated_at is older than
    PLANNER_STALE_THRESHOLD_HOURS. If any are found, sends a report to
    PLANNER_STALE_REPORT_TARGET (pm by default). If that target returns 4xx,
    retries with PLANNER_STALE_REPORT_FALLBACK (foreman).

    Designed to run as an asyncio background task for the lifetime of the app.
    Catches all exceptions (including CancelledError cleanly) so it never
    brings the app down.
    """
    logger.info(
        "stale-scanner: starting (threshold=%dh, interval=%sh, target=%s)",
        STALE_THRESHOLD_HOURS,
        STALE_SCAN_INTERVAL_HOURS,
        STALE_REPORT_TARGET,
    )

    while True:
        try:
            await asyncio.sleep(STALE_SCAN_INTERVAL_HOURS * 3600)
        except asyncio.CancelledError:
            logger.info("stale-scanner: cancelled, exiting")
            return

        try:
            rows = await pool.fetch(
                """
                SELECT id, title, reviewer, assignee,
                       EXTRACT(EPOCH FROM (now() - updated_at)) / 3600 AS hours_waiting
                FROM tasks
                WHERE stage = 'pending_review'
                  AND updated_at < now() - make_interval(hours => $1)
                ORDER BY updated_at ASC
                """,
                STALE_THRESHOLD_HOURS,
            )

            if not rows:
                logger.debug("stale-scanner: no stale pending_review tasks")
                continue

            lines = [
                f"Stale pending_review tasks (more than {STALE_THRESHOLD_HOURS}h waiting):"
            ]
            for r in rows:
                reviewer_str = r["reviewer"] or "no reviewer"
                assignee_str = r["assignee"] or "no assignee"
                hours = round(float(r["hours_waiting"]), 1)
                lines.append(
                    f"  #{r['id']}: {r['title']} "
                    f"[reviewer={reviewer_str}, assignee={assignee_str}, "
                    f"waiting={hours}h]"
                )
            report = "\n".join(lines)

            payload = {
                "assignee": STALE_REPORT_TARGET,
                "task_id": 0,
                "event": "stale_pending_review",
                "title": f"{len(rows)} stale pending_review task(s)",
                "summary": report,
            }

            logger.info(
                "stale-scanner: %d stale task(s); notifying %s",
                len(rows),
                STALE_REPORT_TARGET,
            )

            status_code = await _post_notify(payload)
            if status_code is not None and status_code >= 400:
                # Primary target rejected; try the fallback.
                logger.warning(
                    "stale-scanner: primary target %s returned %s, trying fallback %s",
                    STALE_REPORT_TARGET,
                    status_code,
                    STALE_REPORT_FALLBACK,
                )
                payload["assignee"] = STALE_REPORT_FALLBACK
                await _post_notify(payload)

        except asyncio.CancelledError:
            logger.info("stale-scanner: cancelled, exiting")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("stale-scanner: scan failed: %s", exc)


# --- stale in_progress watchdog -----------------------------------------------
#
# An agent is woken by events: an assignment, a status change, a mention, a
# message. Nothing is an event when a turn is cut or the agent simply stops, so
# an ordinary task could sit in_progress under its name for good (seen on
# 2026-10-11, with twenty more found the same day). This pass is the missing clock.
#
# It is bounded on purpose. A reminder that repeats without limit is a wake
# storm; one that reaches back without limit starts month-old work nobody asked
# for. So: a few reminders, one escalation, then silence until someone touches
# the task, and nothing at all for a task idle longer than the window.
#
# The record of what was already said is the comments themselves. They are what
# the woken agent reads, what the owner sees on the task, and they survive a
# restart, so there is no second place to keep in step with them.

#: Hours without activity before an in_progress task is asked about.
STALE_IN_PROGRESS_HOURS = float(os.environ.get("PLANNER_STALE_IN_PROGRESS_HOURS", "1"))

#: Reminders to the assignee before the task is escalated.
STALE_IN_PROGRESS_MAX_REMINDERS = int(
    os.environ.get("PLANNER_STALE_IN_PROGRESS_MAX_REMINDERS", "3"))

#: Past this many idle hours a task is backlog, not an agent that fell asleep.
STALE_IN_PROGRESS_MAX_AGE_HOURS = float(
    os.environ.get("PLANNER_STALE_IN_PROGRESS_MAX_AGE_HOURS", "24"))

#: How often the pass runs.
STALE_IN_PROGRESS_SCAN_MINUTES = float(
    os.environ.get("PLANNER_STALE_IN_PROGRESS_SCAN_MINUTES", "10"))

#: Who the watchdog's comments are written as.
STALE_COMMENT_AUTHOR = "system"
STALE_REMINDER_TYPE = "stale_reminder"
STALE_ESCALATED_TYPE = "stale_escalated"

# Activity is the later of updated_at and the last comment, because a comment
# does not bump updated_at. The watchdog's own comments are not activity, or
# every reminder would reset the clock it is counting on.
_STALE_IN_PROGRESS_SQL = """
SELECT t.id, t.title, t.assignee, t.requester,
       EXTRACT(EPOCH FROM (now() - a.active_at)) / 3600 AS idle_hours,
       r.reminders, r.escalated,
       (r.last_reminder IS NULL
        OR r.last_reminder < now() - make_interval(secs => $1)) AS due
FROM tasks t
CROSS JOIN LATERAL (
    SELECT greatest(t.updated_at, max(c.created_at)) AS active_at
    FROM task_comments c
    WHERE c.task_id = t.id
      AND coalesce(c.comment_type, '') NOT IN ($3, $4)
) a
CROSS JOIN LATERAL (
    SELECT count(*) FILTER (WHERE c.comment_type = $3) AS reminders,
           max(c.created_at) FILTER (WHERE c.comment_type = $3) AS last_reminder,
           coalesce(bool_or(c.comment_type = $4), false) AS escalated
    FROM task_comments c
    WHERE c.task_id = t.id
      AND c.created_at > a.active_at
      AND c.comment_type IN ($3, $4)
) r
WHERE t.stage = 'in_progress'
  AND t.assignee IS NOT NULL
  AND t.workflow_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM task_tags g
                  WHERE g.task_id = t.id AND g.tag LIKE 'blocked:%')
  AND a.active_at < now() - make_interval(secs => $1)
  AND a.active_at > now() - make_interval(secs => $2)
ORDER BY a.active_at
"""


class _NobodyWoken(Exception):
    """Rolls a reminder back when core-api woke no agent for it."""


async def _notify_woke(payload: dict) -> str | None:
    """POST one notify and return the agent core-api woke, or None.

    core-api is the authority on who is an agent: it answers ``woke`` with the
    slug when the assignee is one, and null for a person or an unknown name.
    Any failure is None too; the caller then records nothing and the next pass
    tries again.
    """
    missing = ([] if NOTIFY_URL else ["PLANNER_NOTIFY_URL"]) + _missing_proof_settings()
    if missing:
        logger.error("stale-in-progress: notify NOT sent: %s not set (task_id=%s)",
                     " and ".join(missing), payload.get("task_id"))
        return None
    try:
        async with httpx.AsyncClient(timeout=NOTIFY_TIMEOUT_S) as client:
            resp = await client.post(
                NOTIFY_URL,
                json=payload,
                headers={"Remote-User": NOTIFY_REMOTE_USER,
                         "X-SN-Service": "planner",
                         "X-SN-Service-Auth": NOTIFY_SERVICE_SECRET,
                         "X-SN-On-Behalf-Of": NOTIFY_REMOTE_USER},
            )
        if resp.status_code != 200:
            logger.error("stale-in-progress: notify REFUSED: assignee=%s task_id=%s -> %s %s",
                         payload.get("assignee"), payload.get("task_id"),
                         resp.status_code, resp.text[:200])
            return None
        return resp.json().get("woke")
    except Exception as exc:  # noqa: BLE001 -- a pass must outlive one bad call
        logger.warning("stale-in-progress: notify failed: assignee=%s task_id=%s: %s",
                       payload.get("assignee"), payload.get("task_id"), exc)
        return None


async def _stale_remind(pool: asyncpg.Pool, row) -> bool:
    """Write the reminder and wake the assignee, as one fact or not at all."""
    n = row["reminders"] + 1
    idle = round(float(row["idle_hours"]), 1)
    text = (
        f"Reminder {n} of {STALE_IN_PROGRESS_MAX_REMINDERS}: this task is in progress "
        f"under {row['assignee']} and nothing has happened on it for {idle} h. "
        f"{row['assignee']}: continue it, or return it with a comment that says why "
        f"you cannot. If you are waiting on someone, assign the task to them and say in "
        f"a comment that mentions them what you need; the task belongs to whoever it "
        f"waits on. A task you are not working on must not stay in progress under your name."
    )
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO task_comments (task_id, author, content, comment_type) "
                    "VALUES ($1, $2, $3, $4)",
                    row["id"], STALE_COMMENT_AUTHOR, text, STALE_REMINDER_TYPE)
                woke = await _notify_woke({
                    "assignee": row["assignee"],
                    "task_id": row["id"],
                    "event": "stale_in_progress",
                    "title": row["title"] or "",
                    "summary": text,
                    # Without it core-api also messages a person who holds the
                    # task, and a rolled-back reminder is due again every scan.
                    "wake_only": True,
                })
                if woke != row["assignee"]:
                    # A person, an unknown name, or a refused call. A reminder
                    # nobody received must not be counted as one.
                    raise _NobodyWoken()
    except _NobodyWoken:
        return False
    logger.info("stale-in-progress: task %s reminder %d/%d -> %s (idle %.1fh)",
                row["id"], n, STALE_IN_PROGRESS_MAX_REMINDERS, row["assignee"], idle)
    return True


async def _stale_escalate(pool: asyncpg.Pool, row) -> None:
    """Say once that the reminders are over, and tell whoever asked for the work."""
    idle = round(float(row["idle_hours"]), 1)
    requester = row["requester"]
    text = (
        f"Reminders stopped: {row['assignee']} was reminded {row['reminders']} times and "
        f"nothing has happened on this task for {idle} h. It needs a decision from "
        f"{requester or 'its requester'}: reassign it, return it, or close it."
    )
    await pool.execute(
        "INSERT INTO task_comments (task_id, author, content, comment_type) "
        "VALUES ($1, $2, $3, $4)",
        row["id"], STALE_COMMENT_AUTHOR, text, STALE_ESCALATED_TYPE)
    woke = None
    if requester and requester != row["assignee"] and "@" not in requester:
        woke = await _notify_woke({
            "assignee": requester,
            "task_id": row["id"],
            "event": "stale_in_progress_escalated",
            "title": row["title"] or "",
            "summary": text,
        })
    logger.warning("stale-in-progress: task %s escalated after %d reminders to %s "
                   "(idle %.1fh, requester %s, woke %s)",
                   row["id"], row["reminders"], row["assignee"], idle, requester, woke)


async def stale_in_progress_pass(pool: asyncpg.Pool) -> dict:
    """One pass over idle in_progress tasks. Returns what it did, for the log."""
    rows = await pool.fetch(
        _STALE_IN_PROGRESS_SQL,
        STALE_IN_PROGRESS_HOURS * 3600,
        STALE_IN_PROGRESS_MAX_AGE_HOURS * 3600,
        STALE_REMINDER_TYPE,
        STALE_ESCALATED_TYPE,
    )
    done = {"idle": len(rows), "reminded": [], "escalated": []}
    for row in rows:
        if row["escalated"] or not row["due"]:
            continue
        if "@" in row["assignee"]:
            # core-api resolves a person by email and mails them on any notify,
            # with the wording of a new assignment. This pass is for agents; a
            # person must not get that mail every scan.
            continue
        if row["reminders"] >= STALE_IN_PROGRESS_MAX_REMINDERS:
            await _stale_escalate(pool, row)
            done["escalated"].append(row["id"])
        elif await _stale_remind(pool, row):
            done["reminded"].append(row["id"])
    return done


async def run_stale_in_progress_scanner(pool: asyncpg.Pool) -> None:
    """Long-running coroutine: the clock for in_progress tasks nobody is working.

    Same shape as the pending_review scanner above: sleeps first, then scans,
    catches everything so it never brings the app down, exits cleanly on cancel.
    """
    logger.info(
        "stale-in-progress: starting (idle=%sh, reminders=%d, window=%sh, every %smin)",
        STALE_IN_PROGRESS_HOURS, STALE_IN_PROGRESS_MAX_REMINDERS,
        STALE_IN_PROGRESS_MAX_AGE_HOURS, STALE_IN_PROGRESS_SCAN_MINUTES,
    )
    while True:
        try:
            await asyncio.sleep(STALE_IN_PROGRESS_SCAN_MINUTES * 60)
        except asyncio.CancelledError:
            logger.info("stale-in-progress: cancelled, exiting")
            return
        try:
            done = await stale_in_progress_pass(pool)
            if done["reminded"] or done["escalated"]:
                logger.info("stale-in-progress: pass done: %s", done)
        except asyncio.CancelledError:
            logger.info("stale-in-progress: cancelled, exiting")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("stale-in-progress: pass failed: %s", exc)
