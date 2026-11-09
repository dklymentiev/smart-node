"""Identity is forwarded by Traefik+Authum via the Remote-User header.

We only trust this header when the request originates from loopback or the
Docker network (SN_DOCKER_CIDR) -- the controller is bound to the Docker
bridge gateway specifically to make this allowlist meaningful. Anyone
reaching the socket directly is by definition already on the host.

`get_actor()` returns the raw identity STRING used for the append-only event
log. `get_principal()` resolves that identity to a tenant-scoped account
(`sn_config.users`) and is the authorization boundary for the read/write API:
who the caller is, which tenant they belong to, and what they may see.

HOW STRONGLY WE KNOW IT
-------------------------------
The source-network check answers "may this caller assert an identity", and it
is not the same question as "is this caller who they say they are". Every agent
on the node reaches the controller from exactly the trusted network, so an
agent that sends `Remote-User: admin` IS the owner as far as the old code was
concerned. The only thing stopping it was a sentence in the agent's own
instructions -- prompt text, past which anything that edits the prompt is.

So a Principal now carries HOW its identity was established:

  session   the reverse proxy authenticated a browser session and stamped its
            own shared proof on the request. An agent cannot produce this: the
            secret lives in core-api's EnvironmentFile (root, 0600) and in the
            Traefik route, neither of which is readable by an agent user.
  proved    the kernel named the caller. core-api also listens on a unix
            socket; SO_PEERCRED reports the peer's uid, which the peer never
            sends and therefore cannot forge, and the uid maps to an agent row
            through os_user. On that socket the Remote-User header is ignored
            entirely rather than merely outranked.
  asserted  a Remote-User from the trusted network with nothing proving it.
            Good enough for attribution and for ordinary work; NOT good enough
            for anything the owner is relied upon to have decided.

WHY BOTH, AND WHY session STILL OUTRANKS proved. They answer different
questions: `proved` says which PROCESS is on the other end, `session` says a
human authenticated in a browser. An agent process is exactly what `proved`
identifies and exactly what an approval must not come from.

`require_session` is the gate for the second kind of operation. This is the
bottom rung of the trust scale the whole node is moving to: one place
computes how strongly an identity is known, and callers ask for a level rather
than each re-deriving it from headers.

FAIL CLOSED. With no secret configured nothing can reach `session`, so
approvals are refused for EVERYONE including the owner, and the error names
the missing setting. That is deliberate: a gate that quietly passes when
misconfigured is the hole it was built to close, and this failure is visible
in one attempt while the other is visible only afterwards.
"""

import hmac
import hashlib
import ipaddress
import logging
import os
import pwd
import re
import socket
import struct
import time
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request

log = logging.getLogger("core-api.auth")

# Only positive socket credential resolutions are cached. A newly registered
# credential becomes usable immediately; a revoked one expires within 30s.
_socket_credential_cache: dict[str, tuple[float, str]] = {}


def invalidate_socket_credential(os_user: str) -> None:
    """Drop this process's cached socket resolution after a credential edit."""
    _socket_credential_cache.pop(os_user, None)

# Docker network CIDR: set SN_DOCKER_CIDR in the deployment environment.
# If unset, Docker-network trust is disabled (fail closed); loopback and
# SN_TRUSTED_CIDRS still apply. The installer sets this from the live
# docker network inspect output so the value is always node-specific.
_docker_cidr = os.environ.get("SN_DOCKER_CIDR", "")
DOCKER0_NET = ipaddress.ip_network(_docker_cidr, strict=False) if _docker_cidr else None

# Ordered weakest to strongest. One place says what beats what, so a caller asks
# for a level instead of re-deriving it from headers (the earlier change's vocabulary).
STRENGTH_ORDER = ("untrusted", "asserted", "proved", "session")


def _strength_at_least(have: str, want: str) -> bool:
    try:
        return STRENGTH_ORDER.index(have) >= STRENGTH_ORDER.index(want)
    except ValueError:
        return False

# Stamped on the request by the Traefik route that sits behind the Authum
# middleware, and by nothing else. Its value is the shared secret below.
PROXY_PROOF_HEADER = "X-SN-Proxy-Auth"

