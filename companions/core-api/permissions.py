"""The central permission check for core-api, one chokepoint per route.

Each annotated route uses `requires("<permission.name>")`.
One module checks every route against ONE table, so the policy is the data in
`sn_config.role_permissions` and not scattered as `if role == "admin"` across
the handlers.

HOW IT WORKS

A route declares what it needs:

    from permissions import requires
    @router.post("/api/agents", dependencies=[Depends(requires("agents.create"))])

That dependency, on every call:

  1. Resolves the Principal (`auth.get_principal`), so we know the role.
  2. Confirms `role_permissions` has (role, permission). Otherwise 403 that
     names BOTH role and permission -- so an operator reading a log can see
     which cell of the matrix was missing, not just that something was.
  3. If the permission has an object axis (`permissions.object_axis != 'none'`),
     consults the matching grant table for the pair (actor, object_id).

SUPER-ADMIN BRANCH. The super-admin role has every permission in the matrix
AND bypasses the object axis grant check: no per-desk, per-workspace, per-tool
grant is needed. This mirrors how the owner reads across tenants (see
`CROSS_TENANT_ROLES` in auth.py). The audit event carries
`object_axis_bypass=true` and names the object id so the row is still
reconstructable.

HANDLER-SCOPED PERMISSIONS. Some permissions cover objects whose grant model
is not defined yet. Their catalogue row says `object_axis = 'none'`, the check
here is the role axis only, and the handler keeps its own narrower scope. The
audit event says so (`object_axis_deferred=handler`), so a reader of the log
knows which calls were scoped by the handler and not by this module.

TRUSTED-ORIGIN PERMISSIONS. A few routes are callbacks the node's own services
make: recording an inbound message, writing an event, a routing lookup. No
role is meant to hold those, and the caller is usually not an account at all.
They are admitted by WHERE the call provably comes from, in one place:

  A. a node service that proved itself with the service credential and is
     listed for that permission in SN_SERVICE_PERMISSIONS;
  B. a unix-socket peer the kernel names, whose uid belongs to the group of
     the node's service accounts (SN_COMPONENTS_GROUP);
  C. the reverse proxy's proof, for the permissions listed for the proxy.

The order for such a permission is: super-admin, then the role axis (empty for
the internal rows, admin-tier for the overlay rows an operator may also invoke),
then trusted origin, else 403. Nothing here admits a caller because a setting
is missing: an unset list admits nobody and the refusal names the setting.

A DECLARED SECRET OPERATOR. One ordinary permission, secrets.read, is also
admitted to a unix-socket peer whose OS account the installer listed in
SN_SECRETS_OPERATOR_USERS. Those are the accounts of the service MCPs, which
read their own credentials from the store per call and are no account on the
role axis. The kernel names the peer, the installer names the list, and the
handler still decides what may be read. Without this the role gate refused
them by identity before the handler's own rule was ever asked.

A NODE SERVICE ON ANY PERMISSION. Channel A is not limited to those names. A
service that proves itself is admitted to exactly the permissions listed for
it in SN_SERVICE_PERMISSIONS, whichever they are, and to nothing else. Two
limits keep that from becoming a way around the matrix: the service must be
acting as ITSELF (a call that names an agent to act for is measured on that
agent's role, here as everywhere), and a permission with an object axis is
never admitted this way, because a service holds no object grant. Channels B
and C stay with the trusted-origin names.

THE SETUP WINDOW. `setup.complete` is held by no role and admitted by none of
the channels above. It is admitted while the node is still being installed, to
the caller presenting the one-time setup token; see `_setup_window_origin`.

The startup scan verifies declared names and object identifiers against the DB
catalogue. During route annotation, a default-deny middleware refuses every
unmarked API route. Unsupported object axes also refuse by default.
"""

from __future__ import annotations

import grp
import hmac
import logging
import os
import pwd
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.routing import APIRoute
from starlette.routing import Match

