"""CRUD endpoints for the References registry (sn_config.agent_references).

Tenant-scoped via get_principal: the caller's tenant comes from their
authenticated account, never a query parameter. A cross-tenant (super-admin)
role sees every tenant's references on list; writes always land in (and are
scoped to) the caller's own tenant. Soft-delete via archived_at; slug unique
among live rows so a deleted key can be reused.

DB column is `slug`; the API exposes it as `key` (what agents reference),
mirroring the Policies registry.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from auth import Principal, get_principal
from db import cursor
from permissions import requires
from events_emit import emit
from schemas.reference import Reference, ReferenceCreate, ReferenceUpdate


router = APIRouter(prefix="/api/references", tags=["references"])

SELECT_COLS = (
    "id, tenant_id, slug AS key, title, target_kind, target_id, required, "
    "created_at, updated_at, created_by, updated_by"
)


@router.get("", response_model=dict)
def list_references(principal: Principal = Depends(requires("references.read")),
                    target_kind: str | None = None):
    where = ["archived_at IS NULL"]
    params: list = []
    if not principal.cross_tenant:
        where.append("tenant_id = %s")
        params.append(principal.tenant_id)
    if target_kind:
        where.append("target_kind = %s")
        params.append(target_kind)
    sql = (
        f"SELECT {SELECT_COLS} FROM sn_config.agent_references "
        f"WHERE {' AND '.join(where)} ORDER BY slug"
    )
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return {"references": [Reference(**r).model_dump(mode="json") for r in rows]}


@router.get("/{key}", response_model=Reference)
def get_reference(key: str, principal: Principal = Depends(requires("references.read"))):
    with cursor() as cur:
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.agent_references "
            "WHERE tenant_id = %s AND slug = %s AND archived_at IS NULL",
            (principal.tenant_id, key),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"reference '{key}' not found")
    return Reference(**row)


@router.post("", response_model=Reference, status_code=status.HTTP_201_CREATED)
def create_reference(payload: ReferenceCreate,
                     principal: Principal = Depends(requires("references.write"))):
    actor = principal.username
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT 1 FROM sn_config.agent_references "
            "WHERE tenant_id = %s AND slug = %s AND archived_at IS NULL",
            (principal.tenant_id, payload.key),
        )
        if cur.fetchone():
            raise HTTPException(status_code=409,
                                detail=f"reference '{payload.key}' already exists")
        cur.execute(
            f"""
            INSERT INTO sn_config.agent_references
              (tenant_id, slug, title, target_kind, target_id, required,
               created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {SELECT_COLS}
            """,
            (
                principal.tenant_id, payload.key, payload.title,
                payload.target_kind, payload.target_id, payload.required,
                actor, actor,
            ),
        )
        row = cur.fetchone()
        emit(cur, actor=actor, event_type="reference.created",
             entity_type="reference", entity_id=payload.key,
             payload={"target_kind": payload.target_kind})
    return Reference(**row)


@router.put("/{key}", response_model=Reference)
def update_reference(key: str, payload: ReferenceUpdate,
                     principal: Principal = Depends(requires("references.write"))):
    actor = principal.username
    with cursor(commit=True) as cur:
        cur.execute(
            f"""
            UPDATE sn_config.agent_references
               SET title = %s, target_kind = %s, target_id = %s, required = %s,
                   updated_at = now(), updated_by = %s
             WHERE tenant_id = %s AND slug = %s AND archived_at IS NULL
         RETURNING {SELECT_COLS}
            """,
            (
                payload.title, payload.target_kind, payload.target_id,
                payload.required, actor, principal.tenant_id, key,
            ),
        )
        row = cur.fetchone()
        if row is not None:
            emit(cur, actor=actor, event_type="reference.updated",
                 entity_type="reference", entity_id=key, payload={})
    if row is None:
        raise HTTPException(status_code=404, detail=f"reference '{key}' not found")
    return Reference(**row)


@router.delete("/{key}", response_model=dict)
def delete_reference(key: str, principal: Principal = Depends(requires("references.write"))):
    """Soft delete: set archived_at. The key becomes reusable."""
    actor = principal.username
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sn_config.agent_references "
            "SET archived_at = now(), updated_by = %s "
            "WHERE tenant_id = %s AND slug = %s AND archived_at IS NULL "
            "RETURNING slug",
            (actor, principal.tenant_id, key),
        )
        row = cur.fetchone()
        if row is not None:
            emit(cur, actor=actor, event_type="reference.deleted",
                 entity_type="reference", entity_id=key, payload={})
    if row is None:
        raise HTTPException(status_code=404, detail=f"reference '{key}' not found")
    return {"ok": True, "deleted": key}
