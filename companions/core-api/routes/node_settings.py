"""The node's own settings, read by the panel's Settings page.

  GET  /api/node/about     its name, domain and release, and whether it is paused
  GET  /api/node/access    who can sign in and with what role; each role's holders
                           and permissions
  PUT  /api/node/digest    the daily digest e-mail, on or off

The owner asked for a Settings page with the node's global settings only
(2026-10-15). Agent prompts, limits and workflows have their own pages; this
is what belongs to the node as a whole. Only the digest switch writes.

Who can sign in is the auth provider's account file (Authum's users.json):
only the account names and roles are read from it, never anything else. The
role a person acts with is core-api's, from sn_core.actor_roles on the actor
the account maps to (user:<login>, or the actor of that id). An actor merged
into another (canonical_id) is listed under it as another name for it.
access.read is the owner's alone (super-admin): it lists every role's holders.
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from db import cursor
from permissions import requires

router = APIRouter(prefix="/api/node", tags=["node"])

RELEASE_FILE = os.environ.get("SN_RELEASE_FILE", "/etc/smart-node/release")
AUTH_USERS_FILE = os.environ.get("SMART_NODE_AUTH_USERS_FILE", "/var/lib/smart-node/authum/auth/users.json")


def _release() -> dict:
    out = {}
    try:
        with open(RELEASE_FILE, encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k in ("release", "commit", "installed_at"):
                    out[k] = v.strip()
    except OSError:
        pass
    return out


def _logins() -> list[dict]:
    """Account names and roles from the auth provider's file, nothing else."""
    try:
        with open(AUTH_USERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [{"login": k, "auth_role": (v.get("role") if isinstance(v, dict) else None),
             "created": (v.get("created") if isinstance(v, dict) else None)}
            for k, v in sorted(data.items())]


@router.get("/about", dependencies=[Depends(requires("system.status.read"))])
def about():
    with cursor() as cur:
        cur.execute("SELECT name FROM sn_config.tenants WHERE slug = 'default'")
        t = cur.fetchone()
        cur.execute("SELECT node_paused, changed_at, daily_digest FROM sn_config.node_control WHERE id = true")
        c = cur.fetchone()
    r = _release()
    return {
        "name": t["name"] if t else None,
        "domain": os.environ.get("SMART_NODE_DOMAIN") or None,
        "release": r.get("release"),
        "commit": r.get("commit"),
        "installed_at": r.get("installed_at"),
        "paused": bool(c and c["node_paused"]),
        "paused_changed_at": c["changed_at"] if c else None,
        # the digest is an e-mail: offered only where a sender has been proved
        "mail": bool(os.environ.get("SN_NOTIFY_SENDER")),
        "daily_digest": bool(c and c["daily_digest"]),
    }


class DigestInput(BaseModel):
    on: bool


@router.put("/digest", dependencies=[Depends(requires("limits.write"))])
def set_digest(body: DigestInput):
    with cursor(commit=True) as cur:
        cur.execute("UPDATE sn_config.node_control SET daily_digest = %s WHERE id = true", (body.on,))
    return {"daily_digest": body.on}


@router.get("/access", dependencies=[Depends(requires("access.read"))])
def access():
    with cursor() as cur:
        cur.execute(
            "SELECT a.id, a.display_name, a.kind, a.canonical_id, r.role "
            "FROM sn_core.actors a LEFT JOIN sn_core.actor_roles r ON r.actor_id = a.id "
            "WHERE a.active"
        )
        actors = {x["id"]: dict(x) for x in cur.fetchall()}
        cur.execute(
            "SELECT rp.role, rp.permission, p.description FROM sn_config.role_permissions rp "
            "LEFT JOIN sn_config.permissions p ON p.name = rp.permission ORDER BY rp.role, rp.permission"
        )
        perms = cur.fetchall()
    aliases: dict[str, list[str]] = {}
    for a in actors.values():
        if a["canonical_id"]:
            aliases.setdefault(a["canonical_id"], []).append(a["id"])

    def actor_of(login: str):
        for cand in ("user:" + login, login):
            a = actors.get(cand)
            while a and a["canonical_id"] and a["canonical_id"] in actors:
                a = actors[a["canonical_id"]]
            if a:
                return a
        return None

    logins = []
    for x in _logins():
        a = actor_of(x["login"])
        logins.append({**x, "actor": a["id"] if a else None,
                       "role": (a["role"] if a else None) or "user",
                       "other_names": sorted(aliases.get(a["id"], [])) if a else []})

    roles: dict[str, dict] = {}
    for p in perms:
        roles.setdefault(p["role"], {"role": p["role"], "holders": [], "permissions": []})["permissions"].append(
            {"name": p["permission"], "description": p["description"] or ""})
    for a in actors.values():
        if a["role"] and not a["canonical_id"]:
            roles.setdefault(a["role"], {"role": a["role"], "holders": [], "permissions": []})["holders"].append(
                {"id": a["id"], "name": a["display_name"], "kind": a["kind"]})
    order = {"super-admin": 0, "admin": 1, "user": 2, "installer": 3}
    out = sorted(roles.values(), key=lambda r: (order.get(r["role"], 9), r["role"]))
    for r in out:
        r["holders"].sort(key=lambda h: h["id"])
    return {"logins": logins, "roles": out,
            "note": "A person or agent with no role acts as user."}
