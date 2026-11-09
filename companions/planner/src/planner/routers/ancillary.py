"""REST endpoints for ancillary task data (comments / relations / commits / log / tags / links)."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from planner.auth import ApiKey, require_api_key
from planner.mentions import announce_mentions
from planner.pipeline import Pipeline
from planner.repos.ancillary import (
    CommentsRepo,
    CommitsRepo,
    LinksRepo,
    LogRepo,
    NotificationsRepo,
    RelationsRepo,
    TagsRepo,
)
from planner.schemas import (
    CommentIn,
    CommentOut,
    CommitIn,
    CommitOut,
    LinkIn,
    LinkOut,
    LogEntryOut,
    RelationIn,
    RelationOut,
    TagIn,
)

router = APIRouter(
    prefix="/api/v1/tasks/{task_id}",
    tags=["ancillary"],
    dependencies=[Depends(require_api_key)],
)


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@router.post("/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
async def add_comment(
    task_id: int,
    body: CommentIn,
    request: Request,
    api_key: ApiKey = Depends(require_api_key),
) -> CommentOut:
    pool = _pool(request)
    repo = CommentsRepo(pool)
    c = await repo.add(
        task_id,
        author=api_key.actor_id,
        content=body.content,
        comment_type=body.comment_type,
    )
    # Re-trigger hook: if this comment carries a scope
    # gate marker, check whether both markers are now present on a task that is
    # already in awaiting_approval.  If so, fire the owner notification that the
    # gate suppressed when the status changed without both markers in place.
    _MARKER_PREFIXES = (
        Pipeline.PM_PROPOSAL_PREFIX,
        Pipeline.ARCHITECT_REVIEW_PREFIX,
    )
    if any(body.content.startswith(p) for p in _MARKER_PREFIXES):
        await Pipeline(pool).scope_gate_fire_if_ready(task_id)
    # A comment that names an actor with @name tells that actor. This is what
    # lets a comment reach somebody when no field of the task changes.
    await announce_mentions(
        pool, task_id, comment_id=c.id, author=api_key.actor_id, content=body.content
    )
    return CommentOut.model_validate(c)


@router.get("/comments", response_model=list[CommentOut])
async def list_comments(
    task_id: int,
    request: Request,
    comment_id: int | None = Query(default=None, ge=1),
    last: int | None = Query(default=None, ge=1, le=200),
    q: str | None = Query(default=None, max_length=200),
) -> list[CommentOut]:
    repo = CommentsRepo(_pool(request))
    found = await repo.list_for_task(task_id, comment_id=comment_id, last=last, q=q)
    return [CommentOut.model_validate(c) for c in found]


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


@router.post("/relations", response_model=RelationOut, status_code=status.HTTP_201_CREATED)
async def link_relation(task_id: int, body: RelationIn, request: Request) -> RelationOut:
    repo = RelationsRepo(_pool(request))
    r = await repo.link(task_id, body.related_id, body.relation)
    return RelationOut.model_validate(r)


@router.get("/relations", response_model=list[RelationOut])
async def list_relations(task_id: int, request: Request) -> list[RelationOut]:
    repo = RelationsRepo(_pool(request))
    return [RelationOut.model_validate(r) for r in await repo.list_for_task(task_id)]


@router.delete("/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_relation(
    task_id: int, relation_id: int, request: Request
) -> None:
    repo = RelationsRepo(_pool(request))
    if not await repo.unlink(relation_id):
        raise HTTPException(status_code=404, detail=f"relation {relation_id} not found")


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------


@router.post("/commits", response_model=CommitOut, status_code=status.HTTP_201_CREATED)
async def add_commit(task_id: int, body: CommitIn, request: Request) -> CommitOut:
    repo = CommitsRepo(_pool(request))
    c = await repo.add(task_id, **body.model_dump(exclude_unset=True))
    return CommitOut.model_validate(c)


@router.get("/commits", response_model=list[CommitOut])
async def list_commits(task_id: int, request: Request) -> list[CommitOut]:
    repo = CommitsRepo(_pool(request))
    return [CommitOut.model_validate(c) for c in await repo.list_for_task(task_id)]


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


@router.get("/log", response_model=list[LogEntryOut])
async def list_log(
    task_id: int, request: Request, limit: int = 200
) -> list[LogEntryOut]:
    repo = LogRepo(_pool(request))
    return [
        LogEntryOut.model_validate(e)
        for e in await repo.list_for_task(task_id, limit=limit)
    ]


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


@router.post("/tags", status_code=status.HTTP_204_NO_CONTENT)
async def add_tag(task_id: int, body: TagIn, request: Request) -> None:
    repo = TagsRepo(_pool(request))
    await repo.add(task_id, body.tag)


@router.get("/tags", response_model=list[str])
async def list_tags(task_id: int, request: Request) -> list[str]:
    repo = TagsRepo(_pool(request))
    return await repo.list_for_task(task_id)


@router.delete("/tags/{tag}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tag(task_id: int, tag: str, request: Request) -> None:
    repo = TagsRepo(_pool(request))
    if not await repo.remove(task_id, tag):
        raise HTTPException(status_code=404, detail=f"tag {tag!r} not found on task")


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@router.post("/links", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
async def add_link(task_id: int, body: LinkIn, request: Request) -> LinkOut:
    repo = LinksRepo(_pool(request))
    lk = await repo.add(task_id, kind=body.kind, url=body.url, label=body.label)
    return LinkOut.model_validate(lk)


@router.get("/links", response_model=list[LinkOut])
async def list_links(task_id: int, request: Request) -> list[LinkOut]:
    repo = LinksRepo(_pool(request))
    return [LinkOut.model_validate(lk) for lk in await repo.list_for_task(task_id)]


@router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_link(task_id: int, link_id: int, request: Request) -> None:
    repo = LinksRepo(_pool(request))
    if not await repo.remove(link_id):
        raise HTTPException(status_code=404, detail=f"link {link_id} not found")
