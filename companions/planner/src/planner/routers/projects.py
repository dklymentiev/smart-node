"""REST endpoints for projects."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status

from planner.auth import require_api_key
from planner.models import ProjectStatus
from planner.repos.projects import ProjectsRepo
from planner.schemas import ProjectIn, ProjectOut, ProjectPatch

router = APIRouter(
    prefix="/api/v1/projects",
    tags=["projects"],
    dependencies=[Depends(require_api_key)],
)


def _repo(request: Request) -> ProjectsRepo:
    pool: asyncpg.Pool = request.app.state.pool
    return ProjectsRepo(pool)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectIn, request: Request) -> ProjectOut:
    project = await _repo(request).create(
        body.slug,
        body.title,
        description=body.description,
        sort_order=body.sort_order,
    )
    return ProjectOut.model_validate(project)


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    request: Request,
    status_filter: ProjectStatus | None = None,
) -> list[ProjectOut]:
    rows = await _repo(request).list(status=status_filter)
    return [ProjectOut.model_validate(r) for r in rows]


@router.get("/by-slug/{slug}", response_model=ProjectOut)
async def get_project_by_slug(slug: str, request: Request) -> ProjectOut:
    project = await _repo(request).get_by_slug(slug)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {slug!r} not found")
    return ProjectOut.model_validate(project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, request: Request) -> ProjectOut:
    project = await _repo(request).get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    return ProjectOut.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int, body: ProjectPatch, request: Request
) -> ProjectOut:
    fields = body.model_dump(exclude_unset=True)
    project = await _repo(request).update(project_id, **fields)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    return ProjectOut.model_validate(project)


@router.delete("/{project_id}", response_model=ProjectOut)
async def archive_project(project_id: int, request: Request) -> ProjectOut:
    project = await _repo(request).archive(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"project {project_id} not found")
    return ProjectOut.model_validate(project)