# ---------------------------------------------------------------------------
# A SERVICE ACTING FOR AN AGENT.
#
# The TCP boundary makes an agent's name unforgeable by refusing it over TCP: "an agent that
# types another agent's name is measured against the other's permissions, and a
# capability any process on the trusted network can type is not one." That was
# right, and it left a hole nobody filled: some services exist ONLY to act for an
# agent. The voice runtime is the product's front line -- a person speaks, an
# agent must delegate -- and it is a container, not the agent's own process. From
# 2026-08-14 its every delegation answered 403, and the caller was told in prose
# that something had gone wrong. It went unnoticed for seven weeks because nobody
# delegated by voice in between.
#
# The socket is not the answer, and that was checked rather than assumed: over it
# Remote-User is not consulted AT ALL, so voice would be named as the voice
# service and still could not originate as alex. The same refusal applies
# for message_watcher.
#
# So: the service proves ITSELF with a shared secret -- the same shape as the
# proxy proof above, constant-time compared for the same reason -- and then names
# the agent it acts for. Two facts, both checked, both recorded. The strictness
# lives in the authorisation list rather than in the transport: a transport that
# happens to be hard to reach is not a permission model.
SERVICE_AUTH_HEADER = "X-SN-Service-Auth"   # the shared secret
SERVICE_NAME_HEADER = "X-SN-Service"        # which service is calling
ON_BEHALF_HEADER = "X-SN-On-Behalf-Of"      # the agent it acts for

# Gateway may report budget and turn-limit notices for the configured owner.
# This credential is separate from the agent-delegation secret and is valid
# only for these three POST paths. /api/messages/send is further restricted in
# its handler to the configured owner and the fixed pre-warning shape.
_GATEWAY_OWNER_PATHS = (
    re.compile(r"^/api/messages/send$"),
    re.compile(r"^/api/budgets/[0-9]+/notify-exhaustion$"),
    re.compile(r"^/api/limits/notify-turn-cutoff$"),
)