from auth import (
    ON_BEHALF_HEADER,
    PROXY_PROOF_HEADER,
    SERVICE_AUTH_HEADER,
    SERVICE_NAME_HEADER,
    Principal,
    get_principal,
    peer_credentials,
    proxy_secret,
    service_secret,
    verified_service,
)
from db import cursor
from events_emit import emit

log = logging.getLogger("core-api.permissions")

SUPER_ADMIN = "super-admin"

_OBJECT_GRANTS = {
    "desk": ("sn_core.desk_participants", "desk_id"),
    "workspace": ("sn_core.workspace_bindings", "workspace_id"),
}

# Permissions whose object scope is enforced by the handler, not here. Their
# catalogue rows carry object_axis 'none' until a grant model exists for the
# objects they cover; this set only labels the audit event.
HANDLER_SCOPED_PERMISSIONS = frozenset({
    "knowledge.read",
    "knowledge.write",
    "projects.read",
    "projects.write",
    "projects.members.write",
    "projects.reprovision",
    "projects.archive",
    "users.read",
    "messages.read",
    "messages.send",
    "desks.read",
    "attachments.read",
})

# Admitted by trusted origin when no role admits the caller. The first four are
# held by no role at all; the last three are also held by the admin tier.
TRUSTED_ORIGIN_PERMISSIONS = frozenset({
    "hitl.delivery",
    "internal.events.write",
    "internal.messages.inbound",
    "internal.messages.voice.write",
    "internal.messages.routing",
    "notifications.notify",
    "crons.runs.write",
    "budgets.notify_exhaustion",
})

# What the reverse proxy may be admitted to by its proof alone. Empty: a call
# through the proxy carries a signed-in person, and a person is measured on the
# role axis. A name is added here only when a route must be reachable through
# the proxy with no account behind it.
PROXY_PERMISSIONS: frozenset = frozenset()

# Held by no role and admitted only while the node is being installed.
SETUP_PERMISSION = "setup.complete"

SERVICE_PERMISSIONS_SETTING = "SN_SERVICE_PERMISSIONS"
COMPONENTS_GROUP_SETTING = "SN_COMPONENTS_GROUP"

# The OS accounts the installer declares as readers of the secret store, and the
# one permission that declaration admits them to. The handler reads the same
# setting for its own rule; the two must agree or the door is shut twice.
SECRET_OPERATORS_SETTING = "SN_SECRETS_OPERATOR_USERS"
SECRET_OPERATOR_PERMISSION = "secrets.read"


@dataclass
class PermissionDecision:
    """Everything a caller needs to audit one check, success or failure."""

    permission: str
    role: str
    axis: str
    object_id: Optional[str]
    allowed: bool
    reason: str
    axis_bypass: bool = False
    actor_id: Optional[str] = None
    actor_kind: Optional[str] = None
    # Set when a trusted origin admitted the call: which channel, and who.
    channel: Optional[str] = None
    origin_principal: Optional[str] = None
    origin_detail: dict[str, Any] = field(default_factory=dict)
    # Set when the refusal has an answer of its own that is not a plain 403.
    status: Optional[int] = None
    # Set when trusted origin was consulted and refused: what each channel saw.
    origins_tried: dict[str, str] = field(default_factory=dict)
    # A refusal caused by the node's configuration, not by the caller.
    missing_setting: Optional[str] = None

    def as_audit_payload(self) -> dict[str, Any]:
        payload = {
            "permission": self.permission,
            "role": self.role,
            "object_axis": self.axis,
            "object_id": self.object_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "object_axis_bypass": self.axis_bypass,
            "actor_id": self.actor_id,
            "actor_kind": self.actor_kind,
        }
        if self.permission in HANDLER_SCOPED_PERMISSIONS:
            payload["object_axis_deferred"] = "handler"
        if self.channel:
            payload["admitted_by"] = "trusted_origin"
            payload["channel"] = self.channel
            payload["principal"] = self.origin_principal
            payload.update(self.origin_detail)
        if self.origins_tried:
            payload["trusted_origin"] = dict(self.origins_tried)
        if self.missing_setting:
            payload["missing_setting"] = self.missing_setting
        return payload


