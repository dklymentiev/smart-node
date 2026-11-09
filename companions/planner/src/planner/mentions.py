"""Mentions in task comments: ``@name`` addresses a comment to an actor.

WHY THIS EXISTS. A comment used to tell nobody. The only things that reached an
actor were a change of the task's assignee, reviewer or status, so a comment
written FOR somebody waited until that somebody happened to open the task. The
case that made it matter: a workflow hands two blocks in a row to the same
agent. The second work order is a new comment on a task whose assignee and
status do not change, so nothing was sent and the flow stood still until the
agent was woken by hand (live run of 2026-10-10).

A mention makes the comment itself the thing that tells. The planner does not
decide HOW an actor is told: it sends the same notify the assignee path sends,
and the node delivers it the way it delivers any other (a message in an agent's
inbox, mail or chat for a person).

What counts as a mention: ``@`` followed by the exact id of an active actor,
outside code and outside quoted text. A name that is no actor is not a mention
and is ignored without a word: comments are prose and ``@`` appears in prose.
"""

from __future__ import annotations

import logging
import re

import asyncpg

from planner.notify import fire_mention_notify

logger = logging.getLogger(__name__)

#: ``@name`` not glued to a word before it, so an e-mail address is not a mention.
_MENTION_RE = re.compile(r"(?<![\w@./-])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_FENCES = ("```", "~~~")

#: How much of the comment travels with the notice.
_EXCERPT_CHARS = 200


def mentioned_names(text: str | None) -> list[str]:
    """The names a comment mentions, in order, each once.

    Lines inside a fenced code block and quoted lines (``>``) are skipped, and
    so is inline code: a name there is being shown, not addressed. A full stop
    that ends the sentence is not part of the name.
    """
    names: list[str] = []
    fenced = False
    for line in (text or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_FENCES):
            fenced = not fenced
            continue
        if fenced or stripped.startswith(">"):
            continue
        for match in _MENTION_RE.finditer(_INLINE_CODE_RE.sub("", line)):
            name = match.group(1).rstrip(".")
            if name and name not in names:
                names.append(name)
    return names


def excerpt(text: str | None) -> str:
    """The comment on one line, cut to what a notice can carry."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _EXCERPT_CHARS else flat[: _EXCERPT_CHARS - 3] + "..."


async def announce_mentions(
    pool: asyncpg.Pool,
    task_id: int,
    *,
    comment_id: int,
    author: str,
    content: str,
) -> list[str]:
    """Tell every actor the comment mentions. Returns the actors told.

    Not told: the author (nobody is notified of their own words), an actor of
    type ``system`` (machinery has no inbox), an inactive actor. Withheld
    entirely while the task still waits for the owner's approval and neither
    its author nor its requester is an owner: the same gate that withholds an
    assignment (``should_suppress_wake``), so a mention is not a way around it.

    Best-effort, like every other notify: the comment is already written, and a
    fault here must not turn a written comment into an error the caller retries.
    """
    names = mentioned_names(content)
    if not names:
        return []
    try:
        # Imported here: pipeline and repos import each other at module level.
        from planner.pipeline import should_suppress_wake
        from planner.repos.tasks import _owner_ids

        task = await pool.fetchrow(
            "SELECT title, stage AS status, requester, approved_at FROM tasks WHERE id = $1", task_id
        )
        if task is None:
            return []
        if should_suppress_wake(
            actor=author,
            requester=task["requester"],
            approved_at=task["approved_at"],
            task_status=task["status"],
            owner_actors=_owner_ids(),
        ):
            logger.info("task %s: mentions %s withheld until the owner approves the task",
                        task_id, names)
            return []
        rows = await pool.fetch(
            "SELECT id FROM actors WHERE id = ANY($1::text[]) AND active AND type <> 'system'",
            names,
        )
        known = {row["id"] for row in rows}
        told = [name for name in names if name in known and name != author]
        for actor in told:
            fire_mention_notify(
                actor=actor,
                task_id=task_id,
                title=task["title"],
                summary="comment %s by %s: %s" % (comment_id, author, excerpt(content)),
            )
        return told
    except Exception:  # noqa: BLE001 - see the docstring: never fail a written comment
        logger.exception("task %s: could not announce the mentions in comment %s",
                         task_id, comment_id)
        return []
