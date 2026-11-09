"""The node's secret store over HTTP: /api/secrets.

One store for every credential on the node (lib/secret_store.py,
sn_config.secrets under pgcrypto): the gateway's provider keys and the
integrations agents and companions use. This route is the sanctioned write
path for people and the designated writer agents; companions and agents read
at the moment of use through /resolve.

Values are NEVER returned by any list, status or error response; only
/resolve and a version read return plaintext, and only to a proved socket
caller with a scoped grant or an authenticated owner session. Network
location by itself is never identity. Every write, every read and every
refusal is journalled with the actor.
"""

import logging
import os
import pwd
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from auth import (get_actor, identity_strength, peer_credentials,
                  peer_identity, trusted_client)
from db import cursor
from permissions import requires
from events_emit import emit
from lib import secret_store

log = logging.getLogger("core-api.secrets")

router = APIRouter()

REF_PATTERN = secret_store.REF_PATTERN

# Reserved top-level names from the sops era; a ref under them is refused so an
# import of an old file can never collide with its own bookkeeping.
META_KEYS = {"sops", "_meta", "_snaps"}

REQUESTS_WINDOW_DAYS = 30


class SecretSet(BaseModel):
    ref: str = Field(pattern=REF_PATTERN,
                     description="Dotted path, e.g. 'replicate.api_key', or a gateway key ref.")
    value: str = Field(min_length=1, max_length=8192,
                       description="Plaintext value. Never logged.")
    note: str | None = Field(default=None, max_length=200)

    @field_validator("ref")
    @classmethod
    def _no_meta_prefix(cls, v: str) -> str:
        head = secret_store.namespace_of(v)
        if head in META_KEYS:
            raise ValueError(f"top-level key '{head}' is reserved")
        return v


def _check_ref(ref: str) -> None:
    if not secret_store.valid_ref(ref):
        raise HTTPException(status_code=400, detail="invalid ref")
    if secret_store.namespace_of(ref) in META_KEYS:
        raise HTTPException(status_code=400, detail="reserved key")


def _trusted(request: Request) -> bool:
    return trusted_client(request)


def _csv_env(name: str, default: str = "") -> set[str]:
    return {v.strip().lower() for v in os.environ.get(name, default).split(",")
            if v.strip()}


def warn_legacy_writer_env() -> bool:
    """Log loudly at startup if the legacy writer variable is still set.

    SN_SECRETS_AGENT_WRITERS named writers by slug and is no longer read. A node
    that set it would otherwise lose agent secret writes silently; the warning
    puts the new variable's name in the operator's own log.
    """
    if os.environ.get("SN_SECRETS_AGENT_WRITERS") is None:
        return False
    log.warning(
        "SN_SECRETS_AGENT_WRITERS is set but no longer read: secret "
        "writers are named by capability in SN_SECRETS_WRITER_CAPABILITIES "
        "(default sysadmin,configurator). Move the setting there.")
    return True


def _peer_os_user(request: Request) -> str | None:
    """The socket peer's OS account, kernel-proved, or None off the socket."""
    creds = peer_credentials(request)
    if creds is None:
        return None
    try:
        return pwd.getpwuid(creds[1]).pw_name
    except KeyError:
        return None


def _agent_capability(slug: str) -> str | None:
    """The live agent's capability (its role), or None."""
    with cursor() as cur:
        cur.execute(
            "SELECT capability FROM sn_config.agents "
            "WHERE slug = %s AND archived_at IS NULL",
            (slug,),
        )
        row = cur.fetchone()
    return row["capability"] if row else None


def _secret_actor(request: Request) -> str:
    """A useful audit actor even for a non-agent socket peer.

    auth.get_actor deliberately calls such a peer ``unknown-peer`` because it
    answers an agent-identity question.  The secrets ledger needs the OS answer:
    a sudo/root write is not anonymous and must be distinguishable from an
    agent process.
    """
    slug = peer_identity(request)
    if slug:
        return slug
    os_user = _peer_os_user(request)
    if os_user:
        return f"os:{os_user}"
    actor = get_actor(request)
    if actor not in ("anonymous", "unknown-peer"):
        return actor
    host = request.client.host if request.client else "unknown"
    return f"anonymous@{host}"


