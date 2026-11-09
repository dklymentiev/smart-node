"""Central permission check for admin-runner, one chokepoint per route.

Mirrors `core-api/permissions.py` in shape, but admin-runner is a smaller
surface and uses a slimmer path:

  * every admin.* permission in `sn_config.permissions` has `object_axis='none'`
    (checked by the test that ships with step 4a), so there is no per-object
    grant to consult.
  * identity is an account: the request arrives via core-api, which proves
    itself and names the original actor in `X-SN-On-Behalf-Of`;
    `auth.get_principal` refuses a call without that proof and resolves the
    name into a Principal. The account may be a person's or an agent's.
    The effective role is read from `sn_core.actor_roles` by the user's
    `sn_config.users.actor_id`, matching core-api so super-admin here means the
    same thing as super-admin there.

A route declares what it needs:

    @app.post("/operations/{name}")
    def run_operation(..., principal: Principal = Depends(require_permission("admin.node_status"))):

That dependency, on every call:

  1. Resolves the Principal (`auth.get_principal`).
  2. Resolves effective role (`sn_core.actor_roles` by `principal.user_id`'s
     `actor_id`); absence falls back to `user`.
  3. Confirms `sn_config.role_permissions` has (role, permission). On miss,
     403 with a reason that names BOTH role and permission.
  4. Audits every decision (allowed or denied) via `audit.audit()`.

super-admin: as in core-api, holds every permission in the matrix. A super-admin
row missing from role_permissions is treated as a server-side configuration
failure (500), not an actor denial.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import Depends, Header, HTTPException, Request

from audit import audit
from auth import Principal, get_principal
from db import cursor

log = logging.getLogger("admin-runner.permissions")

SUPER_ADMIN = "super-admin"


@dataclass
class PermissionDecision:
    """Everything a caller needs to audit one check, success or failure."""

    permission: str
    role: str
    allowed: bool
    reason: str
    actor_id: Optional[str] = None

    def as_audit_payload(self) -> dict[str, Any]:
        return {
            "permission": self.permission,
            "role": self.role,
            "object_axis": "none",
            "allowed": self.allowed,
            "reason": self.reason,
            "actor_id": self.actor_id,
        }


def _lookup_permission(name: str) -> Optional[dict[str, Any]]:
    """Return the catalogue row for `name`, or None.

    Admin-runner only grants admin.* permissions, which all have object_axis
    'none' as of 1.12.49 (verified by `test_op_permissions.py`). A row missing
    from the catalogue is a startup problem the lifespan check already
    refuses; at request time, treat it as a 500 so the misconfiguration is
    visible instead of silently admitting.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT name, object_axis "
            "FROM sn_config.permissions WHERE name = %s",
            (name,),
        )
        return cur.fetchone()


def _role_has_permission(role: str, permission: str) -> bool:
    """Direct read against sn_config.role_permissions, no caching."""
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sn_config.role_permissions "
            "WHERE role = %s AND permission = %s",
            (role, permission),
        )
        return cur.fetchone() is not None


