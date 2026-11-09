"""Projects -- the work areas of the node.

A project is three things that must agree: a registry row here, a unix group,
and `/srv/projects/<slug>` at 2770 setgid owned by that group. The slug is the
one key across memory (`project-<slug>`), documents (`<slug>/`), the node
planner's project of the same name, and the filesystem.

WHY A ROW AND NOT A DIRECTORY CONVENTION. A convention holds none of that
mapping, and the projector and drift detector planned for 1.11.0 would have
nothing to compare the disk against: a directory with no owner would be
indistinguishable from a correct one.

THE ROW IS WRITTEN FIRST, THE DISK SECOND, AND THAT ORDER IS DELIBERATE. If the
provisioner fails, the outcome is a row with no directory -- visible, and
repaired by calling this again. The other order gives a directory with no row,
which is exactly the state the drift detector exists to report and which
nothing here could then explain.

CREATING A PROJECT IS A GRANT OF ACCESS, so it needs a proven session rather
than an asserted identity. While `Remote-User` could be asserted by
anything on the node, an agent could put itself into any project -- and since
the old broad sudo grant gives agent-sysadmin unrestricted access, into one whose group can then
do anything. A proven session is therefore required for this
work, not a preference.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import Principal, require_session
from db import cursor
from permissions import requires
from events_emit import emit
from lib.admin_runner import runner_op

log = logging.getLogger("core-api.projects")

router = APIRouter(prefix="/api/projects", tags=["projects"])

SLUG_PATTERN = r"^[a-z][a-z0-9-]{1,40}$"
PROJECT_ROOT = "/srv/projects"

_COLS = ("id, slug, name, unix_group, fs_path, workspace_id, planner_project, "
         "created_by, created_at, archived_at, restricted")


class ProjectCreate(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str | None = None
    # The node planner's project this maps to. Referenced, never duplicated:
    # duplicating it breaks "one door per class" on day one.
    planner_project: str | None = None
    # Present and normally NULL. Workspaces are a later grouping; the flat
    # layout was chosen because adding them costs one column, while un-nesting
    # would cost moving every file.
    workspace_id: int | None = None
    # when true, the installer drops the grp-role-developer default
    # ACL from /work/projects/<slug> and keeps only grp-prj-<slug>. Off by
    # default; every project is developer-writable unless the owner names it
    # narrower.
    restricted: bool = False


class ProjectUpdate(BaseModel):
    # toggle the narrow-access flag. Deliberately the only field on
    # this model -- name/planner_project/workspace_id do not change access,
    # so they are not routed through this endpoint. Adding them later is a
    # separate task with its own review.
    restricted: bool | None = None


class MemberIn(BaseModel):
    member_kind: str = Field(pattern=r"^(user|agent)$")
    member_id: str
    role: str = Field(default="member", pattern=r"^(lead|member|viewer)$")


def _row(r) -> dict:
    out = dict(r)
    for k in ("created_at", "archived_at"):
        if out.get(k) is not None:
            out[k] = out[k].isoformat()
    return out


def _members(cur, project_id: int) -> list[dict]:
    cur.execute(
        "SELECT member_kind, member_id, role FROM sn_config.project_members "
        "WHERE project_id = %s ORDER BY member_kind, member_id", (project_id,))
    return [dict(r) for r in (cur.fetchall() or [])]


def _os_users(cur, project_id: int) -> list[str]:
    """The OS accounts that must be in the project's group.

    An agent's account is `agents.os_user`, which is NOT derivable from the
    slug in general -- it is defaulted to `agent-<slug>` at creation and may
    have been set otherwise, so it is read rather than constructed. A `user`
    member is a human account and its member_id IS the OS name; a user with no
    account on the host is skipped by the provisioner and reported.
    """
    cur.execute(
        "SELECT pm.member_kind, pm.member_id, a.os_user "
        "FROM sn_config.project_members pm "
        "LEFT JOIN sn_config.agents a "
        "  ON pm.member_kind = 'agent' AND a.slug = pm.member_id "
        " AND a.archived_at IS NULL "
        "WHERE pm.project_id = %s", (project_id,))
    names = []
    for r in cur.fetchall() or []:
        name = r["os_user"] if r["member_kind"] == "agent" else r["member_id"]
        if name:
            names.append(name)
    return sorted(set(names))


def _provision(cur, principal: Principal, project: dict) -> dict:
    """Make the disk match the row. Returns the provisioner's own answer."""
    result = runner_op(principal.username, "ensure_project", {
        "slug": project["slug"],
        "unix_group": project["unix_group"],
        "members": _os_users(cur, project["id"]),
    })
    # `skipped` names members the provisioner could not add -- usually an agent
    # whose OS user does not exist yet. Reported, never swallowed: a member who
    # silently has no access is the failure this whole surface exists to avoid.
    emit(cur, actor=f"user:{principal.username}", event_type="project.provisioned",
         entity_type="project", entity_id=project["slug"],
         payload={"unix_group": project["unix_group"],
                  "fs_path": project["fs_path"],
                  "members": result.get("members", []),
                  "skipped": result.get("skipped", [])})
    return result