def gateway_owner_delegation(request: Request) -> str | None:
    """The configured owner when the gateway proves a listed notice call."""
    if request.headers.get(SERVICE_NAME_HEADER, "") != "gateway":
        return None
    # The gateway also calls as ITSELF, with the service credential and no one
    # named: its reads of an agent's prompt. That is not an owner notice and
    # is not judged as one; the permission check decides it by the service
    # list. Only a call that names somebody is a delegation.
    if not request.headers.get(ON_BEHALF_HEADER, "").strip():
        return None
    if not trusted_client(request):
        raise HTTPException(403, "gateway notice source is untrusted")
    secret = os.environ.get("SN_GATEWAY_CORE_API_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "gateway core-api proof is not configured")
    offered = request.headers.get(SERVICE_AUTH_HEADER, "")
    if not offered or not hmac.compare_digest(offered, secret):
        raise HTTPException(403, "gateway notice proof is invalid")
    owner = os.environ.get("BUDGET_NOTIFY_ACTOR", "").strip()
    named = request.headers.get(ON_BEHALF_HEADER, "").strip()
    if (not owner or named != owner
            or request.method.upper() != "POST"
            or not any(p.fullmatch(request.url.path) for p in _GATEWAY_OWNER_PATHS)):
        raise HTTPException(403, "gateway notice is outside its delegated scope")
    remote = request.headers.get("remote-user", "").strip()
    if remote and remote != owner:
        raise HTTPException(403, "gateway notice has conflicting actor names")
    return owner


# A node service acting for the OWNER. The planner and the dispatcher raise
# owner-level calls no browser session stands behind: a task notification, the
# desk delivery decision, the release of a reply the owner approved. Each proves
# itself with the service credential and may name one human only, the owner this
# node was configured with (SN_OPERATOR_ACTOR, written by the installer from the
# users table). The list below is every call that may be made this way; a call
# outside it is refused even with a valid credential, so holding the credential
# does not make a service the owner.
#
# Reads are absent on purpose: a safe method needs no proof, and a credential
# should not travel on a request that does not need it.
_OPERATOR_SERVICE_PATHS = {
    "planner": (
        ("POST", re.compile(r"^/api/notify$")),
    ),
    "brin": (
        ("POST", re.compile(r"^/api/notify$")),
        ("POST", re.compile(r"^/api/messages/reply$")),
        ("POST", re.compile(r"^/api/messages/[0-9]+/deliver-desk$")),
        ("PATCH", re.compile(r"^/api/messages/[0-9]+/state$")),
        ("POST", re.compile(r"^/api/integrations/google/calendar/events$")),
    ),
}


def operator_actor() -> str:
    """The node owner's identity, as the installer recorded it. Empty when the
    node was never told: callers refuse and name the setting, they do not guess."""
    return os.environ.get("SN_OPERATOR_ACTOR", "").strip()


def operator_service_delegation(request: Request) -> "tuple[str, str] | None":
    """(service, owner) when a listed service proves a listed call for the owner.

    None when the request is not an owner delegation at all. The same three
    headers also carry a service acting for an AGENT; that case is left to
    acting_for() and must not be refused here.
    """
    service = request.headers.get(SERVICE_NAME_HEADER, "").strip().lower()
    allowed = _OPERATOR_SERVICE_PATHS.get(service)
    if allowed is None:
        return None
    owner = operator_actor()
    named = request.headers.get(ON_BEHALF_HEADER, "").strip()
    if owner and named != owner:
        return None
    method = request.method.upper()
    in_scope = any(method == m and p.fullmatch(request.url.path) for m, p in allowed)
    if not owner and (not in_scope or acting_for(request)):
        return None
    if not trusted_client(request):
        raise HTTPException(403, "operator service source is untrusted")
    secret = service_secret()
    if not secret:
        log.error("SN_SERVICE_AUTH_SECRET is not set -- no service can prove a "
                  "call made for the owner (re-run core-api.sh)")
        raise HTTPException(503, "operator service proof is not configured: "
                                 "SN_SERVICE_AUTH_SECRET is not set")
    offered = request.headers.get(SERVICE_AUTH_HEADER, "")
    # Bytes, because compare_digest raises on a non-ASCII str and a header can
    # carry one: the comparison must fail closed, not fail with a 500.
    if not offered or not hmac.compare_digest(offered.encode("utf-8", "replace"),
                                              secret.encode("utf-8")):
        raise HTTPException(403, "operator service proof is invalid")
    if not owner:
        log.error("SN_OPERATOR_ACTOR is not set -- %s cannot act for the owner "
                  "on %s %s (re-run core-api.sh)", service, method, request.url.path)
        raise HTTPException(503, "operator service delegation is not configured: "
                                 "SN_OPERATOR_ACTOR is not set")
    if not in_scope:
        raise HTTPException(403, "operator service call is outside its delegated scope")
    remote = request.headers.get("remote-user", "").strip()
    if remote and remote != owner:
        raise HTTPException(403, "operator service call has conflicting actor names")
    return (service, owner)


def _record_owner_delegation(actor: str, entity: str, service: str,
                             request: Request) -> None:
    """One event per call a service made as the owner, so the journal can tell
    the owner acting from a service acting in their name.

    Once per REQUEST, however many times identity is resolved within it. The
    permission check resolves the caller and so may the handler behind it; both
    reach this point for the same call, and two rows would read as two calls.
    """
    state = getattr(request, "state", None)
    if state is not None:
        if getattr(state, "sn_owner_delegation_recorded", False):
            return
        state.sn_owner_delegation_recorded = True
    from db import cursor
    from events_emit import emit
    with cursor(commit=True) as cur:
        emit(cur, actor=actor, event_type="identity.delegated_owner",
             entity_type="identity", entity_id=entity,
             payload={"service": service, "method": request.method,
                      "path": request.url.path})


def service_secret() -> str:
    """The shared proof a node service stamps on its requests. Read per call, so
    a rotated value needs only a restart and not a code path that cached it."""
    return os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()


def verified_service(request: Request) -> str | None:
    """The service behind this request, or None.

    Both halves must be present: the secret proves that SOMETHING authorised is
    calling, the name says which one. A secret without a name would let any
    holder impersonate every service -- the same failure this exists to prevent,
    one level down."""
    secret = service_secret()
    if not secret:
        return None
    offered = request.headers.get(SERVICE_AUTH_HEADER, "")
    if not offered or not hmac.compare_digest(offered, secret):
        return None
    name = (request.headers.get(SERVICE_NAME_HEADER, "") or "").strip().lower()
    return name or None


def service_may_act_for(service: str, agent: str) -> bool:
    """Is `service` allowed to originate as `agent`?

    Declared in the environment as SN_SERVICE_AGENTS, one clause per service:

        SN_SERVICE_AGENTS="voice:alex,architect;runner:*"

    A clause naming `*` allows any agent, for a service whose whole job is to act
    for whoever is being woken. An UNLISTED service may act for nobody: the
    default is refusal, so adding a service is deliberate and forgetting one
    fails closed rather than opening quietly.

    Deliberately not a table yet. A table earns its keep when the panel edits it;
    until then it costs a schema change (B02 lays down an end state, it does not
    migrate) and buys nothing. This can be evaluated if a migration is needed.
    """
    if not service or not agent:
        return False
    raw = os.environ.get("SN_SERVICE_AGENTS", "")
    for clause in raw.split(";"):
        svc, _, agents = clause.partition(":")
        if svc.strip().lower() != service.strip().lower():
            continue
        allowed = {a.strip().lower() for a in agents.split(",") if a.strip()}
        return "*" in allowed or agent.strip().lower() in allowed
    return False


def acting_for(request: Request) -> "tuple[str, str] | None":
    """(service, agent) when a verified service names an agent it may act for.

    None when anything is missing or unauthorised, and deliberately silent about
    WHICH: a caller able to tell "wrong secret" from "not allowed for that agent"
    has an oracle to probe.
    """
    svc = verified_service(request)
    if not svc:
        return None
    agent = (request.headers.get(ON_BEHALF_HEADER, "") or "").strip().lower()
    if not agent or not service_may_act_for(svc, agent):
        return None
    # A wildcard clause means "any agent", not "any identity". Without this a
    # service listed with '*' could name the owner and be handed a proved owner
    # principal on every route.
    from db import cursor                            # lazy: no import-time DB dep
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (agent,),
        )
        if cur.fetchone() is None:
            return None
    return (svc, agent)


