"""Thin client for the privileged admin-runner over loopback.

core-api is unprivileged; anything that must touch root-owned node state
(e.g. /etc/cron.d) is delegated to admin-runner. The endpoint is resolved from
the integrations catalog (slug=admin-runner), never hardcoded. The caller's
identity is forwarded with a private core-api service proof; admin-runner
re-enforces the owner gate against that identity.
"""
import json
import os
import urllib.error
import urllib.request

from fastapi import HTTPException

from db import cursor


def runner_auth_headers(remote_user: str) -> dict[str, str]:
    """Prove core-api to admin-runner while naming the original caller."""
    secret = os.environ.get("SN_ADMIN_CLIENT_SECRET", "").strip()
    if not secret:
        raise HTTPException(503, "admin-runner client credential is not configured")
    if not remote_user:
        raise HTTPException(403, "admin-runner call has no original actor")
    return {
        "Content-Type": "application/json",
        "X-SN-Service": "core-api",
        "X-SN-Service-Auth": secret,
        "X-SN-On-Behalf-Of": remote_user,
    }


def resolve_endpoint() -> str:
    with cursor() as cur:
        cur.execute(
            "SELECT endpoint FROM sn_config.integrations "
            "WHERE slug = %s AND archived_at IS NULL",
            ("admin-runner",),
        )
        row = cur.fetchone()
    if not row or not row["endpoint"]:
        raise HTTPException(status_code=502, detail="admin-runner integration has no endpoint configured")
    return row["endpoint"].rstrip("/")


def runner_op(remote_user: str, op: str, args: dict, timeout: int = 120,
              confirm_token: str | None = None) -> dict:
    """POST a privileged op to admin-runner, forwarding the owner identity.

    confirm_token is for the two-call destructive-op flow: the first call
    returns {needs_confirm, confirm_token}; the caller re-submits with the
    token to execute. Pass confirm_token=None (default) for the first call.
    """
    endpoint = resolve_endpoint()
    payload: dict = {"args": args or {}}
    if confirm_token is not None:
        payload["confirm_token"] = confirm_token
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/operations/{op}",
        data=body,
        headers=runner_auth_headers(remote_user),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=502, detail=f"admin-runner {op} HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"admin-runner unreachable: {e.reason}")