def _lookup_permission(name: str) -> Optional[dict[str, Any]]:
    """Return the catalogue row for `name`, or None.

    None means a route is asking for a permission the catalogue does not
    declare -- a code-vs-DB drift the startup check (slice 2) refuses to boot
    with. At request time, we treat it as a hard 500: the server is
    misconfigured and admitting the call would hide that.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT name, description, object_axis "
            "FROM sn_config.permissions WHERE name = %s",
            (name,),
        )
        return cur.fetchone()


def _role_has_permission(role: str, permission: str) -> bool:
    """Direct read against `sn_config.role_permissions`. One source of truth."""
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sn_config.role_permissions "
            "WHERE role = %s AND permission = %s",
            (role, permission),
        )
        return cur.fetchone() is not None


def _effective_role(principal: Principal) -> str:
    """The actor role is authoritative; absence resolves to `user`."""
    if not principal.actor_id:
        return "user"
    with cursor() as cur:
        cur.execute(
            "SELECT role FROM sn_core.actor_roles WHERE actor_id = %s",
            (principal.actor_id,),
        )
        row = cur.fetchone()
    return row["role"] if row else "user"


def _object_granted(axis: str, actor_id: str, object_id: str) -> bool:
    """Consult the declared object grant; unknown axes always refuse.

    The remaining axes require structured object keys and are added when their
    routes are annotated in 4c. They cannot silently fall back to a role grant.
    """
    grant = _OBJECT_GRANTS.get(axis)
    if grant is None:
        return False
    table, key = grant  # fixed identifiers, never from request data
    value = object_id.removeprefix(axis + ":")
    with cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {table} WHERE actor_id = %s AND {key}::text = %s",
            (actor_id, value),
        )
        return cur.fetchone() is not None


def check_permission(
    principal: Principal,
    name: str,
    object_id: Optional[str] = None,
) -> PermissionDecision:
    """Decide one (principal, permission, object_id) tuple.

    Returned verbatim to the caller for audit emission. Never raises for a
    plain denial -- the dependency that wraps this translates refusals into
    HTTPException. check_permission is also called directly from code paths
    that need to probe a permission without binding themselves to FastAPI.
    """
    catalog = _lookup_permission(name)
    if catalog is None:
        return PermissionDecision(
            permission=name,
            role=_effective_role(principal),
            axis="unknown",
            object_id=object_id,
            allowed=False,
            reason=f"permission '{name}' is not declared in sn_config.permissions",
        )

    role = _effective_role(principal)
    axis = catalog["object_axis"]

    # Super-admin: full role axis + object axis bypass. Flagged in the decision
    # so the audit row shows which calls were admitted this way.
    if role == SUPER_ADMIN:
        # The internal rows are held by no role on purpose, so their absence
        # from the matrix is the design and not an incomplete seed.
        if name not in TRUSTED_ORIGIN_PERMISSIONS and not _role_has_permission(role, name):
            # super-admin should hold every permission in the seed; a miss
            # here means the matrix is incomplete and the install is broken.
            return PermissionDecision(
                permission=name,
                role=role,
                axis=axis,
                object_id=object_id,
                allowed=False,
                reason=(
                    "super-admin matrix is incomplete: "
                    f"role_permissions missing ('{role}', '{name}')"
                ),
            )
        return PermissionDecision(
            permission=name,
            role=role,
            axis=axis,
            object_id=object_id,
            allowed=True,
            reason="super-admin",
            axis_bypass=axis != "none",
        )

    if not _role_has_permission(role, name):
        return PermissionDecision(
            permission=name,
            role=role,
            axis=axis,
            object_id=object_id,
            allowed=False,
            reason=f"role '{role or '(none)'}' lacks permission '{name}'",
        )

    if axis == "none":
        return PermissionDecision(
            permission=name,
            role=role,
            axis=axis,
            object_id=object_id,
            allowed=True,
            reason="role grant (no object axis)",
        )

    # Non-'none' axis: the role grant alone is never enough.
    if object_id is None:
        return PermissionDecision(
            permission=name,
            role=role,
            axis=axis,
            object_id=None,
            allowed=False,
            reason=(
                f"permission '{name}' has object_axis '{axis}' and the route "
                "did not pass an object id to requires()"
            ),
        )

    actor_id = principal.actor_id
    granted = bool(actor_id and _object_granted(axis, actor_id, object_id))

    if not granted:
        return PermissionDecision(
            permission=name,
            role=role,
            axis=axis,
            object_id=object_id,
            allowed=False,
            reason=(
                f"role '{role}' holds '{name}' but object axis '{axis}' denied "
                f"actor '{actor_id}' on '{object_id}'"
            ),
        )

    return PermissionDecision(
        permission=name,
        role=role,
        axis=axis,
        object_id=object_id,
        allowed=True,
        reason=f"role grant + {axis} object grant",
    )


def service_has_permission(service: str, permission: str) -> bool:
    """Is `service` listed for `permission`?

    Declared in the environment as SN_SERVICE_PERMISSIONS, one clause per
    service, in the shape SN_SERVICE_AGENTS already has:

        SN_SERVICE_PERMISSIONS="brin:internal.events.write,internal.messages.routing;voice:internal.events.write"

    There is no wildcard: every permission a service is admitted to is written
    out, so reading the setting says exactly what a holder of the credential
    can reach. An unlisted service, and an unset setting, admit nothing.
    """
    if not service or not permission:
        return False
    raw = os.environ.get(SERVICE_PERMISSIONS_SETTING, "")
    for clause in raw.split(";"):
        svc, _, names = clause.partition(":")
        if svc.strip().lower() != service.strip().lower():
            continue
        return permission in {n.strip() for n in names.split(",") if n.strip()}
    return False


def _uid_in_group(uid: int, group: str) -> bool:
    """Does the account behind `uid` belong to `group`?

    Both ways an account belongs are checked. A service account has the group
    as its PRIMARY group, which the group's member list does not show."""
    try:
        account = pwd.getpwuid(uid)
        entry = grp.getgrnam(group)
    except KeyError:
        return False
    return account.pw_gid == entry.gr_gid or account.pw_name in entry.gr_mem


