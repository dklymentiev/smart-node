"""Tasks repository -- CRUD only.

This repo is the storage layer. It does not enforce pipeline transitions
(R2: pipeline state machine). Status values can be set freely here;
the state-machine module wraps this repo and adds validation.

Filters cover the common operator and agent queries (status, project,
assignee, priority, objective). More elaborate filters (full-text on
title/description, tag-based) belong in the API layer.
"""

from __future__ import annotations

from datetime import date
import json
import os
import re

import asyncpg

from planner import statuses as _st
from planner.notify import fire_assignee_notify, fire_reviewer_notify
from planner.models import (
    Task,
    TaskBlockedReason,
    TaskComplexity,
    TaskPriority,
    TaskStatus,
)

_VALID_PRIORITIES = ("low", "medium", "high", "critical")
# Legacy stage names, the three states, and the two retired aliases: every
# name a writer may send as ``status``. See planner.statuses.
_VALID_STATUSES = _st.ALL_INPUT_NAMES
_VALID_COMPLEXITY = ("simple", "moderate", "complex", "unknown")
_VALID_BLOCKED_REASON = ("task_dependency", "external_approval", "scheduled_time")

_TASK_COLS = (
    "id, title, description, project_id, objective_id, assignee, requester, reviewer, "
    "priority, stage AS status, created_at, updated_at, assessed_at, approved_at, "
    "execution_started_at, completed_at, due_date, complexity_hint, scope, eta, "
    "blocked_by, blocked_reason, external_detail, previous_status, "
    "verification_attempts, workflow_id, status AS state, wait_reason, close_outcome, "
    "recur_interval::text AS recur_interval, next_run, proposed_assignee"
)

#: Words a writer may use for an interval besides Postgres interval text.
_RECUR_ALIASES = {"daily": "1 day", "weekly": "1 week", "monthly": "1 month",
                  "yearly": "1 year"}
#: Values that switch recurrence off (both columns cleared).
_RECUR_OFF = ("", "off", "none", "null", "no")