@router.get("", response_model=dict)
def list_projects(include_archived: bool = False,
                  principal: Principal = Depends(requires("projects.read"))):
    with cursor() as cur:
        sql = f"SELECT {_COLS} FROM sn_config.projects "
        params: list = []
        where = []
        if not principal.cross_tenant:
            where.append("tenant_id = %s")
            params.append(principal.tenant_id)
        if not include_archived:
            where.append("archived_at IS NULL")
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY slug"
        cur.execute(sql, params)
        rows = cur.fetchall() or []
        out = []
        for r in rows:
            item = _row(r)
            item["members"] = _members(cur, r["id"])
            out.append(item)
    return {"projects": out, "count": len(out)}


@router.get("/{slug}", response_model=dict)
def get_project(slug: str, principal: Principal = Depends(requires("projects.read"))):
    with cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM sn_config.projects WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"project not found: {slug}")
        if not principal.cross_tenant:
            cur.execute("SELECT tenant_id FROM sn_config.projects WHERE slug = %s", (slug,))
            if cur.fetchone()["tenant_id"] != principal.tenant_id:
                raise HTTPException(404, f"project not found: {slug}")
        out = _row(row)
        out["members"] = _members(cur, row["id"])
    return out


@router.post("", status_code=201, response_model=dict,
             dependencies=[Depends(requires("projects.write"))])
def create_project(body: ProjectCreate,
                   principal: Principal = Depends(require_session)):
    """Create the row, then make the disk match it.

    Idempotent on the row (409 on a slug that exists) but NOT a no-op on the
    disk: see reprovision, which is the repair path for a row whose directory
    is missing or whose membership drifted.
    """
    unix_group = "project-" + body.slug
    fs_path = f"{PROJECT_ROOT}/{body.slug}"
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute("SELECT 1 FROM sn_config.projects WHERE slug = %s", (body.slug,))
        if cur.fetchone():
            raise HTTPException(409, f"project already exists: {body.slug}")
        cur.execute(
            "INSERT INTO sn_config.projects "
            "  (slug, name, unix_group, fs_path, workspace_id, planner_project, "
            "   created_by, tenant_id, restricted) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLS}",
            (body.slug, body.name or body.slug, unix_group, fs_path,
             body.workspace_id, body.planner_project, principal.username,
             principal.tenant_id, body.restricted))
        row = _row(cur.fetchone())
        emit(cur, actor=f"user:{principal.username}", event_type="project.created",
             entity_type="project", entity_id=body.slug,
             payload={"unix_group": unix_group, "fs_path": fs_path})
        provisioned = _provision(cur, principal, row)
    row["members"] = []
    row["provisioned"] = provisioned
    return row