@dataclass
class TrustedOrigin:
    """The outcome of asking where a call provably comes from."""

    channel: Optional[str] = None           # service | peer | proxy when admitted
    principal: Optional[str] = None         # service name | uid=N | proxy
    tried: dict[str, str] = field(default_factory=dict)
    missing_setting: Optional[str] = None
    presented: bool = False                 # a proof was offered, admitted or not
    detail: dict[str, Any] = field(default_factory=dict)
    status: Optional[int] = None            # a refusal that is not a plain 403
    status_detail: Optional[str] = None

    @property
    def admitted(self) -> bool:
        return self.channel is not None


def _service_origin(request: Request, permission: str, axis: str,
                    out: TrustedOrigin) -> bool:
    named = (request.headers.get(SERVICE_NAME_HEADER, "") or "").strip()
    offered = request.headers.get(SERVICE_AUTH_HEADER, "")
    if not named and not offered:
        out.tried["service"] = "not presented"
        return False
    out.presented = True
    if (request.headers.get(ON_BEHALF_HEADER, "") or "").strip():
        # A delegation. The service carries somebody else's identity, and that
        # identity's role is what the call is measured on. Admitting it on the
        # service's own list would hand every agent the service's reach.
        out.tried["service"] = "acting for another identity: decided on that identity's role"
        return False
    if axis != "none":
        out.tried["service"] = (f"this permission has object axis '{axis}' and a "
                                "service holds no object grant")
        return False
    if not service_secret():
        out.tried["service"] = "SN_SERVICE_AUTH_SECRET is not set"
        out.missing_setting = out.missing_setting or "SN_SERVICE_AUTH_SECRET"
        return False
    try:
        service = verified_service(request)
    except TypeError:
        # compare_digest refuses a non-ASCII str; a header can carry one.
        service = None
    if not service:
        out.tried["service"] = "proof not verified"
        return False
    if not os.environ.get(SERVICE_PERMISSIONS_SETTING, "").strip():
        out.tried["service"] = f"{SERVICE_PERMISSIONS_SETTING} is not set"
        out.missing_setting = out.missing_setting or SERVICE_PERMISSIONS_SETTING
        return False
    if not service_has_permission(service, permission):
        out.tried["service"] = f"'{service}' is not listed for this permission"
        return False
    out.channel, out.principal = "service", service
    return True


