"""Identity resolution for admin-runner.

core-api proves itself with the private SN_ADMIN_CLIENT_SECRET, then names the
original actor in X-SN-On-Behalf-Of. A trusted source address alone does not
prove an actor, and a name sent without that proof is refused here, before any
permission is looked at. The actor is resolved against sn_config.users.

Authorization is NOT in this module: every privileged operation declares its
permission in `operations.py:REGISTRY` and the central check in
`permissions.require_permission` decides on (role, permission). It resolves
the caller through this module, so the proof above is asked on every call.
"""
import hmac
import ipaddress
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request

import config
from db import cursor

# Docker network CIDR: set SN_DOCKER_CIDR in the deployment environment.
# If unset, Docker-network trust is disabled (fail closed); loopback and
# SN_TRUSTED_CIDRS still apply.
_docker_cidr = os.environ.get("SN_DOCKER_CIDR", "")
_DOCKER0 = ipaddress.ip_network(_docker_cidr, strict=False) if _docker_cidr else None


def _trusted_nets():
    nets = [_DOCKER0] if _DOCKER0 is not None else []
    for part in config.TRUSTED_CIDRS.split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return nets


def get_actor(request: Request, remote_user):
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    trusted = ip is not None and (ip.is_loopback or any(ip in n for n in _trusted_nets()))
    if not trusted:
        raise HTTPException(403, "admin-runner caller is outside trusted networks")
    secret = os.environ.get("SN_ADMIN_CLIENT_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "admin-runner client credential is not configured")
    offered = request.headers.get("x-sn-service-auth", "")
    if (request.headers.get("x-sn-service", "") != "core-api"
            or not offered or not hmac.compare_digest(offered, secret)):
        raise HTTPException(403, "admin-runner requires core-api service proof")
    on_behalf_of = request.headers.get("x-sn-on-behalf-of", "").strip()
    if not on_behalf_of:
        raise HTTPException(403, "admin-runner requires the original actor")
    return on_behalf_of


@dataclass
class Principal:
    username: str
    user_id: int
    tenant_id: int
    role: str


def get_principal(request: Request, remote_user):
    username = get_actor(request, remote_user)
    with cursor() as cur:
        cur.execute(
            "SELECT id, tenant_id, role FROM sn_config.users "
            "WHERE email = %s AND active = true",
            (username,),
        )
        row = cur.fetchone()
    if row is None or row["tenant_id"] is None:
        raise HTTPException(403, "no active owner identity for " + str(username))
    return Principal(username, row["id"], row["tenant_id"], row["role"] or "")