@router.patch("/{slug}", response_model=dict, dependencies=[Depends(requires("projects.write"))])
def update_project(slug: str, body: ProjectUpdate,
                   principal: Principal = Depends(require_session)):
    """Toggle the narrow-access flag.

    Only `restricted` is settable here; every other field on the project row
    either identifies it (slug, unix_group, fs_path) or belongs to a separate
    surface (members, archived_at, planner_project). Toggling `restricted`
    does not reprovision the filesystem on its own: the installer applies the
    new ACL the next time it runs. Call POST /{slug}/reprovision to force it.
    """
    if body.restricted is None:
        raise HTTPException(400, "nothing to update: restricted is required")
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(f"SELECT {_COLS} FROM sn_config.projects "
                    "WHERE slug = %s AND archived_at IS NULL", (slug,))
        proj = cur.fetchone()
        if proj is None:
            raise HTTPException(404, f"project not found: {slug}")
        cur.execute("UPDATE sn_config.projects SET restricted = %s "
                    f"WHERE slug = %s RETURNING {_COLS}",
                    (body.restricted, slug))
        row = _row(cur.fetchone())
        emit(cur, actor=f"user:{principal.username}",
             event_type="project.restricted.set",
             entity_type="project", entity_id=slug,
             payload={"restricted": body.restricted})
        row["members"] = _members(cur, row["id"])
    return row


@router.post("/{slug}/members", status_code=201, response_model=dict,
             dependencies=[Depends(requires("projects.members.write"))])
def add_member(slug: str, body: MemberIn,
               principal: Principal = Depends(require_session)):
    """Add a member and put their OS account in the group in the same call.

    Both halves or neither: a row saying someone is a member while the group
    says otherwise is worse than no row, because the panel then shows access
    that does not exist.
    """
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(f"SELECT {_COLS} FROM sn_config.projects "
                    "WHERE slug = %s AND archived_at IS NULL", (slug,))
        proj = cur.fetchone()
        if proj is None:
            raise HTTPException(404, f"project not found: {slug}")
        if body.member_kind == "agent":
            cur.execute("SELECT 1 FROM sn_config.agents "
                        "WHERE slug = %s AND archived_at IS NULL", (body.member_id,))
            if cur.fetchone() is None:
                raise HTTPException(404, f"agent not found: {body.member_id}")
        cur.execute(
            "INSERT INTO sn_config.project_members "
            "  (project_id, member_kind, member_id, role) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (project_id, member_kind, member_id) "
            "DO UPDATE SET role = EXCLUDED.role",
            (proj["id"], body.member_kind, body.member_id, body.role))
        emit(cur, actor=f"user:{principal.username}", event_type="project.member.added",
             entity_type="project", entity_id=slug,
             payload={"member_kind": body.member_kind, "member_id": body.member_id,
                      "role": body.role})
        provisioned = _provision(cur, principal, _row(proj))
        members = _members(cur, proj["id"])
    return {"ok": True, "slug": slug, "members": members,
            "provisioned": provisioned}


@router.post("/{slug}/reprovision", response_model=dict,
             dependencies=[Depends(requires("projects.reprovision"))])
def reprovision(slug: str, principal: Principal = Depends(require_session)):
    """Make the disk match the row again.

    The repair path, and the one an operator reaches for after reading a drift
    report. It is separate from create so that fixing a directory does not
    require deleting and recreating the registry row -- which would lose
    created_at and every reference to the slug.
    """
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(f"SELECT {_COLS} FROM sn_config.projects "
                    "WHERE slug = %s AND archived_at IS NULL", (slug,))
        proj = cur.fetchone()
        if proj is None:
            raise HTTPException(404, f"project not found: {slug}")
        provisioned = _provision(cur, principal, _row(proj))
    return {"ok": True, "slug": slug, "provisioned": provisioned}


@router.delete("/{slug}", response_model=dict, dependencies=[Depends(requires("projects.archive"))])
def archive_project(slug: str, principal: Principal = Depends(require_session)):
    """Archive the row. The directory and its files are LEFT ALONE.

    Deliberate, and stated so nobody 'completes' it later by accident: this
    endpoint archives a registry entry, and the work in the directory belongs
    to the business, not to the row. Removing files is an operator action with
    a backup behind it, not a side effect of a DELETE.
    """
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute("UPDATE sn_config.projects SET archived_at = now() "
                    "WHERE slug = %s AND archived_at IS NULL RETURNING id", (slug,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"project not found or already archived: {slug}")
        emit(cur, actor=f"user:{principal.username}", event_type="project.archived",
             entity_type="project", entity_id=slug, payload={})
    return {"ok": True, "slug": slug, "archived": True,
            "note": "the registry row is archived; "
                    f"{PROJECT_ROOT}/{slug} and its files are untouched"}