def _peer_origin(request: Request, out: TrustedOrigin) -> bool:
    creds = peer_credentials(request)
    if creds is None:
        out.tried["peer"] = "not a unix-socket peer"
        return False
    _pid, uid, _gid = creds
    group = os.environ.get(COMPONENTS_GROUP_SETTING, "").strip()
    if not group:
        out.tried["peer"] = f"{COMPONENTS_GROUP_SETTING} is not set"
        out.missing_setting = out.missing_setting or COMPONENTS_GROUP_SETTING
        return False
    if not _uid_in_group(uid, group):
        out.tried["peer"] = f"uid={uid} is not in group '{group}'"
        return False
    out.channel, out.principal = "peer", f"uid={uid}"
    return True


def _secret_operator_origin(request: Request, out: TrustedOrigin) -> bool:
    """A socket peer whose OS account the installer declared a secret operator.

    An unset list admits nobody. `presented` stays False on a refusal: an
    undeclared peer offered no proof, and its answer stays identity's own.
    """
    creds = peer_credentials(request)
    if creds is None:
        out.tried["secret-operator"] = "not a unix-socket peer"
        return False
    try:
        os_user = pwd.getpwuid(creds[1]).pw_name
    except KeyError:
        out.tried["secret-operator"] = f"uid={creds[1]} has no OS account"
        return False
    declared = {v.strip().lower()
                for v in os.environ.get(SECRET_OPERATORS_SETTING, "").split(",")
                if v.strip()}
    if os_user.lower() not in declared:
        out.tried["secret-operator"] = (f"OS account '{os_user}' is not listed in "
                                        f"{SECRET_OPERATORS_SETTING}")
        return False
    out.channel, out.principal = "secret-operator", f"os:{os_user}"
    return True


def _proxy_origin(request: Request, permission: str, out: TrustedOrigin) -> bool:
    offered = request.headers.get(PROXY_PROOF_HEADER, "")
    if not offered:
        out.tried["proxy"] = "not presented"
        return False
    secret = proxy_secret()
    if not secret:
        out.tried["proxy"] = "SN_PROXY_AUTH_SECRET is not set"
        out.missing_setting = out.missing_setting or "SN_PROXY_AUTH_SECRET"
        return False
    if not hmac.compare_digest(offered.encode("utf-8", "replace"),
                               secret.encode("utf-8")):
        out.tried["proxy"] = "proof not verified"
        return False
    if permission not in PROXY_PERMISSIONS:
        out.tried["proxy"] = "the proxy is not listed for this permission"
        return False
    out.channel, out.principal = "proxy", "proxy"
    return True