def _deny(request: Request, operation: str, ref: str | None, reason: str) -> None:
    """Record a refused secrets operation, then fail closed."""
    actor = _secret_actor(request)
    log.warning("secret access denied: op=%s ref=%s actor=%s reason=%s",
                operation, ref or "-", actor, reason)
    try:
        with cursor(commit=True) as cur:
            emit(cur, actor=actor, event_type="secret.access_denied",
                 entity_type="secret", entity_id=ref or "*",
                 payload={"operation": operation, "reason": reason})
    except Exception as exc:  # refusal still holds if the ledger is unavailable
        log.error("secret denial event emit failed: %s", exc)
    raise HTTPException(status_code=403, detail="secret access denied")


def _operator_actor(request: Request, operation: str, ref: str | None = None,
                    *, allow_secret_agents: bool = True) -> str:
    """Return the proved operator/writer actor or record and raise 403.

    TCP loopback is not identity: an agent OS process reaches it too.  A TCP
    operator therefore needs the Authum/Traefik session proof.  On the unix
    socket SO_PEERCRED names the process.  Only root (or an explicitly declared
    operator account) and the owner-designated secret agents may mutate the
    store.  A Remote-User header never upgrades either path.
    """
    if identity_strength(request) == "session":
        return _secret_actor(request)

    if peer_credentials(request) is not None:
        slug = peer_identity(request)
        # Writers are named by capability, not slug. The slug list
        # "sysadmin,foreman" stopped matching anyone when those agents were renamed
        # agents to max and john, and nothing reported it. The foreman's
        # capability is `configurator`.
        writers = _csv_env("SN_SECRETS_WRITER_CAPABILITIES", "sysadmin,configurator")
        if slug:
            cap = _agent_capability(slug) if allow_secret_agents else None
            if cap and cap.lower() in writers:
                return slug
            _deny(request, operation, ref, "agent is not an approved secrets writer")
        os_user = _peer_os_user(request)
        operators = _csv_env("SN_SECRETS_OPERATOR_USERS", "root")
        if os_user and os_user.lower() in operators:
            return f"os:{os_user}"
        _deny(request, operation, ref, "socket peer is not an operator")

    _deny(request, operation, ref, "TCP caller has no authenticated owner session")
    raise AssertionError("unreachable")


def _agent_secret_grant(slug: str) -> tuple[str, list[str]]:
    """Return (capability mode, exact/namespace refs) from grants.json."""
    import json as _j
    agents_root = os.environ.get("SN_AGENTS_ROOT", "/srv/agents")
    try:
        with open(f"{agents_root}/{slug}/grants.json") as gf:
            data = _j.load(gf) or {}
    except (FileNotFoundError, OSError, ValueError):
        return "", []
    mode = (data.get("grants") or {}).get("secrets", "")
    refs = data.get("secret_refs") or []
    if not isinstance(refs, list):
        refs = []
    return str(mode), [str(r) for r in refs]


def _secret_ref_allowed(ref: str, allowed: list[str]) -> bool:
    """Exact refs and dotted namespaces ending in .*; no implicit wildcard."""
    return secret_store.ref_allowed(ref, allowed)


def _store_error(e: Exception) -> HTTPException:
    log.error("secret store unavailable: %s", e)
    return HTTPException(status_code=503, detail="secrets-store unavailable: node secret key unreadable")


def _read_value(ref: str) -> str | None:
    """One value from the store, or None when absent. The one place a
    plaintext leaves the database on this route."""
    try:
        with cursor() as cur:
            return secret_store.get_value(cur, ref)
    except secret_store.StoreUnavailable as e:
        raise _store_error(e)


