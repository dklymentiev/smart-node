"""Provision path: author sn_config.tenant_profile rows and
materialize them through the projector.

The profile is the single source of truth. sysadmin authors config here and
reprojects instead of mutating agents / integrations / policies directly --
raw writes produce drift the projector's reconcile/teardown pass cannot see.
This is true for onboarding AND for every later correction ("add agent X",
"retire integration Y"): edit the profile, reproject.

Gated to loopback / docker0 callers or a cross-tenant (admin) principal --
writing the profile and running the projector is a node-level privileged op.
"""

import ipaddress
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2.extras import Json
from pydantic import BaseModel, Field

from lib import projector
from auth import get_principal
from db import cursor
from permissions import requires
from events_emit import emit

log = logging.getLogger("core-api.provision")

router = APIRouter()

_docker_cidr = os.environ.get("SN_DOCKER_CIDR", "")
DOCKER0_NET = ipaddress.ip_network(_docker_cidr, strict=False) if _docker_cidr else None


def _trusted_nets():
    """Networks whose callers are trusted for provision: Docker network + whatever
    the node marks trusted via SN_TRUSTED_CIDRS. On a node, core-api binds the
    Docker bridge gateway and an on-node caller (the sysadmin agent) is seen with
    the node's own IP -- which is in SN_TRUSTED_CIDRS, not in the Docker network.
    Honor it so the on-node configurator can author the profile (loopback alone is
    not enough because core-api is not bound on 127.0.0.1)."""
    nets = [DOCKER0_NET] if DOCKER0_NET is not None else []
    for c in os.environ.get("SN_TRUSTED_CIDRS", "").split(","):
        c = c.strip()
        if not c:
            continue
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            pass
    return nets


_TRUSTED_NETS = _trusted_nets()

# Mirror the tenant_profile CHECK constraints so bad input fails at 422 with a
# clear message; the DB CHECKs remain the authoritative backstop.
_VALUE_KINDS = {"literal", "pointer", "secret-ref"}
_BEHAVIORS = {"config-sync", "stateful-merge", "seed-once", "policy-versioned", "secret-write-once"}
_SOURCES = {"vertical-pack-default", "interview", "manual"}
_STATUSES = {"pending", "confirmed", "projected", "failed", "diverged", "retired"}

_PROFILE_COLS = ("param_key", "value", "value_kind", "behavior", "target",
                 "natural_key", "source", "status", "version")


def _guard(request: Request) -> None:
    """Allow loopback / docker0, or a cross-tenant (admin) principal; else 403."""
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or any(ip in n for n in _TRUSTED_NETS):
            return
    except ValueError:
        pass
    try:
        principal = get_principal(request)
    except Exception:
        principal = None
    if principal is not None and principal.cross_tenant:
        return
    raise HTTPException(403, "provision requires a loopback / trusted-node caller or an admin principal")


class ParamIn(BaseModel):
    param_key: str = Field(min_length=1, max_length=200)
    value: object = None            # jsonb payload (behaviour-specific); never a raw secret
    value_kind: str = "literal"
    behavior: str
    target: str | None = None
    natural_key: str | None = None
    source: str = "manual"
    status: str = "confirmed"


class ParamsIn(BaseModel):
    params: list[ParamIn] = Field(min_length=1)


def _validate(p: ParamIn) -> None:
    if p.value_kind not in _VALUE_KINDS:
        raise HTTPException(422, f"bad value_kind {p.value_kind!r}")
    if p.behavior not in _BEHAVIORS:
        raise HTTPException(422, f"bad behavior {p.behavior!r}")
    if p.source not in _SOURCES:
        raise HTTPException(422, f"bad source {p.source!r}")
    if p.status not in _STATUSES:
        raise HTTPException(422, f"bad status {p.status!r}")
    # M-a: secrets and ONLY secrets are write-once (tenant_profile_secret_iff_write_once)
    if (p.value_kind == "secret-ref") != (p.behavior == "secret-write-once"):
        raise HTTPException(422, "value_kind 'secret-ref' and behavior 'secret-write-once' must coincide")
    if p.behavior in ("seed-once", "policy-versioned") and not p.natural_key:
        raise HTTPException(422, f"behavior {p.behavior!r} requires natural_key")


@router.get("/params", dependencies=[Depends(requires("provision.read"))])
def list_params(request: Request, include_retired: bool = False):
    """Current profile rows (source of truth). Values are returned as-is; a
    secret-ref is only ever a pointer, never a plaintext secret."""
    _guard(request)
    where = "" if include_retired else "WHERE status <> 'retired'"
    with cursor() as cur:
        cur.execute(f"SELECT {', '.join(_PROFILE_COLS)} FROM sn_config.tenant_profile "
                    f"{where} ORDER BY param_key")
        rows = cur.fetchall()
    return {"params": rows, "count": len(rows)}


@router.put("/params", dependencies=[Depends(requires("provision.write"))])
def author_params(body: ParamsIn, request: Request):
    """Upsert a batch of profile params. Re-authoring a key bumps its version.
    Does NOT project -- call POST /project to materialize."""
    _guard(request)
    out = []
    with cursor(commit=True) as cur:
        for p in body.params:
            _validate(p)
            cur.execute(
                """
                INSERT INTO sn_config.tenant_profile
                    (param_key, value, value_kind, behavior, target, natural_key,
                     source, status, version, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, now(), now())
                ON CONFLICT (param_key) DO UPDATE SET
                    value        = EXCLUDED.value,
                    value_kind   = EXCLUDED.value_kind,
                    behavior     = EXCLUDED.behavior,
                    target       = EXCLUDED.target,
                    natural_key  = EXCLUDED.natural_key,
                    source       = EXCLUDED.source,
                    status       = EXCLUDED.status,
                    version      = sn_config.tenant_profile.version + 1,
                    updated_at   = now()
                RETURNING param_key, behavior, status, version
                """,
                (p.param_key, Json(p.value), p.value_kind, p.behavior, p.target,
                 p.natural_key, p.source, p.status),
            )
            r = cur.fetchone()
            emit(cur, actor="provision", event_type="param.authored",
                 entity_type="tenant_profile", entity_id=p.param_key,
                 payload={"behavior": r["behavior"], "version": r["version"],
                          "status": r["status"]})
            out.append(r)
    return {"authored": out, "count": len(out)}


@router.post("/params/{param_key}/retire", dependencies=[Depends(requires("provision.write"))])
def retire_param(param_key: str, request: Request):
    """Mark a param retired so the next projection tears its target down."""
    _guard(request)
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sn_config.tenant_profile SET status = 'retired', updated_at = now() "
            "WHERE param_key = %s AND status <> 'retired' RETURNING param_key",
            (param_key,),
        )
        if cur.fetchone() is None:
            raise HTTPException(404, f"param '{param_key}' not found or already retired")
        emit(cur, actor="provision", event_type="param.retire-requested",
             entity_type="tenant_profile", entity_id=param_key, payload={})
    return {"param_key": param_key, "status": "retired"}


@router.post("/project", dependencies=[Depends(requires("provision.write"))])
def run_projection(request: Request):
    """Materialize the current profile: forward tiers T0..T7 then reverse pass.
    Idempotent and re-runnable. Returns the per-result summary."""
    _guard(request)
    return projector.project(actor="provision")