def _setup_window_origin(request: Request, out: TrustedOrigin) -> bool:
    """Admit the first-run setup call while the setup window is open.

    The window is open while the one-time setup token is still on disk: the
    installer writes it, and the setup handler removes it once the first
    operator exists. That is the handler's own first test, asked here so the
    route is declared like every other and its admission is recorded. Whether
    the caller actually HOLDS the token is still the handler's question, since
    the token travels in the request body; an open window admits the call to
    that comparison and to nothing else.

    A closed window is answered as the handler always answered it, so a panel
    that tells "already set up" from "refused" keeps doing so.
    """
    from routes import setup          # lazy: routes import this module

    if not setup._read_setup_token():
        out.tried["setup"] = "window closed: no setup token on this node"
        out.status = 409
        out.status_detail = "setup is not available (already completed?)"
        return False
    out.channel, out.principal = "setup", "setup-window"
    out.detail = {"window": "open", "token": "compared by the handler"}
    return True


def trusted_origin(request: Request, permission: str, axis: str = "none") -> TrustedOrigin:
    """Where this call provably comes from, for one permission.

    The channels are asked in a fixed order and the first that admits wins. A
    refusal records what every channel saw, so the audit event shows what was
    NOT presented and not only that the call was refused. A missing setting is
    reported only by the channel the caller actually presented.

    Every permission may be admitted to a listed service. The socket peer and
    the proxy proof are asked only for the trusted-origin names. A declared
    secret operator on the socket is asked for secrets.read alone.
    """
    out = TrustedOrigin()
    if permission == SETUP_PERMISSION:
        _setup_window_origin(request, out)
        return out
    if _service_origin(request, permission, axis, out):
        return out
    if permission == SECRET_OPERATOR_PERMISSION and _secret_operator_origin(request, out):
        return out
    if permission not in TRUSTED_ORIGIN_PERMISSIONS:
        return out
    if _peer_origin(request, out):
        return out
    if _proxy_origin(request, permission, out):
        return out
    return out


def decide_permission(
    request: Request,
    principal: Optional[Principal],
    name: str,
    object_id: Optional[str] = None,
    identity_refusal: Optional[str] = None,
) -> Optional[PermissionDecision]:
    """Decide one call: the role axis first, then where the call comes from.

    `principal` is None when the caller resolved to no account, which is the
    ordinary case for a service callback; `identity_refusal` says why.

    Returns None for the one case this module has nothing to say about: no
    account, an ordinary permission, and no proof of origin offered. That call
    is refused by identity, with identity's own answer.
    """
    role = "(no account)"
    by_role: Optional[PermissionDecision] = None
    if principal is not None:
        by_role = check_permission(principal, name, object_id)
        if by_role.allowed or by_role.axis == "unknown":
            return by_role
        role, axis = by_role.role, by_role.axis
    else:
        catalog = _lookup_permission(name)
        if catalog is None:
            return PermissionDecision(
                permission=name, role=role, axis="unknown", object_id=None, allowed=False,
                reason=f"permission '{name}' is not declared in sn_config.permissions",
            )
        axis = catalog["object_axis"]

    origin = trusted_origin(request, name, axis)
    if origin.admitted:
        return PermissionDecision(
            permission=name, role=role, axis=axis, object_id=object_id, allowed=True,
            reason=f"trusted origin ({origin.channel}: {origin.principal})",
            channel=origin.channel, origin_principal=origin.principal,
            origin_detail=origin.detail,
        )

    by_origin_only = name in TRUSTED_ORIGIN_PERMISSIONS or name == SETUP_PERMISSION
    if not by_origin_only and not origin.presented:
        # An ordinary permission and no proof offered: the role axis answered.
        return by_role
    if by_role is not None and not by_origin_only:
        # The role refused and the proof offered did not admit either. The
        # refusal stays the role's; the event also says what the proof was.
        by_role.origins_tried = origin.tried
        by_role.missing_setting = origin.missing_setting
        return by_role

    who = (f"role '{role}' does not hold it" if principal is not None
           else f"the caller is no account ({identity_refusal or 'unidentified'})")
    if origin.missing_setting:
        reason = (f"permission '{name}' cannot be admitted by trusted origin: "
                  f"{origin.missing_setting} is not set on this node")
    elif origin.status_detail:
        reason = origin.status_detail
    else:
        reason = (f"permission '{name}' refused: {who}, and no trusted origin "
                  "was presented (a listed node service, a node component on "
                  "the unix socket, or the proxy)")
    return PermissionDecision(
        permission=name, role=role, axis=axis, object_id=object_id, allowed=False,
        reason=reason, origins_tried=origin.tried,
        missing_setting=origin.missing_setting, status=origin.status,
    )