# Extra trusted source networks, supplied via env (comma-separated CIDRs or IPs).
# core-api binds only to the Docker bridge gateway, so anything reaching the socket
# is already host-local. Host *processes* (e.g. the studio companion, a native
# systemd service -- not a container) reach that bind via the host's own primary
# interface, so their source is the node's LAN IP rather than a Docker-network
# address. SN_TRUSTED_CIDRS lets the deployment declare that host IP as trusted
# without hardcoding it in code.
def _parse_trusted_cidrs():
    raw = os.environ.get("SN_TRUSTED_CIDRS", "")
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return nets


EXTRA_TRUSTED = _parse_trusted_cidrs()

# Roles permitted to read across all tenants. Everyone else is scoped to their
# own tenant_id. `role` is free-text in sn_config.users; this set is the single
# place that grants cross-tenant visibility.
CROSS_TENANT_ROLES = {"super-admin"}


def peer_credentials(request: Request):
    """(pid, uid, gid) of the process on the other end, or None.

    Only a unix domain socket can answer this. The kernel fills it in at connect
    time and the caller never sends it, so unlike every header it cannot be
    typed by the process it describes.

    The socket is reached through the ASGI receive callable's owning protocol
    object. That is uvicorn's internal shape rather than a documented ASGI
    extension, so every step is guarded and any surprise means None -- which
    costs the caller the `proved` level and nothing else. Never let a failure
    here invent an identity.
    """
    try:
        proto = request.receive.__self__            # uvicorn RequestResponseCycle
        transport = getattr(proto, "transport", None)
        if transport is None:
            return None
        sock = transport.get_extra_info("socket")
        if sock is None or sock.family != socket.AF_UNIX:
            return None
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return pid, uid, gid
    except Exception as e:                          # noqa: BLE001 -- see docstring
        log.debug("peer credentials unavailable: %r", e)
        return None


def peer_identity(request: Request) -> str | None:
    """The agent slug the KERNEL says is calling, or None.

    uid -> passwd name -> sn_config.agents.os_user -> slug. A uid that owns no
    agent row (root, a service account, an operator shell) resolves to nothing:
    this function answers one question only -- which agent is on the other end
    of this socket -- and a wrong answer would be worse than no answer.
    """
    creds = peer_credentials(request)
    if creds is None:
        return None
    _pid, uid, _gid = creds
    try:
        os_user = pwd.getpwuid(uid).pw_name
    except KeyError:
        log.warning("peer uid %s has no passwd entry; refusing to name it", uid)
        return None
    from db import cursor                            # lazy: no import-time DB dep
    with cursor() as cur:
        cur.execute(
            "SELECT slug FROM sn_config.agents "
            "WHERE os_user = %s AND archived_at IS NULL",
            (os_user,),
        )
        row = cur.fetchone()
    return row["slug"] if row else None


