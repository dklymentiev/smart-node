"""Owner-managed credentials that prove an existing actor on a named surface.

Bearer plaintext is returned once at creation or rotation. The database and
event log receive only its SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import Principal, invalidate_socket_credential, require_session
from db import cursor
from events_emit import emit
from permissions import requires

router = APIRouter()

_SCOPES = {
    "os_user": "socket",
    "bearer_token": "admin",
    "authum_username": "session",
    "telegram_from_id": "telegram",
}


class CredentialCreate(BaseModel):
    cred_type: str
    cred_value: str | None = Field(default=None, min_length=1)


# The username becomes the actor id suffix and the account name, so it is
# held to the shape of a local account name and never normalised silently.
_OPERATOR_USERNAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class OperatorCreate(BaseModel):
    username: str
    display_name: str = Field(default="Operator", min_length=1, max_length=120)


def _digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(48)


@router.post(
    "/api/operators",
    dependencies=[Depends(requires("roles.grant"))],
    status_code=201,
)
def create_operator(
    payload: OperatorCreate,
    principal: Principal = Depends(require_session),
):
    """Register a node-specific operator and issue its first bearer token.

    Actor, account, role and credential are written in one transaction: any
    refusal below rolls all of them back, so a failed call leaves no rows.
    """
    username = payload.username
    if not _OPERATOR_USERNAME.fullmatch(username):
        raise HTTPException(
            422,
            "operator username must start with a lowercase letter and contain "
            "only lowercase letters, digits, '-' or '_' (32 characters at most)",
        )
    actor_id = f"component:{username}"
    token = _new_token()
    with cursor(commit=True) as cur:
        # Accounts are resolved by name alone, across tenants, and an agent
        # slug carried as a caller name is evaluated with that agent's grants.
        # An operator must therefore never share a name with either.
        cur.execute(
            "SELECT 1 FROM sn_config.users WHERE email = %s "
            "UNION ALL SELECT 1 FROM sn_config.agents WHERE slug = %s",
            (username, username),
        )
        if cur.fetchone() is not None:
            raise HTTPException(409, "username already belongs to an account or an agent")
        cur.execute(
            "INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) "
            "VALUES (%s, %s, 'component', 'system', 'component', 'internal') "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            (actor_id, payload.display_name),
        )
        if cur.fetchone() is None:
            raise HTTPException(409, f"actor '{actor_id}' already exists")
        cur.execute(
            "INSERT INTO sn_config.users (tenant_id, email, role, active, actor_id) "
            "VALUES (%s, %s, 'admin', true, %s)",
            (principal.tenant_id, username, actor_id),
        )
        cur.execute(
            "INSERT INTO sn_core.actor_roles (actor_id, role, granted_by, source) "
            "VALUES (%s, 'admin', %s, 'owner_grant')",
            (actor_id, principal.actor_id),
        )
        cur.execute(
            "INSERT INTO sn_config.actor_credentials "
            "(actor_id, cred_type, cred_value, scope) "
            "VALUES (%s, 'bearer_token', %s, %s)",
            (actor_id, _digest(token), _SCOPES["bearer_token"]),
        )
        emit(
            cur,
            actor=principal.actor_id or principal.username,
            event_type="actor.operator.registered",
            entity_type="actor",
            entity_id=actor_id,
            payload={"username": username, "role": "admin"},
        )
        emit(
            cur,
            actor=principal.actor_id or principal.username,
            event_type="actor.credential.created",
            entity_type="actor",
            entity_id=actor_id,
            payload={"cred_type": "bearer_token", "scope": _SCOPES["bearer_token"]},
        )
    return {"actor_id": actor_id, "username": username, "role": "admin",
            "token": token, "note": "Token shown once; rotate if lost."}


@router.post(
    "/api/actors/{actor_id}/credentials",
    dependencies=[Depends(requires("roles.grant"))],
    status_code=201,
)
def create_credential(
    actor_id: str,
    payload: CredentialCreate,
    principal: Principal = Depends(require_session),
):
    scope = _SCOPES.get(payload.cred_type)
    if scope is None:
        raise HTTPException(422, f"unsupported credential type '{payload.cred_type}'")
    if payload.cred_type == "bearer_token":
        if payload.cred_value is not None:
            raise HTTPException(422, "bearer token value must be generated by the server")
        token = _new_token()
        stored_value = _digest(token)
    else:
        if not payload.cred_value or not payload.cred_value.strip():
            raise HTTPException(422, "credential value is required")
        token = None
        stored_value = payload.cred_value.strip()

    with cursor(commit=True) as cur:
        cur.execute("SELECT relation FROM sn_core.actors WHERE id = %s", (actor_id,))
        actor = cur.fetchone()
        if actor is None:
            raise HTTPException(404, f"actor '{actor_id}' not found")
        if actor["relation"] != "internal" and payload.cred_type in (
            "os_user", "bearer_token", "authum_username"
        ):
            raise HTTPException(403, "administrative credentials require an internal actor")
        cur.execute(
            "INSERT INTO sn_config.actor_credentials "
            "(actor_id, cred_type, cred_value, scope) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING actor_id",
            (actor_id, payload.cred_type, stored_value, scope),
        )
        if cur.fetchone() is None:
            raise HTTPException(409, "credential already exists; rotate or revoke it")
        emit(
            cur,
            actor=principal.actor_id or principal.username,
            event_type="actor.credential.created",
            entity_type="actor",
            entity_id=actor_id,
            payload={"cred_type": payload.cred_type, "scope": scope},
        )
    result = {"actor_id": actor_id, "cred_type": payload.cred_type, "scope": scope}
    if token is not None:
        result.update(token=token, note="Token shown once; rotate if lost.")
    elif payload.cred_type == "os_user":
        invalidate_socket_credential(stored_value)
    return result


@router.delete(
    "/api/actors/{actor_id}/credentials/{cred_type}",
    dependencies=[Depends(requires("roles.grant"))],
    status_code=204,
)
def revoke_credential(
    actor_id: str,
    cred_type: str,
    principal: Principal = Depends(require_session),
):
    with cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM sn_config.actor_credentials "
            "WHERE actor_id=%s AND cred_type=%s RETURNING scope, cred_value",
            (actor_id, cred_type),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "credential not found")
        emit(
            cur,
            actor=principal.actor_id or principal.username,
            event_type="actor.credential.revoked",
            entity_type="actor",
            entity_id=actor_id,
            payload={"cred_type": cred_type, "scope": row["scope"]},
        )
    if cred_type == "os_user":
        invalidate_socket_credential(row["cred_value"])


@router.post(
    "/api/actors/{actor_id}/credentials/bearer_token/rotate",
    dependencies=[Depends(requires("roles.grant"))],
)
def rotate_bearer_token(
    actor_id: str,
    principal: Principal = Depends(require_session),
):
    token = _new_token()
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sn_config.actor_credentials SET cred_value=%s, created_at=now() "
            "WHERE actor_id=%s AND cred_type='bearer_token' AND scope='admin' "
            "RETURNING actor_id",
            (_digest(token), actor_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(404, "bearer credential not found")
        emit(
            cur,
            actor=principal.actor_id or principal.username,
            event_type="actor.credential.rotated",
            entity_type="actor",
            entity_id=actor_id,
            payload={"cred_type": "bearer_token", "scope": "admin"},
        )
    return {"actor_id": actor_id, "cred_type": "bearer_token", "token": token,
            "note": "Token shown once; rotate if lost."}
