"""Pipeline state machine for tasks.

Layered on top of :class:`planner.repos.tasks.TasksRepo`. The repo accepts any
status; this module enforces the transition matrix. Every status change is
atomic inside a single DB transaction:

    1. lock the task row (``SELECT ... FOR UPDATE``)
    2. validate the transition (unless ``force=True``)
    3. update ``tasks`` (status + optional extra fields + ``updated_at``)
    4. append one row to ``task_log`` (action ``status_changed``)
    5. enqueue one row in ``task_notifications`` (``status:<new>``)
    6. optionally insert a row in ``task_comments`` from ``actor``

R2-1 scope: ``TRANSITIONS`` map + :func:`is_valid_transition` /
:func:`allowed_from` + :class:`Pipeline.status_change`. Higher-level stage
actions (claim, submit_assessment, approve, ...) live in subsequent R2 tasks
and call into ``status_change``.
"""

from __future__ import annotations

import json
import logging
from typing import Final

import asyncpg

logger = logging.getLogger(__name__)

from planner import statuses as _st
from planner.models import Task, TaskStatus
from planner.notify import (
    _do_status_notify,
    _post_notify,
    fire_assignee_notify,
    fire_status_notify,
)
from planner.repos.tasks import _TASK_COLS, _row_to_task

# ---------------------------------------------------------------------------
# Transition matrix
# ---------------------------------------------------------------------------

#: The legacy one-word stage names (planner.statuses). There is no transition
#: matrix any more (migration 0010): any stage may follow any other, and what
#: guards a move is a rule that can say why it refuses (a close carries a
#: report, only the reviewer closes a reviewed task, a decision answers only a
#: task that waits for one). ``TRANSITIONS`` is kept as a name for the UI and
#: for callers that list "where can this go": every other stage.
STAGES: Final[tuple[str, ...]] = _st.LEGACY_STAGES
TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    s: frozenset(t for t in STAGES if t != s) for s in STAGES
}


class PipelineError(ValueError):
    """Raised on invalid transitions or pipeline-rule violations."""


class TaskNotFoundError(PipelineError):
    """The task does not exist (maps to 404)."""


class StatusConflictError(PipelineError):
    """The task is no longer in a status this change answers (maps to 409):
    typically a second decision on a task the first one already moved."""


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """Any move between two different stages is allowed; a self-move is a no-op
    handled at the call site, not a transition."""
    return _st.normalize_stage(from_status) != _st.normalize_stage(to_status)


def allowed_from(status: str) -> frozenset[str]:
    """Every stage other than ``status``."""
    return TRANSITIONS.get(_st.normalize_stage(status), frozenset(STAGES))