def _live_agent_grants() -> dict[str, tuple[str, list[str]]]:
    with cursor() as cur:
        cur.execute("SELECT slug FROM sn_config.agents WHERE archived_at IS NULL ORDER BY slug")
        slugs = [r["slug"] for r in cur.fetchall() or []]
    return {slug: _agent_secret_grant(slug) for slug in slugs}


# ---------------------------------------------------------------- routes

@router.get("/refs", response_model=dict, dependencies=[Depends(requires("secrets.refs.read"))])
def list_refs(request: Request):
    """Every ref in the store with what the panel needs to say about it: its
    namespace, where it is wired in, which agents may read it, who set it last
    and who read it last. NEVER returns values.

    Restricted to a proved socket writer or authenticated owner session.
    Loopback alone is not identity: agent OS processes share it.
    """
    _operator_actor(request, "list", allow_secret_agents=True)
    with cursor() as cur:
        items = secret_store.list_items(cur)
        refs = [i["ref"] for i in items]
        wired = secret_store.used_by(cur, refs)
    who = secret_store.readers(_live_agent_grants(), refs)
    for item in items:
        item["used_by"] = wired.get(item["ref"], [])
        item["readers"] = who.get(item["ref"], [])
    return {"refs": refs, "count": len(refs), "items": items}


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(requires("secrets.write"))])
def set_secret(payload: SecretSet, request: Request):
    """Create or replace a secret. Returns {ref} and never echoes the value.
    The previous value, if any, is kept as a version for thirty days.

    Restricted to a proved socket writer or authenticated owner session.
    """
    actor = _operator_actor(request, "set", payload.ref)
    try:
        with cursor(commit=True) as cur:
            replaced = secret_store.set_value(cur, payload.ref, payload.value, actor, note=payload.note)
            secret_store.prune_versions(cur)
            emit(cur, actor=actor, event_type="secret.set",
                 entity_type="secret", entity_id=payload.ref,
                 payload={"replaced": replaced})
    except secret_store.StoreUnavailable as e:
        raise _store_error(e)
    log.info("secret set: ref=%s actor=%s replaced=%s", payload.ref, actor, replaced)
    return {"ref": payload.ref, "actor": actor, "replaced": replaced}


@router.delete("/{ref:path}", response_model=dict,
               dependencies=[Depends(requires("secrets.write"))])
def delete_secret(ref: str, request: Request):
    """Delete a secret by ref; its last value stays as a version for thirty
    days. 404 if absent.

    Restricted to a proved writer: an agent on docker0 must not be able to
    destroy a secret; this was the mechanism of the 2026-09-02 incident.
    """
    _check_ref(ref)
    actor = _operator_actor(request, "delete", ref)
    try:
        with cursor(commit=True) as cur:
            if not secret_store.exists(cur, ref):
                raise HTTPException(status_code=404, detail="ref not found")
            secret_store.delete_value(cur, ref, actor)
            emit(cur, actor=actor, event_type="secret.deleted",
                 entity_type="secret", entity_id=ref, payload={})
    except secret_store.StoreUnavailable as e:
        raise _store_error(e)
    log.info("secret deleted: ref=%s actor=%s", ref, actor)
    return {"ref": ref, "deleted": True, "actor": actor}


@router.get("/resolve", response_model=dict, dependencies=[Depends(requires("secrets.read"))])
def resolve_secret(ref: str, request: Request):
    """Return one value to a proved operator or a scoped agent socket peer,
    and journal the read: who, which ref, how."""
    if not _trusted(request):
        _deny(request, "resolve", ref, "caller not on trusted network")

    slug = peer_identity(request)
    if slug:
        mode, allowed_refs = _agent_secret_grant(slug)
        if "r" not in mode:
            _deny(request, "resolve", ref, "agent holds no secrets read grant")
        if not _secret_ref_allowed(ref, allowed_refs):
            _deny(request, "resolve", ref, "ref is outside the agent secret grant")
        actor, how = slug, "agent"
    else:
        # A non-agent socket peer or an authenticated owner session.  Anonymous
        # loopback/docker traffic is refused: network location is not identity.
        actor = _operator_actor(request, "resolve", ref, allow_secret_agents=True)
        how = "operator"

    _check_ref(ref)
    value = _read_value(ref)
    if value is None:
        raise HTTPException(status_code=404, detail="ref not found")
    try:
        with cursor(commit=True) as cur:
            emit(cur, actor=actor, event_type="secret.read",
                 entity_type="secret", entity_id=ref, payload={"how": how})
    except Exception as exc:  # the read stands; the gap is in the operator's log
        log.error("secret read event emit failed: ref=%s actor=%s: %s", ref, actor, exc)
    return {"ref": ref, "value": value}