def route_permission(route: APIRoute) -> tuple[str, bool] | None:
    """Find the one requires() declaration in a FastAPI dependency tree."""
    found: list[tuple[str, bool]] = []

    def visit(dependant) -> None:
        call = dependant.call
        name = getattr(call, "_permission_name", None)
        if name:
            found.append((name, bool(getattr(call, "_permission_has_object", False))))
        for child in dependant.dependencies:
            visit(child)

    visit(route.dependant)
    if len(found) > 1:
        raise RuntimeError(f"route {route.path} declares multiple permissions: {found}")
    return found[0] if found else None


def validate_routes(app, *, strict_undeclared: bool = True) -> None:
    """Refuse catalogue drift at startup; optionally refuse unmarked routes.

    The service starts with the strict form: every route declares a
    permission. The default-deny middleware stays behind it for a route that
    appears while the service is running.
    """
    routes = [r for r in app.routes if isinstance(r, APIRoute)]
    declarations: dict[str, bool] = {}
    for route in routes:
        if route.path in {"/api", "/api/health"}:
            continue
        declared = route_permission(route)
        if declared is None:
            if strict_undeclared:
                raise RuntimeError(f"no permission declared for route {route.path}")
            continue
        name, has_object = declared
        if name in declarations and declarations[name] != has_object:
            raise RuntimeError(f"permission {name} has inconsistent object declarations")
        declarations[name] = has_object

    if not declarations:
        return
    with cursor() as cur:
        cur.execute(
            "SELECT name, object_axis FROM sn_config.permissions WHERE name = ANY(%s)",
            (list(declarations),),
        )
        catalog = {row["name"]: row["object_axis"] for row in cur.fetchall()}
    for name, has_object in declarations.items():
        if name not in catalog:
            raise RuntimeError(f"permission {name} is absent from sn_config.permissions")
        if catalog[name] != "none" and not has_object:
            raise RuntimeError(f"permission {name} requires object for axis {catalog[name]}")


def undeclared_route(request: Request) -> str | None:
    """Find an API route that was not declared yet (4b default-deny path)."""
    for route in request.app.routes:
        if not isinstance(route, APIRoute):
            continue
        match, _ = route.matches(request.scope)
        if match is Match.FULL and route.path not in {"/api", "/api/health"}:
            if route_permission(route) is None:
                return route.path
    return None


