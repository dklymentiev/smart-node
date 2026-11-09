"""Specialists CRUD — sn_config.specialists.

Specialists are reusable role/personality records. One specialist can be
referenced by multiple agents via agents.specialist_id.
"""

import logging
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query

from db import cursor
from permissions import requires
from schemas.specialist import SpecialistCreate, SpecialistRead, SpecialistUpdate

log = logging.getLogger("core-api.specialists")

router = APIRouter(prefix="/api/specialists", tags=["specialists"])

LIBRARY_DIR = Path("/srv/smart-node/specialists")
LIBRARY_SUMMARY_MAX = 240


def _front_matter(raw: str) -> dict:
    """The `key: value` lines between the leading `---` fences of a role file."""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        k, sep, v = line.partition(":")
        if sep and k.strip() and not k.startswith((" ", "#")):
            out[k.strip()] = v.split(" #", 1)[0].strip()
    return {}


def _parse_library_file(path: Path) -> dict:
    slug = path.stem
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm = _front_matter(raw)
    if fm:
        # a catalog role: its front matter says what it is (slug, name, archetype, category, tagline)
        tagline = fm.get("tagline", "")
        return {"slug": fm.get("slug") or slug, "title": fm.get("name") or slug, "summary": tagline,
                "archetype": fm.get("archetype", ""), "category": fm.get("category", ""),
                "tagline": tagline, "body_md": raw, "size": len(raw)}
    title = slug
    summary = ""
    lines = raw.splitlines()
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("# "):
            title = s[2:].strip() or slug
            body_start = i + 1
            break
    for line in lines[body_start:]:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            if summary:
                break
            continue
        summary = (summary + " " + s).strip() if summary else s
        if len(summary) >= LIBRARY_SUMMARY_MAX:
            break
    summary = re.sub(r"\s+", " ", summary)
    if len(summary) > LIBRARY_SUMMARY_MAX:
        summary = summary[: LIBRARY_SUMMARY_MAX - 1].rstrip() + "…"
    return {
        "slug": slug,
        "title": title,
        "summary": summary,
        "body_md": raw,
        "size": len(raw),
    }


@router.get("/library", dependencies=[Depends(requires("specialists.read"))])
def list_specialist_library():
    """Filesystem-backed library of reusable specialists.

    Source of truth: markdown files in /srv/smart-node/specialists/.
    Distinct from sn_config.specialists (which holds agent-bound copies).
    """
    if not LIBRARY_DIR.is_dir():
        return {"specialists": [], "source": str(LIBRARY_DIR)}
    items = []
    for path in sorted(LIBRARY_DIR.glob("*.md")):
        if path.stem.lower() in {"readme", "index"}:
            continue
        try:
            item = _parse_library_file(path)
        except Exception as exc:
            log.warning("library parse failed for %s: %s", path, exc)
            continue
        # the list says what each role is; its text comes from /library/{slug}
        # (all bodies at once were about 600 KB for a hundred roles)
        item.pop("body_md", None)
        items.append(item)
    return {"specialists": items, "source": str(LIBRARY_DIR)}


@router.get("/library/{slug}", dependencies=[Depends(requires("specialists.read"))])
def get_specialist_library_role(slug: Annotated[str, PathParam(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]):
    """One role of the library with its full text, to read before an agent is made from it."""
    path = LIBRARY_DIR / f"{slug}.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no role '{slug}' in the library")
    return _parse_library_file(path)

COLS = ("id, slug, name, body_md, archetype, category, version, tenant_id, "
        "created_at, updated_at, created_by, updated_by")