@router.get("/requests", response_model=dict, dependencies=[Depends(requires("secrets.refs.read"))])
def list_requests(request: Request):
    """Reads the store refused in the last thirty days, grouped by agent and
    ref, where the agent still lacks the ref: the asks a grant would answer.
    A request for a ref that does not exist is listed too, marked as such."""
    _operator_actor(request, "requests", allow_secret_agents=True)
    with cursor() as cur:
        cur.execute(
            "SELECT actor, entity_id AS ref, count(*) AS n, max(ts) AS last_at, "
            "       (array_agg(payload->>'reason' ORDER BY ts DESC))[1] AS reason "
            "FROM sn_core.events "
            "WHERE event_type = 'secret.access_denied' AND entity_type = 'secret' "
            "  AND payload->>'operation' = 'resolve' AND entity_id <> '*' "
            "  AND ts > now() - make_interval(days => %s) "
            "GROUP BY actor, entity_id ORDER BY max(ts) DESC",
            (REQUESTS_WINDOW_DAYS,))
        rows = cur.fetchall() or []
        present = set()
        if rows:
            cur.execute("SELECT ref FROM sn_config.secrets WHERE ref = ANY(%s)",
                        ([r["ref"] for r in rows],))
            present = {r["ref"] for r in cur.fetchall() or []}
    grants = _live_agent_grants()
    items: list[dict[str, Any]] = []
    for r in rows:
        slug = r["actor"]
        if slug not in grants:
            continue                       # not an agent: an operator's refusal is not a request
        mode, rules = grants[slug]
        if "r" in mode and secret_store.ref_allowed(r["ref"], rules):
            continue                       # already granted since
        items.append({"agent": slug, "ref": r["ref"], "count": int(r["n"]),
                      "last_at": r["last_at"].isoformat() if r["last_at"] else None,
                      "reason": r["reason"], "exists": r["ref"] in present})
    return {"requests": items, "count": len(items), "window_days": REQUESTS_WINDOW_DAYS}


@router.get("/snapshots/{ref}", response_model=dict,
            dependencies=[Depends(requires("secrets.snapshots.read"))])
def list_snapshots(ref: str, request: Request):
    """The earlier values of a ref: when each was replaced or deleted, by whom.
    Values are not listed; one is read by its timestamp."""
    _operator_actor(request, "snapshot.list", ref)
    _check_ref(ref)
    with cursor() as cur:
        versions = secret_store.list_versions(cur, ref)
    return {"ref": ref, "timestamps": [v["ts"] for v in versions], "versions": versions}


@router.get("/snapshots/{ref}/{ts}", response_model=dict,
            dependencies=[Depends(requires("secrets.snapshots.read"))])
def get_snapshot(ref: str, ts: int, request: Request):
    """The value a ref had before the replacement stamped `ts`."""
    actor = _operator_actor(request, "snapshot.get", ref)
    _check_ref(ref)
    try:
        with cursor(commit=True) as cur:
            val = secret_store.version_value(cur, ref, ts)
            if val is None:
                raise HTTPException(status_code=404, detail="snapshot not found")
            emit(cur, actor=actor, event_type="secret.read",
                 entity_type="secret", entity_id=ref, payload={"how": "version", "ts": ts})
    except secret_store.StoreUnavailable as e:
        raise _store_error(e)
    return {"ref": ref, "ts": ts, "value": val}