def requires(
    name: str,
    object: Optional[Callable[[Request], Optional[str]]] = None,
) -> Callable[..., Principal]:
    """FastAPI dependency that gates a route on one permission.

    Usage:

        @router.post(
            "/api/agents",
            dependencies=[Depends(requires("agents.create"))],
        )

    Passes the Principal straight back when admitted, so a route that already
    takes `principal: Principal = Depends(get_principal)` can be rewritten
    as `principal: Principal = Depends(requires("agents.create"))` without a
    second DB round-trip for identity.

    An object-axis permission needs an `object` callable that extracts the
    object id from the request (path param, body field, etc.). The callable
    runs AFTER identity resolution and before the permission check, so a
    missing id surfaces as a 400, not a 403.

    Identity is resolved here and not declared as a dependency: a node service
    listed for the permission has no account, and a dependency on
    `get_principal` would refuse it before its origin was looked at. A route
    therefore receives the Principal when an account resolved, and None when
    the call was admitted by its origin alone. Only a permission that is listed
    for a service, or admitted by trusted origin, can ever hand a route None.
    """
    if object is not None and (name in TRUSTED_ORIGIN_PERMISSIONS or name == SETUP_PERMISSION):
        raise ValueError(
            f"requires('{name}') is admitted by origin and takes no object")

    def _dep(request: Request) -> Optional[Principal]:
        principal, refusal = _resolve_identity(request)

        object_id: Optional[str] = None
        if object is not None and principal is not None:
            try:
                object_id = object(request)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 -- surface the shape
                raise HTTPException(
                    status_code=400,
                    detail=f"requires('{name}') object fetcher raised: {exc!r}",
                )
            if object_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"requires('{name}') needs an object id for axis "
                        "but the fetcher returned None"
                    ),
                )

        decision = decide_permission(
            request, principal, name, object_id,
            str(refusal.detail)[:200] if refusal is not None else None)
        if decision is None:
            raise refusal

        if principal is not None:
            decision.actor_id = principal.actor_id
            decision.actor_kind = principal.actor_kind
            actor = principal.actor_id or principal.username
        elif decision.channel == "service":
            actor = f"component:{decision.origin_principal}"
        elif decision.channel:
            actor = f"system:{decision.channel}"
        else:
            # A fixed name: the event log registers every actor it is given,
            # and what an unverified caller typed must not become one.
            actor = "system:unidentified-caller"
        _audit(actor, decision)
        _refuse_unless_allowed(decision)
        return principal

    _dep.__name__ = f"requires_{name.replace('.', '_')}"
    _dep._permission_name = name
    _dep._permission_has_object = object is not None
    return _dep


def _resolve_identity(request: Request):
    """(Principal, None) or (None, the refusal identity raised).

    Because identity is resolved here and not through FastAPI, the override
    map FastAPI would have consulted is read here too. Without that an
    application that substitutes identity would get the substitute in its
    handlers and the real one in the permission check.

    Only a 403 means "no account behind this call", which the origin may still
    admit. Anything else identity raises is not about the caller: a 503 says
    the node cannot verify a proof it was shown because a setting is missing.
    That answer is identity's to give, and turning it into a refusal of the
    caller would hide the setting an operator has to fix.
    """
    override = request.app.dependency_overrides.get(get_principal)
    try:
        if override is not None:
            return override(), None
        return get_principal(request, request.headers.get("remote-user")), None
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        return None, exc


def _audit(actor: str, decision: PermissionDecision) -> None:
    """Record one decision, allowed or not.

    The event log is the single place this node reconstructs who did what, so
    denials matter as much as admits (an attack sweeps the matrix with
    denials). A failed write is logged and never changes the decision.
    """
    try:
        with cursor(commit=True) as cur:
            emit(
                cur,
                actor=actor,
                event_type=(
                    "auth.permission_granted"
                    if decision.allowed
                    else "auth.permission_denied"
                ),
                entity_type="permission",
                entity_id=decision.permission,
                payload=decision.as_audit_payload(),
            )
    except Exception as exc:  # noqa: BLE001 -- never fail-closed on audit
        log.warning(
            "auth.permission_%s audit emit failed for %s: %r",
            "granted" if decision.allowed else "denied",
            decision.permission,
            exc,
        )


def _refuse_unless_allowed(decision: PermissionDecision) -> None:
    if decision.allowed:
        return
    if decision.axis == "unknown":
        # A missing catalogue row is a deployment/configuration failure,
        # not an actor denial. Startup normally catches it first.
        status = 500
    elif decision.status:
        status = decision.status
    elif decision.missing_setting:
        # The node cannot verify the origin it was shown. Retrying does not
        # help and the caller is not at fault, so this is not a 403.
        status = 503
    else:
        status = 403
    raise HTTPException(status_code=status, detail=decision.reason)