def peer_operator_identity(request: Request) -> str | None:
    """Registered non-agent actor for a kernel-proved socket peer.

    Kept separate from :func:`peer_identity`: an operator OS account is not an
    agent, and returning its name from the agent resolver corrupts secrets
    authorization and the unknown-peer audit semantics. An OS account with no
    registered credential maps to the configured owner only when it is listed
    in SN_OPERATOR_USERS (root by default).
    """
    creds = peer_credentials(request)
    if creds is None:
        return None
    try:
        os_user = pwd.getpwuid(creds[1]).pw_name
    except KeyError:
        return None
    cached = _socket_credential_cache.get(os_user)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    from db import cursor
    with cursor() as cur:
        cur.execute(
            "SELECT u.email FROM sn_config.actor_credentials c "
            "JOIN sn_config.users u ON u.actor_id = c.actor_id "
            "WHERE c.cred_type='os_user' AND c.cred_value=%s "
            "AND c.scope='socket' AND u.active=true",
            (os_user,),
        )
        matches = cur.fetchall()
    if len(matches) == 1:
        email = matches[0]["email"]
        _socket_credential_cache[os_user] = (now + 30, email)
        return email
    if len(matches) > 1:
        log.error("socket OS user %s resolves to multiple active users", os_user)
        return None
    operators = {
        item.strip().lower()
        for item in os.environ.get("SN_OPERATOR_USERS", "root").split(",")
        if item.strip()
    }
    if os_user.lower() not in operators:
        return None
    owner = operator_actor()
    if not owner:
        # No built-in name: a guessed owner would be an identity nobody set.
        log.error("SN_OPERATOR_ACTOR is not set -- socket peer %s cannot be "
                  "resolved to the node owner (re-run core-api.sh)", os_user)
        return None
    return owner


def trusted_client(request: Request) -> bool:
    """True when this caller is one the node trusts to read privileged material.

    Three ways to qualify, in the order they are checked:

    * the UNIX SOCKET, which has no client address at all. It is the strongest identity
      here -- the kernel names the calling process and no header is consulted --
      so a socket caller is trusted by definition.
    * loopback.
    * a declared network: the docker bridge, plus anything in SN_TRUSTED_CIDRS. That
      variable already exists for precisely this case; see EXTRA_TRUSTED above.

    This is the expression that was inlined in get_actor, lifted out so that a second
    caller cannot drift from it. A hardcoded subnet in a second file is how
    /api/secrets/resolve came to refuse every companion on this estate.
    """
    if peer_credentials(request) is not None:
        return True
    client_ip = request.client.host if request.client else ""
    if not client_ip:
        return True
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return (ip.is_loopback or (DOCKER0_NET is not None and ip in DOCKER0_NET)
            or any(ip in net for net in EXTRA_TRUSTED))