def _row_to_read(row) -> SpecialistRead:
    return SpecialistRead(
        id=row["id"], slug=row["slug"], name=row["name"], body_md=row["body_md"],
        archetype=row["archetype"], category=row["category"], version=row["version"], tenant_id=row["tenant_id"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        created_by=row["created_by"], updated_by=row["updated_by"],
    )


def _bump_version(current: str, kind: str) -> str:
    try:
        a, b, c = (int(x) for x in current.split("."))
    except Exception:
        a, b, c = 0, 1, 0
    if kind == "arch":
        return f"{a+1}.0.0"
    if kind == "minor":
        return f"{a}.{b+1}.0"
    return f"{a}.{b}.{c+1}"


@router.get("", dependencies=[Depends(requires("specialists.read"))])
def list_specialists(tenant_id: int | None = Query(default=None)):
    with cursor() as cur:
        if tenant_id is None:
            cur.execute(
                f"SELECT {COLS} FROM sn_config.specialists "
                "WHERE archived_at IS NULL ORDER BY slug"
            )
        else:
            cur.execute(
                f"SELECT {COLS} FROM sn_config.specialists "
                "WHERE archived_at IS NULL AND tenant_id IS NOT DISTINCT FROM %s "
                "ORDER BY slug",
                (tenant_id,),
            )
        rows = cur.fetchall()
    return {"specialists": [_row_to_read(r).model_dump(mode="json") for r in rows]}


@router.get("/{slug}", dependencies=[Depends(requires("specialists.read"))])
def get_specialist(slug: Annotated[str, PathParam(min_length=2, max_length=61)]):
    with cursor() as cur:
        cur.execute(
            f"SELECT {COLS} FROM sn_config.specialists "
            "WHERE slug=%s AND archived_at IS NULL",
            (slug,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"specialist not found: {slug}")
    return _row_to_read(row).model_dump(mode="json")


@router.post("", status_code=201, dependencies=[Depends(requires("specialists.write"))])
def create_specialist(body: SpecialistCreate, created_by: str = "panel"):
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_config.specialists "
            "(slug, name, body_md, archetype, tenant_id, created_by, updated_by) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {COLS}",
            (body.slug, body.name, body.body_md, body.archetype, body.tenant_id, created_by, created_by),
        )
        row = cur.fetchone()
    return _row_to_read(row).model_dump(mode="json")


def _write_target(cur, slug: str, specialist_id: int | None) -> dict:
    """The one active specialist a write by slug is meant for.

    A slug is not unique: a node can hold the shipped template and its own copy
    under the same slug (different tenant). A write by slug used to match every
    active row, so a prompt edit meant for one silently rewrote the other. Now two
    matches are refused, naming the ids, and the caller picks one with
    ?specialist_id=. Reads by slug are left as they were.
    """
    cur.execute(
        "SELECT id, version FROM sn_config.specialists "
        "WHERE slug=%s AND archived_at IS NULL AND (%s::int IS NULL OR id=%s) "
        "ORDER BY id",
        (slug, specialist_id, specialist_id),
    )
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(404, f"specialist not found: {slug}")
    if len(rows) > 1:
        ids = ", ".join(str(r["id"]) for r in rows)
        raise HTTPException(
            409,
            f"slug '{slug}' matches {len(rows)} active specialists (ids {ids}); "
            "pass ?specialist_id= to say which one",
        )
    return rows[0]


@router.put("/{slug}", dependencies=[Depends(requires("specialists.write"))])
def update_specialist(
    slug: Annotated[str, PathParam(min_length=2, max_length=61)],
    body: SpecialistUpdate,
    updated_by: str = "panel",
    specialist_id: int | None = Query(None),
):
    with cursor(commit=True) as cur:
        row = _write_target(cur, slug, specialist_id)
        new_version = _bump_version(row["version"], body.version_bump) if body.version_bump else row["version"]

        sets = ["updated_by=%s", "updated_at=now()", "version=%s"]
        params = [updated_by, new_version]
        if body.name is not None:    sets.append("name=%s");      params.append(body.name)
        if body.body_md is not None: sets.append("body_md=%s");   params.append(body.body_md)
        if body.archetype is not None: sets.append("archetype=%s"); params.append(body.archetype)
        params.append(row["id"])
        cur.execute(
            f"UPDATE sn_config.specialists SET {', '.join(sets)} "
            f"WHERE id=%s RETURNING {COLS}",
            params,
        )
        row = cur.fetchone()
    return _row_to_read(row).model_dump(mode="json")


@router.delete("/{slug}", status_code=204, dependencies=[Depends(requires("specialists.write"))])
def delete_specialist(
    slug: Annotated[str, PathParam(min_length=2, max_length=61)],
    specialist_id: int | None = Query(None),
):
    with cursor(commit=True) as cur:
        row = _write_target(cur, slug, specialist_id)
        cur.execute(
            "UPDATE sn_config.specialists SET archived_at=now() WHERE id=%s",
            (row["id"],),
        )
    return None
