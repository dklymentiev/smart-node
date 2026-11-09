"""Ancillary task repositories.

Seven small tables hang off ``tasks``:

- ``task_comments`` -- discussion thread
- ``task_relations`` -- parent/child/hotfix/postmortem links
- ``task_commits`` -- git commits associated with the task
- ``task_log`` -- append-only audit trail
- ``task_tags`` -- many-to-many tag membership
- ``task_links`` -- arbitrary external URLs (PR, ticket, doc)
- ``task_notifications`` -- outbound event queue (consumers set fired_at)

Each table gets its own small repo class. Logic stays thin -- these are
write-mostly stores; reads happen inside task detail / dashboard queries.
"""

from __future__ import annotations

import json

import asyncpg

from planner.models import (
    RelationType,
    TaskComment,
    TaskCommit,
    TaskLink,
    TaskLogEntry,
    TaskNotification,
    TaskRelation,
)

_VALID_RELATIONS = ("parent", "child", "hotfix", "postmortem")


class AncillaryError(ValueError):
    """Raised on invalid input to ancillary repos."""


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


class CommentsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def add(
        self,
        task_id: int,
        *,
        author: str,
        content: str,
        comment_type: str | None = None,
    ) -> TaskComment:
        if not content.strip():
            raise AncillaryError("comment content must not be blank")
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO task_comments (task_id, author, content, comment_type)
                VALUES ($1, $2, $3, $4)
                RETURNING id, task_id, author, content, comment_type, created_at
                """,
                task_id,
                author,
                content,
                comment_type,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise AncillaryError(f"FK violation: {exc}") from exc
        assert row is not None
        return TaskComment(**dict(row))

    async def list_for_task(
        self,
        task_id: int,
        *,
        comment_id: int | None = None,
        last: int | None = None,
        q: str | None = None,
    ) -> list[TaskComment]:
        """The log of a task, oldest first. Three ways to ask for less than all
        of it: one comment by id, the newest `last`, the ones containing `q`.

        A reader with a bounded window needs them. An agent was called into
        a task by its thirteenth comment, its runtime showed it the first
        8000 characters of the log, which was two comments, and it reported
        that the comment it was called for did not exist.
        """
        where = ["task_id = $1"]
        args: list[object] = [task_id]
        if comment_id is not None:
            args.append(comment_id)
            where.append(f"id = ${len(args)}")
        if q is not None and q.strip():
            # The text is data, not a pattern: a % or _ in it matches itself.
            args.append(q.strip())
            where.append(f"position(lower(${len(args)}) in lower(content)) > 0")
        sql = (
            "SELECT id, task_id, author, content, comment_type, created_at "
            "FROM task_comments WHERE " + " AND ".join(where)
        )
        if last is not None:
            args.append(last)
            sql = (
                "SELECT * FROM (" + sql + f" ORDER BY created_at DESC, id DESC LIMIT ${len(args)}) newest "
                "ORDER BY created_at, id"
            )
        else:
            sql += " ORDER BY created_at, id"
        rows = await self._pool.fetch(sql, *args)
        return [TaskComment(**dict(r)) for r in rows]

    async def delete(self, comment_id: int) -> bool:
        result = await self._pool.execute(
            "DELETE FROM task_comments WHERE id = $1", comment_id
        )
        return result.endswith(" 1")


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


class RelationsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def link(
        self,
        task_id: int,
        related_id: int,
        relation: RelationType,
    ) -> TaskRelation:
        if relation not in _VALID_RELATIONS:
            raise AncillaryError(f"invalid relation: {relation!r}")
        if task_id == related_id:
            raise AncillaryError("task cannot relate to itself")
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO task_relations (task_id, related_id, relation)
                VALUES ($1, $2, $3)
                RETURNING id, task_id, related_id, relation, created_at
                """,
                task_id,
                related_id,
                relation,
            )
        except asyncpg.UniqueViolationError as exc:
            raise AncillaryError(
                f"relation {relation!r} between {task_id} and {related_id} already exists"
            ) from exc
        except asyncpg.ForeignKeyViolationError as exc:
            raise AncillaryError(f"FK violation: {exc}") from exc
        assert row is not None
        return TaskRelation(**dict(row))

    async def unlink(self, relation_id: int) -> bool:
        result = await self._pool.execute(
            "DELETE FROM task_relations WHERE id = $1", relation_id
        )
        return result.endswith(" 1")

    async def list_for_task(
        self,
        task_id: int,
        *,
        relation: RelationType | None = None,
        as_target: bool = False,
    ) -> list[TaskRelation]:
        col = "related_id" if as_target else "task_id"
        sql = f"SELECT id, task_id, related_id, relation, created_at FROM task_relations WHERE {col} = $1"
        args: list[object] = [task_id]
        if relation is not None:
            if relation not in _VALID_RELATIONS:
                raise AncillaryError(f"invalid relation: {relation!r}")
            sql += " AND relation = $2"
            args.append(relation)
        sql += " ORDER BY created_at, id"
        rows = await self._pool.fetch(sql, *args)
        return [TaskRelation(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------


class CommitsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def add(
        self,
        task_id: int,
        *,
        repo: str | None = None,
        commit_hash: str | None = None,
        message: str | None = None,
        author: str | None = None,
        content_body: str | None = None,
    ) -> TaskCommit:
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO task_commits
                    (task_id, repo, commit_hash, message, author, content_body)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, task_id, repo, commit_hash, message, author,
                          content_body, committed_at
                """,
                task_id,
                repo,
                commit_hash,
                message,
                author,
                content_body,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise AncillaryError(f"FK violation: {exc}") from exc
        assert row is not None
        return TaskCommit(**dict(row))

    async def list_for_task(self, task_id: int) -> list[TaskCommit]:
        rows = await self._pool.fetch(
            """
            SELECT id, task_id, repo, commit_hash, message, author,
                   content_body, committed_at
            FROM task_commits
            WHERE task_id = $1
            ORDER BY committed_at, id
            """,
            task_id,
        )
        return [TaskCommit(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Log (audit)
# ---------------------------------------------------------------------------


class LogRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def append(
        self,
        task_id: int,
        action: str,
        *,
        old_value: str | None = None,
        new_value: str | None = None,
        actor: str = "system",
    ) -> TaskLogEntry:
        if not action.strip():
            raise AncillaryError("action must not be blank")
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO task_log (task_id, action, old_value, new_value, actor)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, task_id, action, old_value, new_value, actor, created_at
                """,
                task_id,
                action,
                old_value,
                new_value,
                actor,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise AncillaryError(f"FK violation: {exc}") from exc
        assert row is not None
        return TaskLogEntry(**dict(row))

    async def list_for_task(self, task_id: int, *, limit: int = 200) -> list[TaskLogEntry]:
        rows = await self._pool.fetch(
            """
            SELECT id, task_id, action, old_value, new_value, actor, created_at
            FROM task_log
            WHERE task_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            task_id,
            limit,
        )
        return [TaskLogEntry(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TagsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def add(self, task_id: int, tag: str) -> bool:
        if not tag.strip():
            raise AncillaryError("tag must not be blank")
        try:
            await self._pool.execute(
                "INSERT INTO task_tags (task_id, tag) VALUES ($1, $2)",
                task_id,
                tag,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False
        except asyncpg.ForeignKeyViolationError as exc:
            raise AncillaryError(f"FK violation: {exc}") from exc

    async def remove(self, task_id: int, tag: str) -> bool:
        result = await self._pool.execute(
            "DELETE FROM task_tags WHERE task_id = $1 AND tag = $2",
            task_id,
            tag,
        )
        return result.endswith(" 1")

    async def list_for_task(self, task_id: int) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT tag FROM task_tags WHERE task_id = $1 ORDER BY tag",
            task_id,
        )
        return [r["tag"] for r in rows]

    async def tasks_with_tag(self, tag: str) -> list[int]:
        rows = await self._pool.fetch(
            "SELECT task_id FROM task_tags WHERE tag = $1 ORDER BY task_id",
            tag,
        )
        return [r["task_id"] for r in rows]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


class LinksRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def add(
        self,
        task_id: int,
        *,
        kind: str,
        url: str,
        label: str | None = None,
    ) -> TaskLink:
        if not kind.strip():
            raise AncillaryError("kind must not be blank")
        if not url.strip():
            raise AncillaryError("url must not be blank")
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO task_links (task_id, kind, url, label)
                VALUES ($1, $2, $3, $4)
                RETURNING id, task_id, kind, url, label, created_at
                """,
                task_id,
                kind,
                url,
                label,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise AncillaryError(f"FK violation: {exc}") from exc
        assert row is not None
        return TaskLink(**dict(row))

    async def list_for_task(self, task_id: int) -> list[TaskLink]:
        rows = await self._pool.fetch(
            """
            SELECT id, task_id, kind, url, label, created_at
            FROM task_links
            WHERE task_id = $1
            ORDER BY created_at, id
            """,
            task_id,
        )
        return [TaskLink(**dict(r)) for r in rows]

    async def remove(self, link_id: int) -> bool:
        result = await self._pool.execute(
            "DELETE FROM task_links WHERE id = $1",
            link_id,
        )
        return result.endswith(" 1")


# ---------------------------------------------------------------------------
# Notifications (outbound event queue)
# ---------------------------------------------------------------------------


class NotificationsRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def enqueue(
        self,
        task_id: int,
        *,
        event_type: str,
        notify_target: str,
        payload: dict | None = None,
    ) -> TaskNotification:
        if not event_type.strip():
            raise AncillaryError("event_type must not be blank")
        if not notify_target.strip():
            raise AncillaryError("notify_target must not be blank")
        payload_json = json.dumps(payload) if payload is not None else None
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO task_notifications
                    (task_id, event_type, notify_target, payload)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING id, task_id, event_type, notify_target, payload,
                          created_at, fired_at
                """,
                task_id,
                event_type,
                notify_target,
                payload_json,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise AncillaryError(f"FK violation: {exc}") from exc
        assert row is not None
        return self._row_to_notification(row)

    async def pending(self, *, limit: int = 100) -> list[TaskNotification]:
        rows = await self._pool.fetch(
            """
            SELECT id, task_id, event_type, notify_target, payload,
                   created_at, fired_at
            FROM task_notifications
            WHERE fired_at IS NULL
            ORDER BY created_at, id
            LIMIT $1
            """,
            limit,
        )
        return [self._row_to_notification(r) for r in rows]

    async def mark_fired(self, notification_id: int) -> bool:
        result = await self._pool.execute(
            "UPDATE task_notifications SET fired_at = now() WHERE id = $1 AND fired_at IS NULL",
            notification_id,
        )
        return result.endswith(" 1")

    @staticmethod
    def _row_to_notification(row: asyncpg.Record) -> TaskNotification:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return TaskNotification(
            id=row["id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            notify_target=row["notify_target"],
            payload=payload,
            created_at=row["created_at"],
            fired_at=row["fired_at"],
        )