def get_actor(request: Request, remote_user: str | None = Header(default=None)) -> str:
    """Return the authenticated user name. Falls back to 'anonymous' if
    no header was forwarded (e.g. direct curl from host for testing).

    ON THE UNIX SOCKET THE HEADER IS NOT CONSULTED AT ALL. Not preferred
    against, not used as a fallback: ignored. A caller that reaches core-api
    over the socket is named by the kernel, and letting a header win -- or fill
    in when the lookup finds nothing -- would put the forgeable path back
    underneath the unforgeable one, which is the whole defect.
    """
    # get_actor is called BOTH through Depends and directly. A direct call leaves
    # `remote_user` as the Header() sentinel object rather than a string, and the sentinel
    # is truthy, so `remote_user or "anonymous"` below used to return the object itself.
    # In routes/secrets.py that reached emit() and raised
    #     TypeError: argument of type 'Header' is not iterable
    # AFTER the secret had already been written: a 500 for a successful write, with no
    # audit row. Read the header ourselves when we were not handed a string, so every
    # caller gets a name whichever way it arrives.
    if not isinstance(remote_user, str):
        remote_user = request.headers.get("remote-user")

    peer = peer_credentials(request)
    if peer is not None:
        named = peer_identity(request)
        if named:
            return named
        # On the socket and not an agent: say so plainly rather than borrowing
        # the header. The socket's mode admits root and the agent group only.
        _pid, uid, _gid = peer
        log.info("socket caller uid=%s owns no agent row; identity is unknown", uid)
        return "unknown-peer"

    client_ip = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        ip = None

    trusted = ip is not None and (
        ip.is_loopback or (DOCKER0_NET is not None and ip in DOCKER0_NET)
        or any(ip in net for net in EXTRA_TRUSTED)
    )
    if not trusted:
        return "untrusted"
    # Some legacy mutation routes call get_actor() directly instead of the
    # richer get_principal() dependency (calendar events are one example).
    # Apply the human TCP rule here as well, so those routes cannot bypass it.
    if (remote_user and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            and identity_strength(request) == "asserted"):
        delegation = operator_service_delegation(request)
        from db import cursor
        with cursor() as cur:
            cur.execute(
                "SELECT a.kind, u.role FROM sn_config.users u "
                "LEFT JOIN sn_core.actors a ON a.id=u.actor_id "
                "WHERE u.email=%s AND u.active=true LIMIT 1",
                (remote_user,),
            )
            row = cur.fetchone()
        if delegation:
            # The same owner test get_principal applies to a delegated name.
            if not row or row["role"] != "super-admin" or row["kind"] != "human":
                raise HTTPException(403, "delegated owner is not the human node owner")
            _record_owner_delegation("user:" + delegation[1], delegation[1],
                                     delegation[0], request)
            return delegation[1]
        if row and (row["kind"] in (None, "human") or
                    (row["kind"] == "component" and row["role"] in ("super-admin", "admin"))):
            from events_emit import emit
            with cursor(commit=True) as cur:
                emit(cur, actor="user:" + remote_user,
                     event_type="auth.human_tcp_assertion_denied",
                     entity_type="identity", entity_id=remote_user,
                     payload={"method": request.method, "path": request.url.path,
                              "strength": "asserted"})
            raise HTTPException(
                403,
                "human mutation with asserted Remote-User requires a signed-in "
                "session, actor bearer credential, or proved Unix socket peer",
            )
    return remote_user or "anonymous"


def get_provisioning_actor(request: Request,
                           remote_user: str | None = Header(default=None)) -> str:
    """get_actor, for a route that hands the caller's name to admin-runner.

    get_actor names a non-agent socket peer 'unknown-peer', so that the secrets
    boundary and the audit trail never take a service account for a person.
    admin-runner's owner gate resolves the name it is given against the users
    table, and 'unknown-peer' is nobody there: the installer, running as root
    on the socket, could not create the node's first agent. Where the kernel
    proves the configured operator, that peer is the owner. Every other answer
    is get_actor's own.
    """
    actor = get_actor(request, remote_user)
    if actor == "unknown-peer":
        return peer_operator_identity(request) or actor
    return actor


def proxy_secret() -> str:
    """The shared proof the reverse proxy stamps on an authenticated request.

    Read per call rather than at import: the installer writes it into the
    EnvironmentFile, and reading it here means a restarted service picks up a
    rotated value without a code path that caches the old one.
    """
    return os.environ.get("SN_PROXY_AUTH_SECRET", "").strip()


def identity_strength(request: Request) -> str:
    """How strongly this request's identity is established.

    'session' -- the reverse proxy authenticated a browser session and stamped
                 its shared proof. Compared with `hmac.compare_digest`: an
                 ordinary `==` on a secret leaks its prefix through timing, and
                 this one is reachable by anything on the node, which is the
                 entire population the gate exists to exclude.
    'proved'   -- the kernel named the caller over a unix socket.
    'asserted' -- a Remote-User from the trusted network with nothing behind it.

    Session outranks proved on purpose: proved says WHICH PROCESS is calling,
    session says a human authenticated. They answer different questions, and the
    operations gated on `session` are the ones a person is relied upon to have
    decided.
    """
    secret = proxy_secret()
    if secret:
        offered = request.headers.get(PROXY_PROOF_HEADER, "")
        if offered and hmac.compare_digest(offered, secret):
            return "session"
    if peer_credentials(request) is not None and (
        peer_identity(request) or peer_operator_identity(request)
    ):
        return "proved"
    return "asserted"