def _effective_role(principal: Principal) -> tuple[str, Optional[str]]:
    """Resolve effective role from sn_core.actor_roles.

    Returns `(role, actor_id)`. If the user has no actor_id, or no row in
    actor_roles, the effective role is `user` (matches core-api's behaviour
    for a role-less actor). actor_id is returned alongside so the audit event
    can carry it.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT actor_id FROM sn_config.users WHERE id = %s",
            (principal.user_id,),
        )
        row = cur.fetchone()
    actor_id = row["actor_id"] if row and row["actor_id"] else None
    if not actor_id:
        return "user", None
    with cursor() as cur:
        cur.execute(
            "SELECT role FROM sn_core.actor_roles WHERE actor_id = %s",
            (actor_id,),
        )
        row = cur.fetchone()
    return (row["role"] if row else "user"), actor_id


def check_permission(principal: Principal, name: str) -> PermissionDecision:
    """Decide one (principal, permission) pair; no object axis.

    Never raises for a plain denial. The dependency that wraps this
    translates refusals into HTTPException. Callers that want to probe a
    permission without binding to FastAPI call this directly.
    """
    catalog = _lookup_permission(name)
    role, actor_id = _effective_role(principal)
    if catalog is None:
        return PermissionDecision(
            permission=name,
            role=role,
            allowed=False,
            reason=f"permission '{name}' is not declared in sn_config.permissions",
            actor_id=actor_id,
        )

    if catalog["object_axis"] != "none":
        # admin-runner's catalogue is all 'none' as of 1.12.49; a non-none
        # row for an admin.* permission is a startup drift and must be
        # surfaced, not silently admitted.
        return PermissionDecision(
            permission=name,
            role=role,
            allowed=False,
            reason=(
                f"permission '{name}' has object_axis "
                f"'{catalog['object_axis']}' but admin-runner only supports 'none'"
            ),
            actor_id=actor_id,
        )

    if role == SUPER_ADMIN:
        if not _role_has_permission(role, name):
            return PermissionDecision(
                permission=name,
                role=role,
                allowed=False,
                reason=(
                    "super-admin matrix is incomplete: "
                    f"role_permissions missing ('{role}', '{name}')"
                ),
                actor_id=actor_id,
            )
        return PermissionDecision(
            permission=name,
            role=role,
            allowed=True,
            reason="super-admin",
            actor_id=actor_id,
        )

    if not _role_has_permission(role, name):
        return PermissionDecision(
            permission=name,
            role=role,
            allowed=False,
            reason=f"role '{role or '(none)'}' lacks permission '{name}'",
            actor_id=actor_id,
        )

    return PermissionDecision(
        permission=name,
        role=role,
        allowed=True,
        reason="role grant",
        actor_id=actor_id,
    )


def validate_catalogue(permissions: list[str]) -> None:
    """Refuse catalogue drift at startup.

    Called by app.py lifespan with the full set of admin.* permissions
    declared by the REGISTRY. Any permission the app expects but the DB
    does not have is a hard failure: boot refuses rather than admitting
    the handler through a chokepoint that cannot ever grant.
    """
    if not permissions:
        return
    with cursor() as cur:
        cur.execute(
            "SELECT name, object_axis FROM sn_config.permissions "
            "WHERE name = ANY(%s)",
            (list(permissions),),
        )
        catalog = {row["name"]: row["object_axis"] for row in cur.fetchall()}
    missing = [p for p in permissions if p not in catalog]
    if missing:
        raise RuntimeError(
            f"admin-runner: permissions absent from sn_config.permissions: {missing}"
        )
    non_none = [p for p, axis in catalog.items() if axis != "none"]
    if non_none:
        raise RuntimeError(
            "admin-runner: permissions with object_axis != 'none' are not "
            f"supported here: {non_none}"
        )


def require_permission(name: str) -> Callable[..., Principal]:
    """FastAPI dependency that gates a route on one permission.

    Usage:

        @app.get(
            "/operations",
            dependencies=[Depends(require_permission("admin.node_status"))],
        )

    Passes the Principal straight back when admitted. Every call writes an
    `admin.permission_granted` or `admin.permission_denied` event through
    `audit.audit()` so the matrix is reconstructable from the event log.
    """
    def _dep(
        request: Request,
        remote_user: str = Header(default=None),
    ) -> Principal:
        try:
            principal = get_principal(request, remote_user)
        except HTTPException as exc:
            # No proved caller, or no account behind the name it carried. The
            # caller is not named in the event: resolving it is what just
            # failed, and asking again would raise before anything was written.
            with cursor(commit=True) as cur:
                audit(
                    cur,
                    actor="component:admin-runner",
                    event_type="admin.permission_denied",
                    entity_type="permission",
                    entity_id=name,
                    payload={
                        "permission": name,
                        "allowed": False,
                        "reason": "identity-or-proof",
                        "status": exc.status_code,
                    },
                )
            raise

        decision = check_permission(principal, name)

        try:
            with cursor(commit=True) as cur:
                audit(
                    cur,
                    actor="user:" + principal.username,
                    event_type=(
                        "admin.permission_granted"
                        if decision.allowed
                        else "admin.permission_denied"
                    ),
                    entity_type="permission",
                    entity_id=name,
                    payload=decision.as_audit_payload(),
                )
        except Exception as exc:  # noqa: BLE001 -- never fail-closed on audit
            log.warning(
                "admin.permission_%s audit failed for %s: %r",
                "granted" if decision.allowed else "denied",
                name,
                exc,
            )

        if not decision.allowed:
            # A missing catalogue row or a non-'none' axis is a startup
            # drift the lifespan check normally catches; keep 500 so the
            # misconfiguration is visible if it slips through.
            is_config = (
                "not declared" in decision.reason
                or "object_axis" in decision.reason
            )
            raise HTTPException(
                status_code=500 if is_config else 403,
                detail=decision.reason,
            )
        return principal

    _dep.__name__ = f"require_permission_{name.replace('.', '_')}"
    _dep._permission_name = name
    return _dep