def _recur_text(value: object) -> str | None:
    """The interval text to store, or None for "not recurring"."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _RECUR_OFF:
        return None
    return _RECUR_ALIASES.get(text, text)


def _ts_text(value: object) -> str | None:
    """A timestamp as text for a ``::timestamptz`` cast (datetime or ISO string)."""
    if value is None or value == "":
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[union-attr]
    return str(value)


class TaskError(ValueError):
    """Raised on invalid task input or constraint violations."""


# Service keys that file tasks on the operator's behalf. They are not agents and
# must not be counted against the two-open-tasks cap: the runner key already held
# 120+ open tasks, so every new filing was refused (409) and the operator had no
# door left. Only the COUNT is skipped; their tasks still land inert, unassigned,
# awaiting approval. Hotfix on example-node by the owner's exception, 2026-09-19;
# the real shape (a configurable limit) belongs to the limits registry,
_CAP_EXEMPT_ACTORS = frozenset({"runner"})

# What the cap counts, and why it is not every live task. An agent's
# quota is what it has PROPOSED and the owner has not answered yet, which is
# exactly `awaiting_approval`: agent-authored work lands there and only a
# human moves it out. Counting approved work instead punished the agents who
# worked best -- on example-node one held eleven live tasks, ten of them approved
# or handed on by the owner, and could file nothing until something was
# rejected to make room.
_UNANSWERED_STATUS = "awaiting_approval"
# A guard against a runaway proposer, not a work allowance, so it sits well
# above what anyone reaches in a day. The registry takes it over.
try:
    _OPEN_TASK_CAP = max(1, int(os.environ.get("PLANNER_AGENT_OPEN_TASK_CAP", "10")))
except ValueError:
    _OPEN_TASK_CAP = 10


# A reminder or an alarm an agent sets for ITSELF. Everything else an agent files is
# inert until the owner approves it, because it can put work on someone else or
# spend someone else's time. An alarm for oneself does neither: it wakes the agent
# that asked for it, nobody else. Making it wait for a click turned "set yourself
# an alarm" into "set one and ask the owner to approve it", and the series skips
# its cycles until then. The limits keep it from becoming a runaway: not tighter
# than a quarter of an hour, and a few at a time.
_SELF_ALARM_MIN_INTERVAL = "15 minutes"
try:
    _SELF_ALARM_CAP = max(1, int(os.environ.get("PLANNER_SELF_ALARM_CAP", "3")))
except ValueError:
    _SELF_ALARM_CAP = 3


def _owner_ids() -> set[str]:
    # Imported lazily to avoid the repository/pipeline import cycle.  There is
    # one owner policy, used by creation, field updates and status transitions.
    from planner.pipeline import Pipeline
    configured = __import__("os").environ.get("PLANNER_OWNER_ACTOR_ID", "admin").strip().lower()
    return (set(Pipeline.OWNER_ACTORS) - {"admin"}) | {configured}


def _row_to_task(row: asyncpg.Record) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        project_id=row["project_id"],
        objective_id=row["objective_id"],
        assignee=row["assignee"],
        requester=row["requester"],
        reviewer=row["reviewer"],
        priority=row["priority"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        assessed_at=row["assessed_at"],
        approved_at=row["approved_at"],
        execution_started_at=row["execution_started_at"],
        completed_at=row["completed_at"],
        due_date=row["due_date"],
        complexity_hint=row["complexity_hint"],
        scope=row["scope"],
        eta=row["eta"],
        blocked_by=row["blocked_by"],
        blocked_reason=row["blocked_reason"],
        external_detail=row["external_detail"],
        previous_status=row["previous_status"],
        verification_attempts=row["verification_attempts"],
        workflow_id=row["workflow_id"],
        state=row["state"],
        wait_reason=row["wait_reason"],
        close_outcome=row["close_outcome"],
        recur_interval=row["recur_interval"],
        next_run=row["next_run"],
        proposed_assignee=row["proposed_assignee"] if "proposed_assignee" in row.keys() else None,
    )


def _check_priority(p: TaskPriority) -> None:
    if p not in _VALID_PRIORITIES:
        raise TaskError(f"invalid priority: {p!r}")


def _check_status(s: TaskStatus) -> None:
    if s not in _VALID_STATUSES:
        raise TaskError(f"invalid status: {s!r}")


def _check_complexity(c: TaskComplexity | None) -> None:
    if c is not None and c not in _VALID_COMPLEXITY:
        raise TaskError(f"invalid complexity_hint: {c!r}")


def _check_blocked_reason(r: TaskBlockedReason | None) -> None:
    if r is not None and r not in _VALID_BLOCKED_REASON:
        raise TaskError(f"invalid blocked_reason: {r!r}")


# A search of a dozen words is a pasted paragraph; the rest would only slow the query.
_SEARCH_MAX_WORDS = 8


def _min_words(n: int) -> int:
    """How many of a query's words a task must hold. A query of one or two words
    needs them all; a longer one, as an agent types it ("candidates outreach list
    document created"), needs about three in five, because one word that the text
    happens to put differently must not hide it. Tasks holding more of the words
    rank first."""
    return n if n <= 2 else -(-3 * n // 5)


class TasksRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        *,
        title: str,
        description: str | None = None,
        project_id: int | None = None,
        objective_id: int | None = None,
        assignee: str | None = None,
        requester: str | None = None,
        priority: TaskPriority = "medium",
        status: TaskStatus = "open",
        due_date: date | None = None,
        complexity_hint: TaskComplexity | None = None,
        scope: str | None = None,
        eta: str | None = None,
        workflow_id: int | None = None,
        actor_id: str | None = None,
        wait_reason: str | None = None,
        close_outcome: str | None = None,
        recur_interval: str | None = None,
        next_run: object | None = None,
    ) -> Task:
        if not title.strip():
            raise TaskError("title must not be blank")
        _check_priority(priority)
        _check_status(status)
        _check_complexity(complexity_hint)
        if (wait_reason or close_outcome) and not _st.is_state(status):
            raise TaskError("wait_reason / close_outcome go with status queued / closed, not with a legacy stage name")
        if status == "closed" and not close_outcome:
            raise TaskError("a task created closed needs a close_outcome")
        recur = _recur_text(recur_interval)
        first_run = _ts_text(next_run)
        if first_run and not recur:
            raise TaskError("next_run goes with recur_interval (a recurring task)")

        # Agent-authored work is inert until a human owner approves it.  Keep
        # this at the repository boundary so REST, MCP and UI cannot bypass it.
        proposed: str | None = None
        self_alarm = bool(
            actor_id
            and recur
            and actor_id not in _owner_ids()
            and (assignee or "").strip().lower() == actor_id.strip().lower()
        )
        if self_alarm:
            # Checked here so a refusal reads as a refusal and not as a task that
            # silently never starts.
            try:
                too_short = await self._pool.fetchval(
                    "SELECT $1::text::interval < $2::text::interval",
                    recur, _SELF_ALARM_MIN_INTERVAL,
                )
            except asyncpg.DataError as exc:
                raise TaskError(str(exc).splitlines()[0]) from exc
            if too_short:
                raise TaskError(
                    f"an alarm you set for yourself repeats at most every "
                    f"{_SELF_ALARM_MIN_INTERVAL}; {recur} is shorter. "
                    f"Ask the owner if you need it tighter."
                )
            mine = await self._pool.fetch(
                """SELECT id, title FROM tasks
                   WHERE requester = $1 AND assignee = $1
                     AND recur_interval IS NOT NULL AND status <> 'closed'
                   ORDER BY id""", actor_id,
            )
            if len(mine) >= _SELF_ALARM_CAP:
                names = ", ".join(f"#{r['id']} {r['title']}" for r in mine)
                raise TaskError(
                    f"you already have {len(mine)} repeating tasks set for yourself, "
                    f"which is the limit of {_SELF_ALARM_CAP}; end one with "
                    f"planner_recur first: {names}"
                )
            requester = actor_id
            assignee = actor_id.strip().lower()
            status = "in_progress"
        elif actor_id:
            if actor_id not in _owner_ids():
                open_rows = await self._pool.fetch(
                    """SELECT id, title FROM tasks
                       WHERE requester = $1 AND stage = $2
                       ORDER BY id""", actor_id, _UNANSWERED_STATUS
                )
                if len(open_rows) >= _OPEN_TASK_CAP and actor_id not in _CAP_EXEMPT_ACTORS:
                    names = ", ".join(f"#{r['id']} {r['title']}" for r in open_rows)
                    try:
                        await self._pool.execute(
                            """INSERT INTO task_notifications
                               (task_id, event_type, notify_target, payload)
                               VALUES (NULL, 'task_creation_refused', $1, $2::jsonb)""",
                            actor_id, json.dumps({"open_tasks": [dict(r) for r in open_rows]}),
                        )
                    except Exception:
                        # The event queue migration may not yet be applied; the
                        # refusal itself must still remain loud and atomic.
                        pass
                    raise TaskError(
                        f"agent {actor_id} has {len(open_rows)} proposals still "
                        f"awaiting the owner's answer, which is the limit of "
                        f"{_OPEN_TASK_CAP}; these are what he has to answer "
                        f"first: {names}"
                    )
                requester = actor_id
                # Not an assignee: an agent cannot hand work to a colleague on its
                # own authority. The doer it named is kept (migration 0012) and takes
                # effect when the owner approves the task.
                proposed = (assignee or "").strip().lower() or None
                assignee = None
                status = "awaiting_approval"

        # A legacy stage name is written to ``stage`` and the trigger derives
        # the state; a state is written as itself with its reason or outcome.
        if _st.is_state(status):
            status_col = "status"
            status_val: str = status
            extra_cols = ", wait_reason, close_outcome"
            extra_vals = ", $14, $15"
            extra_args: list[object] = [wait_reason, close_outcome]
        else:
            status_col = "stage"
            status_val = _st.normalize_stage(status)
            extra_cols = extra_vals = ""
            extra_args = []
        if recur:
            # The first cycle opens one interval from now unless the writer
            # says when.
            i, j = 14 + len(extra_args), 15 + len(extra_args)
            extra_cols += ", recur_interval, next_run"
            extra_vals += (f", ${i}::text::interval, "
                           f"COALESCE(${j}::text::timestamptz, now() + ${i}::text::interval)")
            extra_args += [recur, first_run]
        try:
            row = await self._pool.fetchrow(
                f"""
                INSERT INTO tasks
                    (title, description, project_id, objective_id,
                     assignee, requester, priority, {status_col}, due_date,
                     complexity_hint, scope, eta, workflow_id{extra_cols})
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13{extra_vals})
                RETURNING {_TASK_COLS}
                """,
                title,
                description,
                project_id,
                objective_id,
                assignee,
                requester,
                priority,
                status_val,
                due_date,
                complexity_hint,
                scope,
                eta,
                workflow_id,
                *extra_args,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise TaskError(f"FK violation: {exc}") from exc
        except asyncpg.CheckViolationError as exc:
            raise TaskError(str(exc).splitlines()[0]) from exc
        except asyncpg.DataError as exc:
            # A recur_interval or next_run Postgres cannot read.
            raise TaskError(str(exc).splitlines()[0]) from exc
        assert row is not None
        task = _row_to_task(row)
        if proposed:
            row = await self._pool.fetchrow(
                f"UPDATE tasks SET proposed_assignee = $1 WHERE id = $2 RETURNING {_TASK_COLS}",
                proposed, task.id)
            task = _row_to_task(row)
        if self_alarm:
            # Said once, in the task itself, why nobody approved it: the next
            # reader sees an in-progress task with no owner decision on it.
            try:
                await self._pool.execute(
                    """INSERT INTO task_comments (task_id, author, content, comment_type)
                       VALUES ($1, 'system', $2, 'recurrence')""",
                    task.id,
                    f"A repeating task {actor_id} set for itself: it started without "
                    f"the owner's approval because it wakes only {actor_id}. "
                    f"Repeats every {recur}; first cycle opens at the next due time.",
                )
            except Exception:
                pass  # the note is for readers; the task must exist either way
        # Best-effort assignee-assigned notification (never fails the op). An agent
        # that just set an alarm for itself is awake: waking it again is noise.
        if task.assignee and not self_alarm:
            fire_assignee_notify(
                assignee=task.assignee,
                task_id=task.id,
                title=task.title,
                summary=task.status,
                workflow_id=task.workflow_id,
                task_status=task.status,
            )
        return task

    async def get(self, task_id: int) -> Task | None:
        row = await self._pool.fetchrow(
            f"SELECT {_TASK_COLS} FROM tasks WHERE id = $1",
            task_id,
        )
        return _row_to_task(row) if row else None

    async def handoff(self, task_id: int, *, from_actor: str, to_actor: str,
                      notify: bool = True) -> Task | None:
        """Change owner only while it belongs to from_actor."""
        row = await self._pool.fetchrow(
            f"UPDATE tasks SET assignee = $1, updated_at = now() "
            f"WHERE id = $2 AND assignee = $3 RETURNING {_TASK_COLS}",
            to_actor, task_id, from_actor,
        )
        if row is None:
            return None
        task = _row_to_task(row)
        if notify and to_actor != from_actor:
            fire_assignee_notify(
                assignee=to_actor, task_id=task.id, title=task.title,
                summary=task.status,
            )
        return task

    async def list(
        self,
        *,
        project_id: int | None = None,
        objective_id: int | None = None,
        assignee: str | None = None,
        status: str | None = None,
        priority: TaskPriority | None = None,
        exclude_tags: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
        wait_reason: str | None = None,
        close_outcome: str | None = None,
    ) -> list[Task]:
        where: list[str] = []
        args: list[object] = []
        if project_id is not None:
            where.append(f"project_id = ${len(args) + 1}")
            args.append(project_id)
        if objective_id is not None:
            where.append(f"objective_id = ${len(args) + 1}")
            args.append(objective_id)
        if assignee is not None:
            where.append(f"assignee = ${len(args) + 1}")
            args.append(assignee)
        if status is not None:
            _check_status(status)
            if _st.is_state(status):
                where.append(f"status = ${len(args) + 1}")
                args.append(status)
            else:
                where.append(f"stage = ${len(args) + 1}")
                args.append(_st.normalize_stage(status))
        if wait_reason is not None:
            where.append(f"wait_reason = ${len(args) + 1}")
            args.append(wait_reason)
        if close_outcome is not None:
            where.append(f"close_outcome = ${len(args) + 1}")
            args.append(close_outcome)
        if priority is not None:
            _check_priority(priority)
            where.append(f"priority = ${len(args) + 1}")
            args.append(priority)
        if exclude_tags:
            where.append(
                f"NOT EXISTS (SELECT 1 FROM task_tags tt "
                f"WHERE tt.task_id = tasks.id AND tt.tag = ANY(${len(args) + 1}))"
            )
            args.append(list(exclude_tags))

        sql = f"SELECT {_TASK_COLS} FROM tasks"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC"
        args.append(limit)
        sql += f" LIMIT ${len(args)}"
        args.append(offset)
        sql += f" OFFSET ${len(args)}"
        rows = await self._pool.fetch(sql, *args)
        return [_row_to_task(r) for r in rows]

    async def _stem(self, word: str) -> str:
        """The word cut back to its stem, so a query in one grammatical form finds
        the other forms ("contacts" finds "contact", "publishing" finds "publish").
        The stem comes from the database's own English text-search dictionary and is matched as the START of a word,
        so a short word does not match inside a longer one ("eta" inside "beta").
        A stem that is not the start of the word, or a word the dictionary drops, is
        used as typed."""
        try:
            cand = await self._pool.fetchval(
                "SELECT lexeme FROM unnest(to_tsvector('english'::regconfig, $1)) LIMIT 1", word)
        except Exception:  # a dictionary problem must not cost the search
            return word
        if cand and len(cand) >= 3 and word.lower().startswith(cand):
            return word[:len(cand)]
        return word

    async def search(
        self,
        *,
        q: str,
        project_id: int | None = None,
        objective_id: int | None = None,
        author: str | None = None,
        limit: int = 50,
    ) -> list[Task]:
        """Word search across a task's own text AND its comments.

        The words of ``q`` must occur (case-insensitive, any order) somewhere in
        the task: its title, its description or one of its comments; all of them
        for a query of one or two words, about three in five for a longer one, and
        the tasks holding most of them come first. The words may sit in different
        fields, and in different comments. A caller who
        remembers a task by a few words, not by the sentence its title happens to
        be, finds it; the whole phrase still matches, as it always did.
        Punctuation in a word is text, not a pattern.

        ``author`` keeps only the tasks that name wrote a comment on or filed
        (the requester). Deduped by task, newest first, optionally scoped to a
        project or objective. This is the full-text query the module docstring
        defers to the API layer: it spans tasks + task_comments, so it lives here
        as its own method rather than bloating ``list`` with a text filter.
        """
        if not q or not q.strip():
            raise TaskError("search query must not be blank")
        words = q.split()[:_SEARCH_MAX_WORDS]
        where: list[str] = []
        args: list[object] = []
        conds: list[str] = []
        for w in words:
            stem = await self._stem(w)
            args.append("\\m" + re.sub(r"[^\w]", lambda m: "\\" + m.group(0), stem))
            n = len(args)
            conds.append(
                f"(t.title ~* ${n} OR t.description ~* ${n} "
                "OR EXISTS (SELECT 1 FROM task_comments c "
                f"WHERE c.task_id = t.id AND c.content ~* ${n}))"
            )
        score = " + ".join(f"(CASE WHEN {c} THEN 1 ELSE 0 END)" for c in conds)
        where.append(f"({score}) >= {_min_words(len(conds))}")
        if author and author.strip():
            args.append(author.strip().lower())
            n = len(args)
            where.append(
                f"(lower(t.requester) = ${n} OR EXISTS (SELECT 1 FROM task_comments ca "
                f"WHERE ca.task_id = t.id AND lower(ca.author) = ${n}))"
            )
        if project_id is not None:
            where.append(f"t.project_id = ${len(args) + 1}")
            args.append(project_id)
        if objective_id is not None:
            where.append(f"t.objective_id = ${len(args) + 1}")
            args.append(objective_id)
        cols = ", ".join(f"t.{c.strip()}" for c in _TASK_COLS.split(","))
        sql = f"SELECT {cols} FROM tasks t WHERE " + " AND ".join(where)
        sql += f" ORDER BY ({score}) DESC, t.created_at DESC, t.id DESC"
        args.append(limit)
        sql += f" LIMIT ${len(args)}"
        rows = await self._pool.fetch(sql, *args)
        return [_row_to_task(r) for r in rows]

    async def update(
        self,
        task_id: int,
        actor_id: str | None = None,
        **fields: object,
    ) -> Task | None:
        """Generic field update.

        Accepts any whitelisted column name. Pipeline-aware updates (status
        transitions, verification_attempts increments, etc.) live in the
        state-machine module on top of this repo.
        """
        allowed = {
            "title",
            "description",
            "project_id",
            "objective_id",
            "assignee",
            "requester",
            "reviewer",
            "priority",
            "status",
            "due_date",
            "complexity_hint",
            "scope",
            "eta",
            "blocked_by",
            "blocked_reason",
            "external_detail",
            "previous_status",
            "verification_attempts",
            "assessed_at",
            "execution_started_at",
            "completed_at",
            "workflow_id",
            "wait_reason",
            "close_outcome",
            "recur_interval",
            "next_run",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise TaskError(f"unknown task fields: {sorted(unknown)}")

        if "priority" in fields:
            _check_priority(fields["priority"])  # type: ignore[arg-type]
        if "status" in fields:
            _check_status(fields["status"])  # type: ignore[arg-type]
        if "complexity_hint" in fields:
            _check_complexity(fields["complexity_hint"])  # type: ignore[arg-type]
        if "blocked_reason" in fields:
            _check_blocked_reason(fields["blocked_reason"])  # type: ignore[arg-type]
        if "title" in fields and not str(fields["title"]).strip():
            raise TaskError("title must not be blank")
        # A legacy stage name goes to the ``stage`` column (the trigger derives
        # the state); one of the three states goes to ``status`` as itself.
        if "status" in fields and not _st.is_state(fields["status"]):
            fields = dict(fields)
            fields["stage"] = _st.normalize_stage(str(fields.pop("status")))

        if not fields:
            return await self.get(task_id)

        # Capture the prior assignee/reviewer so we can detect a CHANGE after
        # the update commits (notify only when the field actually changed).
        # Fetched up-front even when only the actor_id is provided so the
        # wake-gate can read the pre-change status/approval row, which is what
        # decides whether a reassignment is a pre-approval handoff.
        old_assignee: object = None
        old_reviewer: object = None
        prior_requester: object = None
        prior_approved_at: object = None
        prior_status: object = None
        if "assignee" in fields or "reviewer" in fields or actor_id:
            prior = await self._pool.fetchrow(
                "SELECT assignee, reviewer, requester, approved_at, stage AS status "
                "FROM tasks WHERE id = $1",
                task_id,
            )
            if prior:
                old_assignee = prior["assignee"]
                old_reviewer = prior["reviewer"]
                prior_requester = prior["requester"]
                prior_approved_at = prior["approved_at"]
                prior_status = prior["status"]

        sets: list[str] = []
        args: list[object] = []
        fields = dict(fields)
        recur_given = "recur_interval" in fields
        recur = _recur_text(fields.pop("recur_interval", None))
        run_given = "next_run" in fields
        run = _ts_text(fields.pop("next_run", None))
        if recur_given and recur is None:
            # Off: the series ends, both columns go together.
            sets += ["recur_interval = NULL", "next_run = NULL"]
        elif recur_given:
            args.append(recur)
            i = len(args)
            args.append(run)
            # A new interval keeps the series' next cycle unless one is given;
            # a task that was not recurring starts one interval from now.
            sets += [f"recur_interval = ${i}::text::interval",
                     f"next_run = COALESCE(${i + 1}::text::timestamptz, next_run, "
                     f"now() + ${i}::text::interval)"]
        elif run_given:
            if run is None:
                raise TaskError("next_run cannot be cleared alone; "
                                "set recur_interval to off to stop the recurrence")
            args.append(run)
            sets.append(f"next_run = ${len(args)}::text::timestamptz")
        for key, value in fields.items():
            sets.append(f"{key} = ${len(args) + 1}")
            args.append(value)
        sets.append("updated_at = now()")

        args.append(task_id)
        try:
            row = await self._pool.fetchrow(
                f"""
                UPDATE tasks SET {", ".join(sets)}
                WHERE id = ${len(args)}
                RETURNING {_TASK_COLS}
                """,
                *args,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise TaskError(f"FK violation: {exc}") from exc
        except asyncpg.CheckViolationError as exc:
            # the 0004 BEFORE UPDATE trigger rejects an invalid status transition;
            # its message already lists the allowed next states -- surface it as a 400.
            # tasks_recur_next_run_paired: next_run on a task with no recur_interval.
            if "tasks_recur_next_run_paired" in str(exc):
                raise TaskError("next_run goes with recur_interval (a recurring task)") from exc
            raise TaskError(str(exc).splitlines()[0]) from exc
        except asyncpg.DataError as exc:
            raise TaskError(str(exc).splitlines()[0]) from exc
        if row is None:
            return None
        task = _row_to_task(row)
        # Best-effort notify when the assignee or reviewer was set/changed to
        # a new value. The wake-gate (pre-approval handoff by a non-owner)
        # uses the PRIOR status: a transition out of awaiting_approval already
        # advanced the status for this write, so reading the fresh row would
        # mis-apply the gate on precisely the handoff that is being approved.
        # See planner.pipeline.should_suppress_wake for the full argument.
        from planner.pipeline import should_suppress_wake  # local to avoid cycle
        owner_set = _owner_ids()
        gate_status = prior_status if prior_status is not None else getattr(task, "status", None)
        suppress_wake = should_suppress_wake(
            actor=actor_id,
            requester=prior_requester,  # type: ignore[arg-type]
            approved_at=prior_approved_at,
            task_status=gate_status,    # type: ignore[arg-type]
            owner_actors=owner_set,
        )
        if (
            "assignee" in fields
            and task.assignee
            and task.assignee != old_assignee
            and not suppress_wake
        ):
            fire_assignee_notify(
                assignee=task.assignee,
                task_id=task.id,
                title=task.title,
                summary=task.status,
                workflow_id=task.workflow_id,
                task_status=task.status,
            )
        # Reviewer change wakes the new reviewer, symmetric to the assignee
        # path. Prior behaviour: a reviewer swap on a task already in
        # pending_review left the new reviewer uninformed, so a decision
        # could sit for days with the right person watching the wrong queue.
        if (
            "reviewer" in fields
            and task.reviewer
            and task.reviewer != old_reviewer
            and not suppress_wake
        ):
            fire_reviewer_notify(
                reviewer=task.reviewer,
                task_id=task.id,
                title=task.title,
                summary=task.status,
            )
        return task

    async def delete(self, task_id: int) -> bool:
        result = await self._pool.execute(
            "DELETE FROM tasks WHERE id = $1",
            task_id,
        )
        return result.endswith(" 1")

    async def count_by_status(
        self,
        *,
        project_id: int | None = None,
    ) -> dict[str, int]:
        sql = "SELECT stage AS status, COUNT(*) AS n FROM tasks"
        args: list[object] = []
        if project_id is not None:
            sql += " WHERE project_id = $1"
            args.append(project_id)
        sql += " GROUP BY stage"
        rows = await self._pool.fetch(sql, *args)
        return {r["status"]: r["n"] for r in rows}