def agent_identity_unproved(request: Request, actor: str,
                            strength: str | None = None) -> bool:
    """True when `actor` names an agent row and nothing proved the caller is it.

    WHY AN AGENT NAME IS DIFFERENT FROM ANY OTHER. routes/planner.py resolves the
    caller from the name, looks it up in sn_config.agents, and evaluates THAT
    agent's grants. So an agent that types another agent's name is measured
    against the other's permissions -- which makes the name a capability, not a
    label, and a capability that any process on the trusted network can type is
    not one.

    Over the socket the kernel answers the question instead ('proved'), and a
    human in the panel answers it differently ('session'). Both are acceptable
    ways to carry an agent name; a bare header is not.

    Deliberately narrow. Service identities (runner, voice, gateway,
    cron-runner) are NOT rows in sn_config.agents, so they are unaffected and
    keep the TCP bind they already use.
    """
    if not actor or actor in ("", "anonymous", "untrusted", "unknown-peer"):
        return False
    if strength is None:
        strength = identity_strength(request)
    if _strength_at_least(strength, "proved"):
        return False
    from db import cursor                            # lazy: no import-time DB dep
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (actor,),
        )
        return cur.fetchone() is not None


@dataclass
class Principal:
    """The authenticated, tenant-scoped caller behind an API request."""

    username: str
    user_id: int
    tenant_id: int
    role: str
    actor_id: str | None = None
    actor_kind: str | None = None
    # How the identity was established -- 'session' | 'proved' | 'asserted'.
    # See the module docstring. Defaulted to the WEAKEST useful level, so a
    # Principal built in a test or by an older caller is never mistaken for
    # proof: a missing field must not read as evidence.
    strength: str = "asserted"
    # The service that carried this identity, when one did. None means
    # the caller is acting as itself. Recorded rather than hidden: "alex sent
    # this" and "the voice line sent this as alex because someone was on a call"
    # are different facts, and a journal that cannot tell them apart cannot
    # answer who caused what.
    acting_service: str | None = None

    @property
    def cross_tenant(self) -> bool:
        return self.role in CROSS_TENANT_ROLES

    @property
    def proven(self) -> bool:
        """True only for an identity the reverse proxy vouched for."""
        return self.strength == "session"


