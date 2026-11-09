"""Policies catalog endpoints (sn_config.policies).

The catalog is the node-level set of reusable rules that agents opt into. A
policy with tenant_id IS NULL is a node-level (scope="node") rule available to
every agent on the node; a policy with a tenant_id set is an enterprise/tenant
rule (scope="enterprise").

These endpoints are node-internal (reached only via the studio proxy on the
docker0 gateway), so they use the trusted db.cursor directly -- the same pattern
as routes/agents.py -- rather than the tenant-scoped principal used by the older
CRUD. Composition into CLAUDE.md lives in routes/agents.py; this module owns the
catalog (list + author) only. Per-agent selection lives on the agents router
(GET/PUT /api/agents/{slug}/policies).
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from db import cursor
from events_emit import emit
from permissions import requires

log = logging.getLogger("core-api.policies")

router = APIRouter()

CATEGORIES = ("safety", "workflow", "sales", "communication", "data-handling")
SEVERITIES = ("info", "warn", "block")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,60}$")


def _slugify(title: str) -> str:
    """Derive a slug from a title that satisfies the policies_slug_check regex
    (^[a-z][a-z0-9-]{1,60}$)."""
    s = title.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "policy"
    if not s[0].isalpha():
        s = "p-" + s
    s = s[:61]
    if not SLUG_RE.match(s):
        raise HTTPException(422, f"could not derive a valid slug from title '{title}'")
    return s


def _scope(tenant_id, agent_id=None) -> str:
    if agent_id is not None:
        return "agent"
    return "node" if tenant_id is None else "enterprise"


SELECT_COLS = (
    "slug, category, title, body_md, severity, enabled, tenant_id, agent_id, version, "
    "rationale"
)


def _row_to_dict(r) -> dict:
    return {
        "slug": r["slug"],
        "category": r["category"],
        "title": r["title"],
        "body_md": r["body_md"],
        "severity": r["severity"],
        "enabled": r["enabled"],
        "scope": _scope(r["tenant_id"], r["agent_id"]),
        "version": r["version"],
        "rationale": r["rationale"],
    }


class PolicyCreate(BaseModel):
    category: str
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1)
    # WHY IT IS REQUIRED. Every one of the fourteen seeded policies carries the
    # dated incident that produced it ("2026-08-06: an agent told the owner
    # 'Sent push notification with the summary' when it had not"), and that is
    # what makes a grown catalogue reviewable: with fifty rules you can ask
    # which ones still earn their place only if each one says what went wrong.
    # The column existed and this endpoint dropped it, so a rule written the
    # supported way arrived with no history while the seeded ones had it. A
    # rule nobody can trace is a rule nobody dares delete.
    rationale: str = Field(min_length=1)
    slug: str | None = None
    severity: str = "warn"
    # The panel's Enabled box. It was not in this model, so pydantic dropped it
    # and a rule written "off" went straight into every agent's prompt.
    enabled: bool = True
    tenant_id: int | None = None  # default node-level
    agent_slug: str | None = None  # set -> rule owned by that agent; None -> global

    @field_validator("category")
    @classmethod
    def _cat(cls, v):
        if v not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        return v

    @field_validator("severity")
    @classmethod
    def _sev(cls, v):
        if v not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        return v

    @field_validator("slug")
    @classmethod
    def _slug(cls, v):
        if v is not None and not SLUG_RE.match(v):
            raise ValueError("slug must match ^[a-z][a-z0-9-]{1,60}$")
        return v

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, v):
        # min_length alone accepts a single space, which satisfies the field and
        # defeats the point of it.
        if not v.strip():
            raise ValueError("rationale must say WHY this rule exists: the date "
                             "and what went wrong without it")
        return v.strip()


@router.get("", dependencies=[Depends(requires("policies.read"))])
def list_policies(category: str | None = Query(default=None)):
    """The catalog. Node-level (scope=node) and enterprise policies, optionally
    filtered by category. Each row includes a `collision` flag: True when another
    non-archived row with the same slug exists at a different scope. Agents that
    receive a colliding slug get both rule bodies; the collision field makes this
    visible so the operator can resolve it before it causes confusion."""
    where = ["archived_at IS NULL"]
    params: list = []
    if category:
        where.append("category = %s")
        params.append(category)
    with cursor() as cur:
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.policies "
            f"WHERE {' AND '.join(where)} ORDER BY category, slug",
            tuple(params),
        )
        rows = cur.fetchall() or []

        # Identify slugs that appear at more than one active scope so callers
        # can see collisions without a second request.
        slug_counts: dict[str, int] = {}
        for r in rows:
            slug_counts[r["slug"]] = slug_counts.get(r["slug"], 0) + 1
        collision_slugs = {s for s, n in slug_counts.items() if n > 1}

    result = []
    for r in rows:
        d = _row_to_dict(r)
        d["collision"] = r["slug"] in collision_slugs
        result.append(d)
    return {
        "policies": result,
        "collisions": sorted(collision_slugs),
    }


@router.post("", status_code=201,
              dependencies=[Depends(requires("policies.write"))])
def create_policy(payload: PolicyCreate, created_by: str = "panel"):
    """Author a new rule. If slug is omitted it is derived from the title.
    Default tenant_id is NULL (node-level)."""
    slug = payload.slug or _slugify(payload.title)
    with cursor(commit=True) as cur:
        # Resolve agent scope: agent_slug set -> the rule belongs to that one agent.
        agent_id = None
        if payload.agent_slug is not None:
            cur.execute(
                "SELECT id FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
                (payload.agent_slug,),
            )
            a = cur.fetchone()
            if a is None:
                raise HTTPException(404, f"agent '{payload.agent_slug}' not found")
            agent_id = a["id"]
        # Uniqueness is per (tenant_id, agent_id, slug) among non-archived rows.
        # COALESCE collapses NULLs (NULL = NULL is never true in a bare comparison).
        cur.execute(
            "SELECT 1 FROM sn_config.policies "
            "WHERE COALESCE(tenant_id, 0) = COALESCE(%s, 0) "
            "AND COALESCE(agent_id, 0) = COALESCE(%s, 0) "
            "AND slug = %s AND archived_at IS NULL",
            (payload.tenant_id, agent_id, slug),
        )
        if cur.fetchone():
            raise HTTPException(409, f"rule '{slug}' already exists in this scope")
        cur.execute(
            f"""
            INSERT INTO sn_config.policies
              (tenant_id, agent_id, slug, category, title, body_md, severity, enabled,
               rationale, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {SELECT_COLS}
            """,
            (
                payload.tenant_id, agent_id, slug, payload.category, payload.title,
                payload.body_md, payload.severity, payload.enabled, payload.rationale,
                created_by, created_by,
            ),
        )
        row = cur.fetchone()
        emit(cur, actor=f"user:{created_by}", event_type="policy.created",
             entity_type="policy", entity_id=slug,
             payload={"category": payload.category, "severity": payload.severity,
                      "scope": _scope(payload.tenant_id, agent_id)})
    return _row_to_dict(row)


class PolicyUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body_md: str | None = None
    category: str | None = None
    severity: str | None = None
    enabled: bool | None = None
    # Optional here, because enabling or disabling a rule is not a rewrite. But
    # when the rule itself changes the reason should change with it, and it can
    # never be emptied: omit it to leave it alone, never to erase it.
    rationale: str | None = None

    @field_validator("category")
    @classmethod
    def _cat(cls, v):
        if v is not None and v not in CATEGORIES:
            raise ValueError(f"category must be one of {CATEGORIES}")
        return v

    @field_validator("severity")
    @classmethod
    def _sev(cls, v):
        if v is not None and v not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        return v

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, v):
        if v is not None and not v.strip():
            raise ValueError("rationale cannot be emptied; omit it to leave it unchanged")
        return v.strip() if v is not None else v


def _scope_filter(scope: str, tenant_id: int | None, agent_slug: str | None,
                  cur) -> tuple[str, list]:
    """Build the WHERE fragment and params for scope-targeted queries.

    scope='node'       -> tenant_id IS NULL AND agent_id IS NULL  (default, seeded rules)
    scope='enterprise' -> tenant_id = <tenant_id> AND agent_id IS NULL
    scope='agent'      -> agent_id = <agent row id resolved from agent_slug>

    Raises HTTPException(400) on bad inputs.
    """
    if scope == "node":
        return "tenant_id IS NULL AND agent_id IS NULL", []

    if scope == "enterprise":
        if tenant_id is None:
            raise HTTPException(400, "scope=enterprise requires tenant_id")
        return "tenant_id = %s AND agent_id IS NULL", [tenant_id]

    if scope == "agent":
        if not agent_slug:
            raise HTTPException(400, "scope=agent requires agent_slug")
        cur.execute(
            "SELECT id FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (agent_slug,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"agent '{agent_slug}' not found")
        return "agent_id = %s", [row["id"]]

    raise HTTPException(400, f"scope must be one of: node, enterprise, agent (got '{scope}')")


@router.patch("/{slug}", dependencies=[Depends(requires("policies.write"))])
def update_policy(
    slug: str,
    payload: PolicyUpdate,
    updated_by: str = "panel",
    scope: str = Query(default="node"),
    tenant_id: int | None = Query(default=None),
    agent_slug: str | None = Query(default=None),
):
    """Edit a rule. Default scope='node' preserves the original behaviour for
    callers that do not pass a scope. Pass scope='enterprise' with tenant_id to
    reach seeded rules (tenant_id=1 on a fresh node). Pass scope='agent' with
    agent_slug to reach agent-owned rules."""
    sets, params = [], []
    for col, val in (("title", payload.title), ("body_md", payload.body_md),
                     ("category", payload.category), ("severity", payload.severity),
                     ("enabled", payload.enabled), ("rationale", payload.rationale)):
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if not sets:
        raise HTTPException(400, "no fields to update")
    sets += ["updated_by = %s", "updated_at = now()"]
    params.append(updated_by)

    with cursor(commit=True) as cur:
        scope_sql, scope_params = _scope_filter(scope, tenant_id, agent_slug, cur)
        params_full = params + scope_params + [slug]
        cur.execute(
            f"UPDATE sn_config.policies SET {', '.join(sets)} "
            f"WHERE {scope_sql} AND slug = %s AND archived_at IS NULL "
            f"RETURNING {SELECT_COLS}",
            tuple(params_full),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(
                404,
                f"rule '{slug}' not found at scope={scope}"
                + (f" tenant_id={tenant_id}" if tenant_id else "")
                + (f" agent={agent_slug}" if agent_slug else ""),
            )
        emit(cur, actor=f"user:{updated_by}", event_type="policy.updated",
             entity_type="policy", entity_id=slug,
             payload={"scope": scope, "tenant_id": tenant_id, "agent_slug": agent_slug})
    return _row_to_dict(row)


@router.delete("/{slug}", status_code=204,
                dependencies=[Depends(requires("policies.write"))])
def delete_policy(
    slug: str,
    updated_by: str = "panel",
    scope: str = Query(default="node"),
    tenant_id: int | None = Query(default=None),
    agent_slug: str | None = Query(default=None),
):
    """Archive (soft-delete) a rule. Default scope='node'. Pass scope='enterprise'
    with tenant_id, or scope='agent' with agent_slug, to target seeded or agent
    rules respectively. Exactly one row is archived per call (LIMIT 1 via subquery)."""
    with cursor(commit=True) as cur:
        scope_sql, scope_params = _scope_filter(scope, tenant_id, agent_slug, cur)
        # Use a subquery to enforce a single-row limit: Postgres UPDATE has no LIMIT.
        cur.execute(
            "UPDATE sn_config.policies SET archived_at = now(), updated_by = %s "
            f"WHERE id = (SELECT id FROM sn_config.policies "
            f"            WHERE {scope_sql} AND slug = %s AND archived_at IS NULL LIMIT 1)",
            tuple([updated_by] + scope_params + [slug]),
        )
        if cur.rowcount == 0:
            raise HTTPException(
                404,
                f"rule '{slug}' not found at scope={scope}"
                + (f" tenant_id={tenant_id}" if tenant_id else "")
                + (f" agent={agent_slug}" if agent_slug else ""),
            )
        emit(cur, actor=f"user:{updated_by}", event_type="policy.deleted",
             entity_type="policy", entity_id=slug,
             payload={"scope": scope, "tenant_id": tenant_id, "agent_slug": agent_slug})
    return None