class _NowSentinel:
    """Sentinel for ``extra_fields`` that translates to SQL ``now()``.

    Using the database clock keeps stage timestamps consistent with
    ``updated_at`` and avoids drift between Python and Postgres clocks.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debug aid only
        return "_NOW"


_NOW: _NowSentinel = _NowSentinel()


# Whitelist of fields that ``status_change`` may patch alongside ``status``.
# Stage timestamps and pipeline metadata only -- generic field updates belong
# in TasksRepo.update.
_EXTRA_FIELDS_ALLOWED: Final[frozenset[str]] = frozenset(
    {
        "assessed_at",
        "approved_at",
        "execution_started_at",
        "completed_at",
        "complexity_hint",
        "scope",
        "eta",
        "blocked_by",
        "blocked_reason",
        "external_detail",
        "previous_status",
        "verification_attempts",
        "assignee",
        "reviewer",  # who reviews the task in pending_review
        "wait_reason",  # with to_status "queued": why it waits (migration 0010)
        "close_outcome",  # with to_status "closed": how it ended
    }
)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


#: Task statuses for which an agent-to-agent reassignment is still gated by
#: owner approval. Outside these states the task has already been approved to
#: run (open/in_progress/assessing/pending_review), so re-targeting the agent
#: assigned to it is a legitimate handoff and MUST wake the new owner.
#:
#: Rationale: the original ``suppress_wake`` predicate glued four conditions
#: together without the status check, and that silently glued two legitimate
#: paths too, the runner proxying for the owner (actor=runner, non-owner
#: requester) and a triaging colleague reassigning another agent. Both left
#: tasks woken by nobody and showed up as "runner does not wake" and
#: "assignment does not raise the flag" in reassignment handoffs. The status
#: check keeps the original gate (agent files work for an unapproved task)
#: and no longer catches these two.
PRE_APPROVAL_STATUSES: Final[frozenset[TaskStatus]] = frozenset(
    {"awaiting_approval", "open"}
)


def should_suppress_wake(
    *,
    actor: str | None,
    requester: str | None,
    approved_at: object,
    task_status: str | None,
    owner_actors: frozenset[str] | set[str],
) -> bool:
    """True iff the pending wake should be withheld pending owner approval.

    The wake is suppressed only when ALL of the following hold:
      1. the task is still in a pre-approval status (``awaiting_approval`` or
         ``open``) and has never been approved;
      2. the actor making the change is not an owner;
      3. the task's requester is not an owner either.

    Outside pre-approval statuses the task has already been admitted to the
    pipeline, so a reassignment is a routine handoff and must wake the new
    owner. See ``PRE_APPROVAL_STATUSES`` for the rationale.
    """
    if task_status not in PRE_APPROVAL_STATUSES:
        return False
    if approved_at is not None:
        return False
    if actor is None or actor in owner_actors:
        return False
    if requester is None or requester in owner_actors:
        return False
    return True


class Pipeline:
    OWNER_ACTORS = frozenset({"admin", "owner", "dev"})
    """Atomic state-machine wrapper over the tasks storage layer."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    #: Statuses whose entry triggers cascading unblock of dependent tasks.
    CASCADE_UNBLOCK_FROM: Final[frozenset[TaskStatus]] = frozenset(
        {"done", "escalated", "rejected"}
    )

    async def status_change(
        self,
        task_id: int,
        to_status: TaskStatus,
        *,
        actor: str,
        comment: str | None = None,
        notify_target: str = "agents",
        force: bool = False,
        extra_fields: dict[str, object] | None = None,
        expect_from: frozenset[str] | None = None,
        replace_tag: tuple[str, str] | None = None,
        wait_reason: str | None = None,
        close_outcome: str | None = None,
    ) -> Task:
        """Transition ``task_id`` to ``to_status`` atomically.

        ``to_status`` is a legacy stage name (``pending_review``, ``done`` ...)
        or one of the three states (``queued``, ``in_progress``, ``closed``);
        with a state, ``wait_reason`` / ``close_outcome`` say the rest.

        - ``actor``: ``actors.id`` of the entity performing the change. Recorded
          on ``task_log`` and (if ``comment`` is given) ``task_comments``.
        - ``comment``: optional free-text comment authored by ``actor``.
        - ``notify_target``: queue identifier for the consumer side; defaults
          to ``"agents"``.
        - ``force``: bypass the transition matrix. Used by maintenance jobs
          and the staleness sweep -- never by normal stage actions.
        - ``extra_fields``: pipeline-related columns to patch in the same
          UPDATE (whitelisted; see ``_EXTRA_FIELDS_ALLOWED``).

        Side effect: when ``to_status`` is in
        :attr:`CASCADE_UNBLOCK_FROM`, every task currently blocked by
        ``task_id`` is unblocked in the same transaction (R2-5).

        No-op shortcut: if the task is already in ``to_status`` and no
        ``extra_fields`` are given, the row is returned unchanged and no
        log/notification is written.

        ``expect_from``: the statuses this change is an answer to, checked under
        the same row lock. A decision taken on a task that another decision has
        already moved is refused rather than applied twice.

        ``replace_tag``: ``(prefix, tag)``: every tag starting with ``prefix`` is
        removed and ``tag`` added, inside this transaction. A decision's tag and
        its status move commit together or not at all, and the row lock means
        two decisions cannot both land and leave two ``decision:*`` tags (the
        architect's condition for done-transition approval).

        Returns the updated :class:`~planner.models.Task`.
        """
        if wait_reason is not None or close_outcome is not None:
            extra_fields = dict(extra_fields or {})
            if wait_reason is not None:
                extra_fields["wait_reason"] = wait_reason
            if close_outcome is not None:
                extra_fields["close_outcome"] = close_outcome
        # If this transition also (re)assigns the task, remember the prior
        # assignee so we can fire a best-effort notify only on a real change.
        notify_assignee = extra_fields is not None and "assignee" in extra_fields
        old_assignee: object = None
        configured_owner = __import__("os").environ.get(
            "PLANNER_OWNER_ACTOR_ID", "admin"
        ).strip().lower() or "admin"
        owner_actors = (self.OWNER_ACTORS - {"admin"}) | {configured_owner}
        if notify_assignee:
            old_assignee = await self._pool.fetchval(
                "SELECT assignee FROM tasks WHERE id = $1", task_id
            )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if expect_from is not None:
                    current = await conn.fetchval(
                        "SELECT stage FROM tasks WHERE id = $1 FOR UPDATE", task_id
                    )
                    if current is None:
                        raise TaskNotFoundError(f"task {task_id} not found")
                    if expect_from is not None and current not in expect_from:
                        raise StatusConflictError(
                            f"task {task_id} is {current}, not "
                            f"{' or '.join(sorted(expect_from))}"
                        )
                task = await self._apply_status_change(
                    conn,
                    task_id,
                    to_status,
                    actor=actor,
                    comment=comment,
                    notify_target=notify_target,
                    force=force,
                    extra_fields=extra_fields,
                )
                if replace_tag is not None:
                    prefix, tag = replace_tag
                    await conn.execute(
                        "DELETE FROM task_tags WHERE task_id = $1 AND starts_with(tag, $2)",
                        task_id,
                        prefix,
                    )
                    await conn.execute(
                        "INSERT INTO task_tags (task_id, tag) VALUES ($1, $2) "
                        "ON CONFLICT DO NOTHING",
                        task_id,
                        tag,
                    )
                if task.status in self.CASCADE_UNBLOCK_FROM:
                    await self._cascade_unblock_blockees(conn, task_id)

        # Transaction committed -- now safe to notify (best-effort).
        # The task's status after the transition decides whether the wake-gate
        # (unapproved agent-to-agent handoff) still applies -- see
        # should_suppress_wake for the full argument.
        suppress_wake = should_suppress_wake(
            actor=actor,
            requester=getattr(task, "requester", None),
            approved_at=getattr(task, "approved_at", None),
            task_status=getattr(task, "status", None),
            owner_actors=owner_actors,
        )
        if notify_assignee and task.assignee and task.assignee != old_assignee and not suppress_wake:
            fire_assignee_notify(
                assignee=task.assignee,
                task_id=task.id,
                title=task.title,
                summary=task.status,
                workflow_id=getattr(task, "workflow_id", None),
                task_status=getattr(task, "status", None),
            )

        # Wake the reviewer (pending_review) or assignee (other transitions).
        # fire_status_notify is best-effort: it never raises.
        #
        # Gate 3: scope tasks must carry both PM_PROPOSAL and
        # ARCHITECT_REVIEW comment markers before the owner notification fires.
        # If the gate fails, the status change is committed (the task is in
        # awaiting_approval) but the wake is withheld.  The notify fires later
        # when add_comment detects that both markers are finally present.
        if task.status == "awaiting_approval" and not await self._scope_gate_passed(
            task.id
        ):
            logger.warning(
                "pipeline: scope gate blocked awaiting_approval notify for task %s "
                "(PM_PROPOSAL or ARCHITECT_REVIEW comment missing on release-scope task)",
                task.id,
            )
            return task


        # Agents may update someone else's task for bookkeeping, but must not
        # wake that person until a human owner has approved the work.
        if suppress_wake:
            return task
        wake_target = task.reviewer if task.status == "pending_review" else task.assignee
        fire_status_notify(
            self._pool,
            notify_target=wake_target,
            task_id=task.id,
            to_status=task.status,
            task_title=task.title,
        )

        return task

    async def _apply_status_change(
        self,
        conn: asyncpg.Connection,
        task_id: int,
        to_status: TaskStatus,
        *,
        actor: str,
        comment: str | None = None,
        notify_target: str = "agents",
        force: bool = False,
        extra_fields: dict[str, object] | None = None,
    ) -> Task:
        """Connection-bound core of :meth:`status_change`.

        Used by :meth:`status_change` (with its own transaction) and by
        cascade helpers that already hold a transaction.
        """
        extras = dict(extra_fields or {})
        unknown = set(extras) - _EXTRA_FIELDS_ALLOWED
        if unknown:
            raise PipelineError(
                f"extra_fields not allowed in status_change: {sorted(unknown)}"
            )

        # What is asked: one of the three states with its reason / outcome, or
        # a legacy stage name whose state the dictionaries decide. The guards
        # below speak in stages, so resolve the target stage first.
        new_style = _st.is_state(to_status)
        if not new_style and not _st.is_stage(to_status):
            raise PipelineError(f"unknown status: {to_status!r}")
        wait: str | None = None
        outcome: str | None = None
        if new_style:
            wait = extras.pop("wait_reason", None) or None
            outcome = extras.pop("close_outcome", None) or None
            if to_status == "closed" and not outcome:
                raise PipelineError(
                    "closing a task needs a close_outcome "
                    "(GET /api/v1/dictionaries/close-outcomes lists them)"
                )
            if to_status != "closed" and outcome:
                raise PipelineError("close_outcome goes with status closed only")
            if to_status != "queued" and wait:
                raise PipelineError("wait_reason goes with status queued only")
            if wait and not await _st.exists(conn, "wait_reasons", wait):
                raise PipelineError(f"unknown wait_reason {wait!r}")
            if outcome and not await _st.exists(conn, "close_outcomes", outcome):
                raise PipelineError(f"unknown close_outcome {outcome!r}")
            target_stage = await _st.stage_for(conn, to_status, wait, outcome)
        else:
            if "wait_reason" in extras or "close_outcome" in extras:
                raise PipelineError(
                    "wait_reason / close_outcome go with status queued / closed, "
                    "not with a legacy stage name"
                )
            target_stage = _st.normalize_stage(to_status)

        # Always record when a task closes. The DB trigger (0007, widened by
        # 0010 to every close) enforces the same for direct-SQL writers.
        if target_stage in ("done", "rejected") and "completed_at" not in extras:
            extras["completed_at"] = _NOW

        row = await conn.fetchrow(
            f"SELECT {_TASK_COLS} FROM tasks WHERE id = $1 FOR UPDATE",
            task_id,
        )
        if row is None:
            raise PipelineError(f"task {task_id} not found")

        old_status: str = row["status"]
        same_place = old_status == target_stage and (
            not new_style
            or (row["wait_reason"] == wait and row["close_outcome"] == outcome)
        )

        if same_place and not extras:
            return _row_to_task(row)

        # No transition matrix (migration 0010): the guards below are the rules.
        to_status = target_stage

        # Done-transition guards.
        #
        # 1. Comment required. A done transition must carry a report quoting
        #    evidence for each acceptance item. Nothing prevents a bad close
        #    faster than a hard refusal at the gate.
        if to_status == "done" and not force and not (comment and comment.strip()):
            raise PipelineError(
                "a comment is required when closing a task; "
                "state what you did, quoting the evidence for each acceptance item"
            )

        # 2. Only the reviewer may close (the confirmer is never the
        #    finder). The check fires only when a reviewer is set and differs
        #    from the actor. Tasks without a reviewer may be closed by anyone
        #    (not every task has a formal review step).
        if to_status == "done" and not force:
            task_reviewer: str | None = row["reviewer"]
            if task_reviewer and task_reviewer != actor:
                raise PipelineError(
                    f"task {task_id} has reviewer {task_reviewer!r}; "
                    f"only the reviewer may close it (actor is {actor!r}). "
                    f"Hand it to review instead (status queued, wait_reason review); "
                    f"a wrongly-closed task goes back the same way."
                )

        # Reviewer defaulting: when transitioning to pending_review and the task
        # has no reviewer at all, use the task's requester if it differs from the
        # effective assignee. A reviewer already on the task is never overwritten:
        # the reviewer is the only person the planner lets close a task, so
        # defaulting over an appointed one moves that authority to whoever filed
        # the task, silently and with no log line. If no suitable default
        # exists, log a fault but proceed -- the transition is never blocked by this.
        if (
            to_status == "pending_review"
            and "reviewer" not in extras
            and not row["reviewer"]
        ):
            effective_assignee = extras.get("assignee", row["assignee"])
            requester = row["requester"]
            if requester and requester != effective_assignee:
                extras["reviewer"] = requester
            else:
                logger.error(
                    "pipeline: task %s -> pending_review with no reviewer "
                    "(requester=%s, assignee=%s); transition proceeds but "
                    "no one will be notified",
                    task_id,
                    requester,
                    effective_assignee,
                )

        if new_style:
            # The state as itself; the trigger derives the stage.
            sets = ["status = $1", "wait_reason = $2", "close_outcome = $3", "updated_at = now()"]
            args: list[object] = [
                "closed" if target_stage in ("done", "rejected") else
                ("in_progress" if target_stage == "in_progress" else "queued"),
                wait,
                outcome,
            ]
        else:
            # The legacy name to the projection column; the trigger derives the state.
            sets = ["stage = $1", "updated_at = now()"]
            args = [to_status]
        for key, value in extras.items():
            if isinstance(value, _NowSentinel):
                sets.append(f"{key} = now()")
            else:
                sets.append(f"{key} = ${len(args) + 1}")
                args.append(value)
        args.append(task_id)

        updated_row = await conn.fetchrow(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ${len(args)} "
            f"RETURNING {_TASK_COLS}",
            *args,
        )
        assert updated_row is not None
        to_status = updated_row["status"]  # the stage the trigger derived

        if old_status != to_status:
            await conn.execute(
                """
                INSERT INTO task_log
                    (task_id, action, old_value, new_value, actor)
                VALUES ($1, 'status_changed', $2, $3, $4)
                """,
                task_id,
                old_status,
                to_status,
                actor,
            )

            # Compute the real notification target from the updated row:
            # reviewer for pending_review (they need to act), assignee for
            # everything else (they need to know about the transition).
            # May be None if neither field is populated; the row is still
            # written and the omission is logged by fire_status_notify.
            # The dictionary row of a wait may name who is told (effects.notify:
            # assignee | reviewer | owner | none); the seeded rows keep the old
            # behaviour, the reviewer for a review and the assignee otherwise.
            notify_who = "reviewer" if to_status == "pending_review" else "assignee"
            if updated_row["wait_reason"]:
                eff = await _st.effects(conn, "wait_reasons", updated_row["wait_reason"])
                notify_who = str(eff.get("notify") or notify_who)
            if notify_who == "reviewer":
                computed_notify_target: str | None = updated_row["reviewer"]
            elif notify_who == "owner":
                computed_notify_target = __import__("os").environ.get(
                    "PLANNER_OWNER_ACTOR_ID", "admin").strip().lower() or "admin"
            elif notify_who == "none":
                computed_notify_target = None
            else:
                computed_notify_target = updated_row["assignee"]

            notif_payload = json.dumps(
                {"from": old_status, "to": to_status, "actor": actor}
            )
            await conn.execute(
                """
                INSERT INTO task_notifications
                    (task_id, event_type, notify_target, payload)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                task_id,
                f"status:{to_status}",
                computed_notify_target,
                notif_payload,
            )
        if old_status == to_status and new_style and (
            row["wait_reason"] != updated_row["wait_reason"]
            or row["close_outcome"] != updated_row["close_outcome"]
        ):
            # Same stage, different reason or outcome (blocked on a person,
            # now blocked on an error): worth a line in the log, not a wake.
            await conn.execute(
                """
                INSERT INTO task_log
                    (task_id, action, old_value, new_value, actor)
                VALUES ($1, 'state_changed', $2, $3, $4)
                """,
                task_id,
                f"{row['state']}/{row['wait_reason'] or row['close_outcome'] or '-'}",
                f"{updated_row['state']}/{updated_row['wait_reason'] or updated_row['close_outcome'] or '-'}",
                actor,
            )

        if comment:
            await conn.execute(
                """
                INSERT INTO task_comments
                    (task_id, author, content, comment_type)
                VALUES ($1, $2, $3, 'status_change')
                """,
                task_id,
                actor,
                comment,
            )

        return _row_to_task(updated_row)

    async def _cascade_unblock_blockees(
        self,
        conn: asyncpg.Connection,
        completed_id: int,
    ) -> None:
        """Auto-unblock every task currently blocked_by ``completed_id``.

        Restores each blockee to its ``previous_status`` (or ``open`` if no
        snapshot) and clears the blocking columns. Each cascade step is its
        own status_change inside the same transaction, so they all roll back
        together if anything fails.
        """
        rows = await conn.fetch(
            "SELECT id, previous_status FROM tasks "
            "WHERE blocked_by = $1 AND stage = 'blocked'",
            completed_id,
        )
        for r in rows:
            target: TaskStatus = r["previous_status"] or "open"
            await self._apply_status_change(
                conn,
                r["id"],
                target,
                actor="system",
                comment=(
                    f"Auto-unblocked: blocker #{completed_id} completed."
                ),
                extra_fields={
                    "previous_status": None,
                    "blocked_by": None,
                    "blocked_reason": None,
                    "external_detail": None,
                },
            )

    # ------------------------------------------------------------------
    # Stage actions
    # ------------------------------------------------------------------
    #
    # Thin wrappers over ``status_change`` that encode the side effects
    # of each pipeline stage. Validation of "this status is reachable from
    # the current one" happens inside ``status_change`` via the matrix.

    async def claim(
        self,
        task_id: int,
        *,
        agent: str,
        comment: str | None = None,
    ) -> Task:
        """Agent picks up an open task: ``open -> in_progress`` (the retired
        ``assessing`` stage collapsed into in_progress, migration 0010).

        Side effects: ``assignee = agent``, ``assessed_at = now()``.
        ``agent`` doubles as the actor for log/comment. Refused when the task
        is not open: a claim answers a task nobody holds.
        """
        return await self.status_change(
            task_id,
            "in_progress",
            actor=agent,
            comment=comment,
            extra_fields={"assignee": agent, "assessed_at": _NOW},
            expect_from=frozenset({"open"}),
        )

    async def submit_assessment(
        self,
        task_id: int,
        *,
        actor: str,
        scope: str,
        eta: str,
        complexity_hint: str,
        comment: str | None = None,
    ) -> Task:
        """Assessment done: ``in_progress -> awaiting_approval``.

        Requires ``scope``, ``eta``, ``complexity_hint`` -- all three are
        the contract an assessment must produce. Empty strings are rejected.
        """
        for name, value in (
            ("scope", scope),
            ("eta", eta),
            ("complexity_hint", complexity_hint),
        ):
            if not str(value).strip():
                raise PipelineError(
                    f"submit_assessment: {name} must not be blank"
                )
        if complexity_hint not in ("simple", "moderate", "complex", "unknown"):
            raise PipelineError(
                f"submit_assessment: invalid complexity_hint {complexity_hint!r}"
            )
        return await self.status_change(
            task_id,
            "awaiting_approval",
            actor=actor,
            comment=comment,
            extra_fields={
                "scope": scope,
                "eta": eta,
                "complexity_hint": complexity_hint,
            },
            expect_from=frozenset({"in_progress", "open"}),
        )

    async def _proposed_doer(self, task_id: int) -> str | None:
        """The agent the filer named as the doer, when the task has no assignee and
        that name is a live agent. Read at approval, so that a task filed for a
        colleague is handed to the colleague by the owner's "go" and not left with
        nobody to wake."""
        row = await self._pool.fetchrow(
            "SELECT assignee, proposed_assignee FROM tasks WHERE id = $1", task_id)
        if row is None or row["assignee"] or not row["proposed_assignee"]:
            return None
        live = await self._pool.fetchval(
            "SELECT 1 FROM actors WHERE id = $1 AND type = 'agent' AND active",
            row["proposed_assignee"])
        return row["proposed_assignee"] if live else None

    async def approve(
        self,
        task_id: int,
        *,
        actor: str,
        comment: str | None = None,
    ) -> Task:
        """Approver greenlights: ``awaiting_approval -> in_progress``.

        Side effects: ``approved_at = now()`` and ``execution_started_at = now()``;
        and, when nobody is assigned and the filer named a live agent, that agent
        becomes the assignee and is woken the usual way.
        Refused when the task is not waiting for approval.
        """
        extra: dict[str, object] = {"approved_at": _NOW, "execution_started_at": _NOW}
        doer = await self._proposed_doer(task_id)
        if doer:
            extra["assignee"] = doer
        return await self.status_change(
            task_id,
            "in_progress",
            actor=actor,
            comment=comment,
            extra_fields=extra,
            expect_from=frozenset({"awaiting_approval"}),
        )

    #: What a waiting task may be decided from. approve and modify answer a task
    #: the pipeline is holding for a decision; reject also stops one that has not
    #: started. Both sets are subsets of TRANSITIONS, so the matrix still rules.
    DECIDE_FROM: Final[dict[str, frozenset[str]]] = {
        "approve": frozenset({"awaiting_approval"}),
        "modify": frozenset({"awaiting_approval"}),
        "reject": frozenset({"open", "assessing", "awaiting_approval", "escalated"}),
    }

    async def decide(
        self,
        task_id: int,
        decision: str,
        *,
        actor: str,
        comment: str,
        assignee: str | None = None,
    ) -> Task:
        """Record a person's decision on a waiting task.

        One transaction: the status move for the decision, the comment whose
        first line is ``DECISION: <what>`` (``planner.decisions``) and the
        ``decision:<what>`` tag replacing any earlier round's. Refused with
        :class:`PipelineError` when the task is not in a status this decision
        answers.

        - approve: ``awaiting_approval -> in_progress``, stamps ``approved_at``
          and ``execution_started_at``.
        - modify: ``awaiting_approval -> in_progress`` with ``approved_at`` NOT
          stamped (what runs next is not what was approved); optional
          ``assignee`` hands the rework to someone.
        - reject: pre-execution status ``-> rejected``.

        A task held by a tag while its status stays ``in_progress`` is not
        handled here yet: the architect asked that the two holding mechanisms be
        defined in the flows README first.
        """
        from planner import decisions  # local: decisions has no planner imports

        if decision not in self.DECIDE_FROM:
            raise PipelineError(f"unknown decision: {decision!r}")
        extra: dict[str, object] = {}
        if decision == "approve":
            to_status: TaskStatus = "in_progress"
            extra = {"approved_at": _NOW, "execution_started_at": _NOW}
            # The owner's own choice wins; otherwise the doer the filer named.
            doer = assignee or await self._proposed_doer(task_id)
            if doer:
                extra["assignee"] = doer
        elif decision == "modify":
            to_status = "in_progress"
            if assignee:
                extra["assignee"] = assignee
        else:
            to_status = "rejected"
        return await self.status_change(
            task_id,
            to_status,
            actor=actor,
            comment=comment,
            extra_fields=extra or None,
            expect_from=self.DECIDE_FROM[decision],
            replace_tag=(decisions.TAG_PREFIX, decisions.tag_for(decision)),
        )

    async def reject(
        self,
        task_id: int,
        *,
        actor: str,
        comment: str | None = None,
    ) -> Task:
        """Reject the task: any pre-execution stage -> ``rejected``.

        A task under way is not rejected but closed with an outcome
        (``status_change(..., "closed", close_outcome=...)``)."""
        return await self.status_change(
            task_id,
            "rejected",
            actor=actor,
            comment=comment,
            expect_from=frozenset(s for s in STAGES if s not in ("done", "rejected")),
        )

    async def review(
        self,
        task_id: int,
        *,
        actor: str,
        comment: str | None = None,
    ) -> Task:
        """Worker submits for review: ``in_progress -> pending_review``."""
        return await self.status_change(
            task_id,
            "pending_review",
            actor=actor,
            comment=comment,
            expect_from=frozenset({"in_progress"}),
        )

    async def verify(
        self,
        task_id: int,
        *,
        actor: str,
        comment: str | None = None,
    ) -> Task:
        """Reviewer accepts: ``pending_review -> done``.

        Side effects: ``completed_at = now()``.
        """
        return await self.status_change(
            task_id,
            "done",
            actor=actor,
            comment=comment,
            extra_fields={"completed_at": _NOW},
            expect_from=frozenset({"pending_review"}),
        )

    # ------------------------------------------------------------------
    # Bounce-back (review failed)
    # ------------------------------------------------------------------

    #: Maximum number of verification attempts before auto-escalation.
    VERIFICATION_LIMIT: Final[int] = 2

    async def bounce_back(
        self,
        task_id: int,
        *,
        actor: str,
        reason: str,
    ) -> Task:
        """Reviewer rejects work: ``pending_review -> in_progress``.

        Increments ``verification_attempts``. When the new count reaches
        :attr:`VERIFICATION_LIMIT`, the task is transitioned straight to
        ``escalated`` instead, with an auto-comment from the ``system`` actor
        explaining the limit was hit.

        ``reason`` is required (review feedback). It becomes the comment on
        the bounce-back / escalation.
        """
        if not reason.strip():
            raise PipelineError("bounce_back: reason must not be blank")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT stage AS status, verification_attempts FROM tasks "
                    "WHERE id = $1 FOR UPDATE",
                    task_id,
                )
                if row is None:
                    raise PipelineError(f"task {task_id} not found")
                if row["status"] != "pending_review":
                    raise PipelineError(
                        f"bounce_back: task is in {row['status']!r}, "
                        f"expected 'pending_review'"
                    )
                next_attempts = (row["verification_attempts"] or 0) + 1

        if next_attempts >= self.VERIFICATION_LIMIT:
            return await self.status_change(
                task_id,
                "escalated",
                actor="system",
                comment=(
                    f"Auto-escalated after {next_attempts} verification "
                    f"attempts. Last review feedback: {reason}"
                ),
                extra_fields={"verification_attempts": next_attempts},
            )

        return await self.status_change(
            task_id,
            "in_progress",
            actor=actor,
            comment=reason,
            extra_fields={"verification_attempts": next_attempts},
        )

    # ------------------------------------------------------------------
    # Block / unblock
    # ------------------------------------------------------------------

    async def block(
        self,
        task_id: int,
        *,
        actor: str,
        reason: str = "task_dependency",
        by_task_id: int | None = None,
        external_detail: str | None = None,
        comment: str | None = None,
    ) -> Task:
        """Pause a task: ``<any-active-status> -> blocked``.

        Snapshots the current status into ``previous_status`` so
        :meth:`unblock` can restore it. Sets ``blocked_by`` /
        ``blocked_reason`` / ``external_detail``.

        - ``reason`` must be one of ``task_dependency``, ``external_approval``,
          or ``scheduled_time``. ``by_task_id`` is required for
          ``task_dependency`` and forbidden otherwise; ``external_detail`` is
          required for ``external_approval`` / ``scheduled_time`` and
          forbidden for ``task_dependency``.
        - Raises :class:`PipelineError` if blocking would create a cycle in
          the ``blocked_by`` chain.
        """
        if reason not in ("task_dependency", "external_approval", "scheduled_time"):
            raise PipelineError(f"block: invalid reason {reason!r}")
        if reason == "task_dependency":
            if by_task_id is None:
                raise PipelineError(
                    "block: task_dependency requires by_task_id"
                )
            if external_detail is not None:
                raise PipelineError(
                    "block: task_dependency does not take external_detail"
                )
        else:
            if by_task_id is not None:
                raise PipelineError(
                    f"block: {reason} does not take by_task_id"
                )
            if not external_detail or not external_detail.strip():
                raise PipelineError(
                    f"block: {reason} requires external_detail"
                )

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT stage AS status FROM tasks WHERE id = $1 FOR UPDATE",
                    task_id,
                )
                if row is None:
                    raise PipelineError(f"task {task_id} not found")
                if row["status"] == "blocked":
                    raise PipelineError(f"task {task_id} is already blocked")
                previous = row["status"]

                if by_task_id is not None:
                    if by_task_id == task_id:
                        raise PipelineError(
                            "block: a task cannot block itself"
                        )
                    blocker = await conn.fetchrow(
                        "SELECT id FROM tasks WHERE id = $1",
                        by_task_id,
                    )
                    if blocker is None:
                        raise PipelineError(
                            f"block: blocker task {by_task_id} not found"
                        )
                    await self._assert_no_cycle(conn, task_id, by_task_id)

        return await self.status_change(
            task_id,
            "blocked",
            actor=actor,
            comment=comment,
            extra_fields={
                "previous_status": previous,
                "blocked_by": by_task_id,
                "blocked_reason": reason,
                "external_detail": external_detail,
            },
        )

    async def unblock(
        self,
        task_id: int,
        *,
        actor: str,
        comment: str | None = None,
    ) -> Task:
        """Resume a blocked task by restoring ``previous_status``.

        Clears ``blocked_by`` / ``blocked_reason`` / ``external_detail`` /
        ``previous_status``. If no ``previous_status`` snapshot exists, falls
        back to ``open``.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT stage AS status, previous_status FROM tasks WHERE id = $1",
                task_id,
            )
            if row is None:
                raise PipelineError(f"task {task_id} not found")
            if row["status"] != "blocked":
                raise PipelineError(
                    f"unblock: task is in {row['status']!r}, expected 'blocked'"
                )
            target: TaskStatus = row["previous_status"] or "open"

        return await self.status_change(
            task_id,
            target,
            actor=actor,
            comment=comment,
            expect_from=frozenset({"blocked"}),
            extra_fields={
                "previous_status": None,
                "blocked_by": None,
                "blocked_reason": None,
                "external_detail": None,
            },
        )

    # ------------------------------------------------------------------
    # Incident workflow
    # ------------------------------------------------------------------

    async def report_incident(
        self,
        task_id: int,
        *,
        actor: str,
        description: str,
    ) -> tuple[Task, int, int]:
        """Mark a delivered task as an incident: ``done -> in_progress`` (the
        retired ``incident`` stage collapsed into in_progress, migration 0010;
        the incident itself lives in the relations and the comment below).

        Auto-creates two child tasks in the same transaction:

        - **hotfix**: same project, same assignee as the original, priority
          ``high``, title ``"Fix: <description>"``.
        - **postmortem**: same project, unassigned (so the original assignee
          can't review their own miss), priority ``medium``, title
          ``"Post-mortem: Why did task #<id> pass verification?"``.

        Both children are linked back to the incident via ``task_relations``:
        the parent gets ``hotfix`` and ``postmortem`` rows pointing at the
        children, and each child gets a ``parent`` row pointing at the
        original. A summary comment is written on the incident task.

        Returns ``(updated_incident_task, hotfix_id, postmortem_id)``.
        """
        if not description.strip():
            raise PipelineError("report_incident: description must not be blank")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT title, project_id, assignee, stage FROM tasks WHERE id = $1 FOR UPDATE",
                    task_id,
                )
                if row is None:
                    raise PipelineError(f"task {task_id} not found")
                if row["stage"] != "done":
                    raise StatusConflictError(
                        f"task {task_id} is {row['stage']}, not done; "
                        f"an incident is reported on delivered work"
                    )
                parent_title = row["title"]
                project_id = row["project_id"]
                parent_assignee = row["assignee"]

                # 1. Transition parent done -> incident.
                incident_task = await self._apply_status_change(
                    conn,
                    task_id,
                    "incident",
                    actor=actor,
                    extra_fields=None,
                )

                # 2. Create hotfix.
                hotfix_id = await conn.fetchval(
                    """
                    INSERT INTO tasks
                        (title, description, project_id, assignee, priority, stage)
                    VALUES ($1, $2, $3, $4, 'high', 'open')
                    RETURNING id
                    """,
                    f"Fix: {description}",
                    f"Hotfix for incident on task #{task_id}: {parent_title}",
                    project_id,
                    parent_assignee,
                )

                # 3. Create post-mortem (unassigned -- avoid self-review).
                pm_id = await conn.fetchval(
                    """
                    INSERT INTO tasks
                        (title, description, project_id, priority, stage)
                    VALUES ($1, $2, $3, 'medium', 'open')
                    RETURNING id
                    """,
                    f"Post-mortem: Why did task #{task_id} pass verification?",
                    (
                        f"Post-mortem for task #{task_id}: {parent_title}. "
                        "Must include: (1) what went wrong, (2) why "
                        "verification missed it, (3) checklist changes to "
                        "prevent recurrence."
                    ),
                    project_id,
                )

                # 4. Link parent <-> hotfix (parent's hotfix row + child's parent row).
                await conn.execute(
                    """
                    INSERT INTO task_relations (task_id, related_id, relation)
                    VALUES ($1, $2, 'hotfix'), ($2, $1, 'parent')
                    """,
                    task_id,
                    hotfix_id,
                )
                # 5. Link parent <-> postmortem.
                await conn.execute(
                    """
                    INSERT INTO task_relations (task_id, related_id, relation)
                    VALUES ($1, $2, 'postmortem'), ($2, $1, 'parent')
                    """,
                    task_id,
                    pm_id,
                )

                # 6. Summary comment on the incident task.
                summary = (
                    f"Incident: {description}. "
                    f"Created hotfix #{hotfix_id} (assignee: "
                    f"{parent_assignee or 'unassigned'}) and "
                    f"post-mortem #{pm_id} (unassigned)."
                )
                await conn.execute(
                    """
                    INSERT INTO task_comments
                        (task_id, author, content, comment_type)
                    VALUES ($1, $2, $3, 'incident_followup')
                    """,
                    task_id,
                    actor,
                    summary,
                )

                return incident_task, hotfix_id, pm_id

    # ------------------------------------------------------------------
    # Pickup next task for an agent
    # ------------------------------------------------------------------

    async def pickup_next(
        self,
        *,
        agent: str,
        project_id: int | None = None,
    ) -> tuple[Task, str] | None:
        """Pick the next task for ``agent`` to work on.

        Order:

        1. **Resume own active work** -- tasks already assigned to ``agent``
           in ``in_progress``, ``assessing`` or ``pending_review``.
        2. **Assigned to me** -- tasks assigned to ``agent`` with status
           ``open``.
        3. **Unassigned and open** -- ``open`` tasks with no assignee.

        Within each group: ``priority`` desc (critical, high, medium, low),
        then ``eta`` asc (NULLs last), then ``created_at`` asc (oldest first).

        Returns ``(task, reason)`` where ``reason`` is one of ``"resume"``,
        ``"assigned"``, or ``"unassigned"``. Returns ``None`` if no candidate
        is available.

        Note: this is a pure read; it does not transition the picked task.
        The caller decides what to do with it.
        """
        priority_rank = (
            "CASE priority "
            "WHEN 'critical' THEN 0 "
            "WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 "
            "WHEN 'low' THEN 3 "
            "ELSE 9 END"
        )

        attempts: list[tuple[str, str, list[object]]] = [
            (
                "resume",
                f"""
                SELECT {_TASK_COLS} FROM tasks
                WHERE assignee = $1
                  AND stage IN ('in_progress', 'pending_review')
                  {{project_clause}}
                ORDER BY {priority_rank} ASC,
                         eta IS NULL, eta ASC,
                         created_at ASC, id ASC
                LIMIT 1
                """,
                [agent],
            ),
            (
                "assigned",
                f"""
                SELECT {_TASK_COLS} FROM tasks
                WHERE assignee = $1
                  AND stage = 'open'
                  {{project_clause}}
                ORDER BY {priority_rank} ASC,
                         eta IS NULL, eta ASC,
                         created_at ASC, id ASC
                LIMIT 1
                """,
                [agent],
            ),
            (
                "unassigned",
                f"""
                SELECT {_TASK_COLS} FROM tasks
                WHERE assignee IS NULL
                  AND stage = 'open'
                  {{project_clause}}
                ORDER BY {priority_rank} ASC,
                         eta IS NULL, eta ASC,
                         created_at ASC, id ASC
                LIMIT 1
                """,
                [],
            ),
        ]

        for reason, sql_template, args in attempts:
            if project_id is not None:
                clause = f"AND project_id = ${len(args) + 1}"
                final_args = [*args, project_id]
            else:
                clause = ""
                final_args = list(args)
            sql = sql_template.format(project_clause=clause)
            row = await self._pool.fetchrow(sql, *final_args)
            if row is not None:
                return _row_to_task(row), reason
        return None

    # ------------------------------------------------------------------
    # Scope-task approval gate
    # ------------------------------------------------------------------

    #: Tag that marks a task as a release scope task.
    RELEASE_SCOPE_TAG: Final[str] = "release-scope"
    #: Comment prefix for the PM proposal marker.
    PM_PROPOSAL_PREFIX: Final[str] = "PM_PROPOSAL:"
    #: Comment prefix for the architect review marker.
    ARCHITECT_REVIEW_PREFIX: Final[str] = "ARCHITECT_REVIEW:"

    async def _scope_gate_passed(self, task_id: int) -> bool:
        """Return False only if this is a release-scope task missing a marker.

        A task qualifies as a scope task when it carries the tag
        ``'release-scope'``.  For such tasks both ``PM_PROPOSAL:`` and
        ``ARCHITECT_REVIEW:`` prefixed comments must be present before the
        ``awaiting_approval`` owner notification fires.

        Returns True for non-scope tasks (gate does not apply) and for scope
        tasks that already carry both markers.
        """
        is_scope = await self._pool.fetchval(
            "SELECT 1 FROM task_tags WHERE task_id = $1 AND tag = $2",
            task_id,
            self.RELEASE_SCOPE_TAG,
        )
        if not is_scope:
            return True

        has_pm = await self._pool.fetchval(
            "SELECT 1 FROM task_comments "
            "WHERE task_id = $1 AND content LIKE $2 || '%'",
            task_id,
            self.PM_PROPOSAL_PREFIX,
        )
        has_arch = await self._pool.fetchval(
            "SELECT 1 FROM task_comments "
            "WHERE task_id = $1 AND content LIKE $2 || '%'",
            task_id,
            self.ARCHITECT_REVIEW_PREFIX,
        )
        return bool(has_pm and has_arch)

    async def scope_gate_fire_if_ready(self, task_id: int) -> bool:
        """Check the scope gate and fire the awaiting_approval notify if ready.

        Called after a comment is added to a release-scope task that is
        already in ``awaiting_approval``.  If both markers are now present,
        fire the status notify so the owner receives the approval request.

        Returns True if the notify was fired, False otherwise.
        """
        row = await self._pool.fetchrow(
            "SELECT stage AS status, assignee, reviewer, title "
            "FROM tasks WHERE id = $1",
            task_id,
        )
        if row is None or row["status"] != "awaiting_approval":
            return False
        if not await self._scope_gate_passed(task_id):
            return False

        wake_target = row["reviewer"] or row["assignee"]
        fire_status_notify(
            self._pool,
            notify_target=wake_target,
            task_id=task_id,
            to_status="awaiting_approval",
            task_title=row["title"],
        )
        logger.info(
            "pipeline: scope gate passed for task %s; awaiting_approval notify fired",
            task_id,
        )
        return True

    # ------------------------------------------------------------------
    # Conflict detection + staleness sweep
    # ------------------------------------------------------------------

    #: Statuses considered "active" for conflict detection -- tasks whose
    #: scope is currently being touched (or about to be).
    ACTIVE_FOR_CONFLICTS: Final[frozenset[TaskStatus]] = frozenset(
        {
            "open",
            "awaiting_approval",
            "in_progress",
            "pending_review",
            "blocked",
        }
    )

    async def check_conflicts(
        self,
        scope: str,
        *,
        exclude_task_id: int | None = None,
    ) -> list[Task]:
        """Return active tasks whose ``scope`` overlaps with ``scope``.

        Scope is treated as a comma-separated list of file paths. Two scopes
        conflict if any path token from one appears as a substring in the
        other. Empty / blank scope returns ``[]`` (no claim, no conflict).
        """
        tokens = [t.strip() for t in scope.split(",") if t.strip()]
        if not tokens:
            return []

        active = list(self.ACTIVE_FOR_CONFLICTS)
        sql = f"""
        SELECT {_TASK_COLS} FROM tasks
        WHERE stage = ANY($1::text[])
          AND scope IS NOT NULL AND scope <> ''
          AND id <> COALESCE($2, -1)
        """
        rows = await self._pool.fetch(sql, active, exclude_task_id)

        out: list[Task] = []
        for row in rows:
            existing_tokens = [
                t.strip() for t in (row["scope"] or "").split(",") if t.strip()
            ]
            for new_t in tokens:
                if any(
                    new_t in et or et in new_t for et in existing_tokens
                ):
                    out.append(_row_to_task(row))
                    break
        return out

    #: Hours after which an awaiting_approval task gets a reminder.
    APPROVAL_REMINDER_HOURS: Final[int] = 24
    #: Hours after which an awaiting_approval task is auto-escalated.
    APPROVAL_ESCALATE_HOURS: Final[int] = 48

    async def staleness_sweep(self) -> dict[str, list[int]]:
        """Maintenance pass over the task set.

        Performs three jobs:

        1. **Approval aging**: tasks in ``awaiting_approval`` for longer than
           :attr:`APPROVAL_REMINDER_HOURS` get a reminder comment + queued
           notification (only once per task). Tasks idling longer than
           :attr:`APPROVAL_ESCALATE_HOURS` are auto-escalated.
        2. **Orphaned block**: tasks ``blocked`` by a task that's already in
           ``done`` / ``rejected`` / ``escalated`` (cascade missed it for any
           reason) get auto-unblocked.
        3. **Verification-loop fallback**: tasks in ``pending_review`` with
           ``verification_attempts >= VERIFICATION_LIMIT`` get escalated.

        Returns a dict ``{action: [task_id, ...]}`` describing what was done.
        """
        result: dict[str, list[int]] = {
            "approval_reminder": [],
            "approval_escalate": [],
            "orphan_unblock": [],
            "verification_escalate": [],
        }
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 1a. Auto-escalate tasks idling >48h
                escalate_rows = await conn.fetch(
                    f"""
                    SELECT id FROM tasks
                    WHERE stage = 'awaiting_approval'
                      AND updated_at < now() - INTERVAL '{self.APPROVAL_ESCALATE_HOURS} hours'
                    """
                )
                for r in escalate_rows:
                    await self._apply_status_change(
                        conn,
                        r["id"],
                        "escalated",
                        actor="system",
                        comment=(
                            f"Auto-escalated: awaiting_approval older than "
                            f"{self.APPROVAL_ESCALATE_HOURS}h."
                        ),
                    )
                    result["approval_escalate"].append(r["id"])

                # 1b. Reminder for >24h, only if no reminder yet.
                reminder_rows = await conn.fetch(
                    f"""
                    SELECT t.id FROM tasks t
                    WHERE t.stage = 'awaiting_approval'
                      AND t.updated_at < now() - INTERVAL '{self.APPROVAL_REMINDER_HOURS} hours'
                      AND NOT EXISTS (
                        SELECT 1 FROM task_notifications n
                        WHERE n.task_id = t.id
                          AND n.event_type = 'reminder:awaiting_approval'
                      )
                    """
                )
                for r in reminder_rows:
                    await conn.execute(
                        """
                        INSERT INTO task_comments
                            (task_id, author, content, comment_type)
                        VALUES ($1, 'system', $2, 'escalation')
                        """,
                        r["id"],
                        f"Awaiting approval longer than {self.APPROVAL_REMINDER_HOURS}h.",
                    )
                    await conn.execute(
                        """
                        INSERT INTO task_notifications
                            (task_id, event_type, notify_target, payload)
                        VALUES ($1, 'reminder:awaiting_approval', 'approvers', NULL)
                        """,
                        r["id"],
                    )
                    result["approval_reminder"].append(r["id"])

                # 2. Orphaned blocks.
                orphan_rows = await conn.fetch(
                    """
                    SELECT t.id, t.previous_status FROM tasks t
                    JOIN tasks b ON b.id = t.blocked_by
                    WHERE t.stage = 'blocked'
                      AND b.stage IN ('done', 'rejected', 'escalated')
                    """
                )
                for r in orphan_rows:
                    target = r["previous_status"] or "open"
                    await self._apply_status_change(
                        conn,
                        r["id"],
                        target,
                        actor="system",
                        comment=(
                            "Auto-unblocked: orphaned block "
                            "(blocker reached terminal status)."
                        ),
                        extra_fields={
                            "previous_status": None,
                            "blocked_by": None,
                            "blocked_reason": None,
                            "external_detail": None,
                        },
                    )
                    result["orphan_unblock"].append(r["id"])

                # 3. Verification-loop fallback.
                veri_rows = await conn.fetch(
                    """
                    SELECT id FROM tasks
                    WHERE stage = 'pending_review'
                      AND verification_attempts >= $1
                    """,
                    self.VERIFICATION_LIMIT,
                )
                for r in veri_rows:
                    await self._apply_status_change(
                        conn,
                        r["id"],
                        "escalated",
                        actor="system",
                        comment=(
                            "Auto-escalated: verification limit reached "
                            "but task still in pending_review."
                        ),
                    )
                    result["verification_escalate"].append(r["id"])

        return result

    async def recurrence_sweep(self) -> dict[str, list[int]]:
        """Open the cycles of recurring tasks whose ``next_run`` has passed.

        For each due task (row locked, ``SKIP LOCKED`` so two sweeps never
        open the same cycle):

        - closed with a success outcome: reopened to ``in_progress``
          (``approved_at`` kept, ``completed_at`` cleared) with a comment
          naming the cycle; the assignee is woken.
        - closed with any other outcome (cancelled, rejected ...): a person
          stopped the series; the recurrence is switched off, with a comment.
        - still open from the previous cycle: a comment says the new cycle
          came due while it is open, and the assignee is woken.
        - waiting for the owner's approval: nobody is woken; the cycle is
          skipped.

        ``next_run`` then moves forward by whole intervals until it is in the
        future, so downtime opens one cycle, not one per missed interval.
        Wakes are sent after the transaction commits, awaited (the CLI closes
        its event loop on return, a detached task would be lost).

        Returns ``{action: [task_id, ...]}``.
        """
        result: dict[str, list[int]] = {
            "reopened": [],
            "still_open": [],
            "ended": [],
            "unapproved": [],
        }
        wakes: list[tuple[str, dict]] = []
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    f"""
                    SELECT {_TASK_COLS} FROM tasks
                    WHERE recur_interval IS NOT NULL AND next_run <= now()
                    ORDER BY next_run, id
                    FOR UPDATE SKIP LOCKED
                    """
                )
                for row in rows:
                    task_id = row["id"]
                    cycle = f"{row['next_run']:%Y-%m-%d %H:%M %Z}".strip()
                    if row["status"] == "awaiting_approval":
                        result["unapproved"].append(task_id)
                    elif row["state"] == "closed":
                        eff = await _st.effects(conn, "close_outcomes", row["close_outcome"])
                        if not bool(eff.get("success", False)):
                            await conn.execute(
                                "UPDATE tasks SET recur_interval = NULL, next_run = NULL "
                                "WHERE id = $1",
                                task_id,
                            )
                            await conn.execute(
                                """
                                INSERT INTO task_comments
                                    (task_id, author, content, comment_type)
                                VALUES ($1, 'system', $2, 'recurrence')
                                """,
                                task_id,
                                f"Recurrence ended: the task was closed as "
                                f"{row['close_outcome']}, so cycle {cycle} was not opened.",
                            )
                            result["ended"].append(task_id)
                            continue
                        if not row["assignee"] and row["workflow_id"] is None:
                            # Work cannot start with nobody to do it (migration 0013):
                            # the cycle waits, and the task says why.
                            await conn.execute(
                                """
                                INSERT INTO task_comments
                                    (task_id, author, content, comment_type)
                                VALUES ($1, 'system', $2, 'recurrence')
                                """,
                                task_id,
                                f"Recurring cycle {cycle} was not opened: nobody is "
                                f"assigned to this task. Name a doer and it opens on the "
                                f"next sweep.",
                            )
                            result.setdefault("no_doer", []).append(task_id)
                            continue
                        task = await self._apply_status_change(
                            conn,
                            task_id,
                            "in_progress",
                            actor="system",
                            comment=f"Recurring cycle {cycle} opened.",
                            force=True,
                            extra_fields={"completed_at": None},
                        )
                        wakes.append(("status", {
                            "notify_target": task.assignee,
                            "task_id": task_id,
                            "to_status": task.status,
                            "task_title": task.title,
                        }))
                        result["reopened"].append(task_id)
                    else:
                        await conn.execute(
                            """
                            INSERT INTO task_comments
                                (task_id, author, content, comment_type)
                            VALUES ($1, 'system', $2, 'recurrence')
                            """,
                            task_id,
                            f"Recurring cycle {cycle} came due while the previous "
                            f"cycle is still open.",
                        )
                        if row["assignee"]:
                            wakes.append(("due", {
                                "assignee": row["assignee"],
                                "task_id": task_id,
                                "event": "recurring_due",
                                "title": row["title"] or "",
                                "summary": f"cycle {cycle} came due; the previous one is still open",
                            }))
                        result["still_open"].append(task_id)
                    # Whole intervals past now: one cycle per sweep, never a
                    # burst after downtime. The fallback covers a gap longer
                    # than the series cap (100000 intervals).
                    await conn.execute(
                        """
                        UPDATE tasks SET next_run = COALESCE(
                            (SELECT next_run + n * recur_interval
                               FROM generate_series(1, 100000) AS n
                              WHERE next_run + n * recur_interval > now()
                              ORDER BY n LIMIT 1),
                            now() + recur_interval)
                        WHERE id = $1 AND recur_interval IS NOT NULL
                        """,
                        task_id,
                    )

        for kind, payload in wakes:
            if kind == "status":
                await _do_status_notify(self._pool, **payload)
            else:
                await _post_notify(payload)
        return result

    async def _assert_no_cycle(
        self,
        conn: asyncpg.Connection,
        task_id: int,
        blocker_id: int,
    ) -> None:
        """Walk the ``blocked_by`` chain from ``blocker_id``; fail if it
        comes back to ``task_id``.

        Bounded iteration (1000) guards against pathological data.
        """
        visited: set[int] = set()
        current: int | None = blocker_id
        for _ in range(1000):
            if current is None:
                return
            if current == task_id:
                raise PipelineError(
                    f"block: cycle -- task {task_id} would block itself "
                    f"transitively via {blocker_id}"
                )
            if current in visited:
                # Existing cycle elsewhere in the graph -- not our problem
                # for this block operation.
                return
            visited.add(current)
            row = await conn.fetchrow(
                "SELECT blocked_by FROM tasks WHERE id = $1", current
            )
            current = row["blocked_by"] if row else None
        raise PipelineError(
            "block: blocked_by chain too deep (>1000) -- aborting"
        )