def get_principal(request: Request, remote_user: str | None = Header(default=None)) -> Principal:
    """Resolve the forwarded identity to an active sn_config.users account.

    The identity string from get_actor() (the Authum Remote-User, or the local
    'anonymous'/'untrusted' stand-ins) is matched against sn_config.users.email.
    There is NO default tenant: an identity with no active user row is rejected
    with 403. In production the installer/Authum seeds real operator rows; the
    local stack seeds stand-ins in db/init/99_local_test_actors.sql.
    """
    # Imported lazily so the auth module has no import-time DB dependency.
    from db import cursor

    authorization = request.headers.get("authorization", "")
    bearer_actor = None
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(status_code=403, detail="empty bearer credential")
        digest = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        with cursor() as cur:
            cur.execute(
                "SELECT actor_id FROM sn_config.actor_credentials "
                "WHERE cred_type='bearer_token' AND cred_value=%s AND scope='admin'",
                (digest,),
            )
            matches = cur.fetchall()
        if len(matches) != 1:
            raise HTTPException(status_code=403, detail="bearer credential not recognised")
        bearer_actor = matches[0]["actor_id"]

    # (service, owner) when a node service proved a listed call for the owner.
    delegation = None
    if bearer_actor is None:
        _gateway_owner = gateway_owner_delegation(request)
        delegation = (("gateway", _gateway_owner) if _gateway_owner
                      else operator_service_delegation(request))
    delegated_owner = delegation[1] if delegation else None
    username = (delegated_owner or get_actor(request, remote_user)) if bearer_actor is None else ""

    # A socket call from the configured operator resolves to its users row.
    # get_actor still names other non-agent peers unknown-peer for the secrets
    # boundary and audit. The kernel identifies the operator's process here.
    if bearer_actor is None and username == "unknown-peer" and peer_credentials(request) is not None:
        _operator = peer_operator_identity(request)
        if _operator:
            username = _operator

    # A service acting for an agent. Checked BEFORE the refusal below,
    # because this is the one way an agent name may arrive over TCP: not typed,
    # but carried by something that proved itself and was authorised to carry it.
    # The resulting Principal IS the agent -- everything downstream measures the
    # agent's grants, which is correct, that is who the work is being done as --
    # and `acting_service` records who carried it, so the row can say later that
    # a person on a line caused this and not the agent acting alone.
    _acting = acting_for(request) if bearer_actor is None else None
    _service = None
    if _acting:
        _service, username = _acting

    # An agent name carried by nothing but a header is refused here, not only on
    # the planner proxy. This is the panel's own door; letting it through
    # would leave the forgeable path open one route over.
    if bearer_actor is None and not _acting and not delegated_owner and agent_identity_unproved(request, username):
        raise HTTPException(
            status_code=403,
            detail=f"'{username}' names an agent, and an agent identity is "
                   "accepted only over the core-api unix socket, where the "
                   "kernel establishes it")
    with cursor() as cur:
        cur.execute(
            "SELECT u.id, u.email, u.tenant_id, u.role, u.actor_id, a.kind AS actor_kind "
            "FROM sn_config.users u LEFT JOIN sn_core.actors a ON a.id = u.actor_id "
            "WHERE " + ("u.actor_id = %s" if bearer_actor else "u.email = %s") +
            " AND u.active = true",
            (bearer_actor or username,),
        )
        row = cur.fetchone()

    if row is None:
        raise HTTPException(status_code=403, detail=f"no active user for identity '{username}'")
    if row["tenant_id"] is None:
        raise HTTPException(status_code=403, detail=f"user '{username}' has no tenant assignment")
    if delegated_owner and (row["role"] != "super-admin" or row["actor_kind"] != "human"):
        raise HTTPException(403, "delegated owner is not the human node owner")

    if bearer_actor is not None:
        username = row["email"]

    principal = Principal(
        username=username,
        user_id=row["id"],
        tenant_id=row["tenant_id"],
        role=row["role"] or "",
        actor_id=row["actor_id"],
        actor_kind=row["actor_kind"],
        # A delegated identity is PROVED: a credential was verified and the
        # (service, agent) pair was authorised. It is not 'asserted' -- nothing
        # here was merely typed -- and not 'session', which means a human
        # authenticated and is reserved for what a person is relied upon to have
        # decided. A service must not inherit that.
        strength="proved" if (_acting or bearer_actor or delegated_owner) else identity_strength(request),
        acting_service=delegation[0] if delegation else _service,
    )
    # A trusted IP only proves where the packet arrived, not who set its
    # Remote-User header. Human mutations need session, bearer, or socket proof.
    # Safe-method reads retain their existing access while clients migrate.
    if (
        (principal.actor_kind in (None, "human") or
         (principal.actor_kind == "component" and principal.role in ("super-admin", "admin")))
        and principal.strength == "asserted"
        and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    ):
        from events_emit import emit
        with cursor(commit=True) as cur:
            emit(
                cur,
                actor=principal.actor_id or "user:" + principal.username,
                event_type="auth.human_tcp_assertion_denied",
                entity_type="identity",
                entity_id=principal.actor_id or principal.username,
                payload={"method": request.method, "path": request.url.path,
                         "strength": principal.strength},
            )
        raise HTTPException(
            status_code=403,
            detail="human mutation with asserted Remote-User requires a signed-in "
                   "session, actor bearer credential, or proved Unix socket peer",
        )
    if delegation:
        _record_owner_delegation(principal.actor_id or "user:" + principal.username,
                                 principal.actor_id or principal.username,
                                 delegation[0], request)
    return principal


def require_session(principal: Principal = Depends(get_principal)) -> Principal:
    """Admit only an identity the reverse proxy authenticated.

    Use this for anything the owner is RELIED UPON to have decided -- an
    approval, a grant of access, anything that commits the business to an
    outside party. Everything else takes `get_principal`: gating ordinary work
    on a browser session would lock out the agents that are supposed to do it.

    The two refusals are deliberately different messages. One is a caller who
    is not the proxy; the other is a node whose proxy proof was never
    configured, which is an operator problem and unfixable by retrying.
    """
    if principal.proven:
        return principal
    if not proxy_secret():
        log.error("SN_PROXY_AUTH_SECRET is not set -- no request can be proven "
                  "to come from an authenticated session, so every gated "
                  "operation is refused (re-run core-api.sh)")
        raise HTTPException(
            status_code=503,
            detail="this node cannot verify authenticated sessions: "
                   "SN_PROXY_AUTH_SECRET is not configured (re-run core-api.sh)")
    raise HTTPException(
        status_code=403,
        detail=f"'{principal.username}' is asserted, not authenticated -- this "
               "operation requires a signed-in session in the panel")
