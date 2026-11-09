"""Test-node lifecycle proxy.

core-api is unprivileged; LXD container operations run in the privileged
admin-runner service (loopback only). These three endpoints proxy the three
admin-runner ops so agents can call them through core-api.

  POST   /api/test-nodes/provision   -- create + install a test container
  POST   /api/test-nodes/destroy     -- stop + delete (two-call confirm gate)
  GET    /api/test-nodes/{name}/status -- lxc info + health probe

Caller must have a cross-tenant role (super-admin or operator). The admin-runner
enforces owner identity independently via Remote-User.

Credential isolation and idempotency guards live in admin-runner/operations.py,
not here. This layer only:
  - validates the inputs before forwarding (fast-fail, no side-effects)
  - forwards Remote-User to admin-runner (which re-enforces the owner gate)
  - passes confirm_token through for the node-destroy two-call flow
"""
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from auth import Principal, get_principal
from permissions import requires
from lib.admin_runner import runner_op

router = APIRouter(prefix="/api/test-nodes", tags=["test-nodes"])

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_VERSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,60}$")


def _require_cross_tenant(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.cross_tenant:
        raise HTTPException(403, "test-node operations require a cross-tenant (admin) role")
    return principal


def _valid_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(422, f"invalid container name {name!r} "
                            "(lowercase a-z, digits, hyphens; 2-41 chars; must start with a letter)")
    return name


class ProvisionRequest(BaseModel):
    name: str
    installer_version: str
    seed_manifest: dict | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v.strip()):
            raise ValueError(
                "container name must be lowercase a-z/digits/hyphens, 2-41 chars, "
                "start with a letter"
            )
        return v.strip()

    @field_validator("installer_version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        if not _VERSION_RE.match(v.strip()):
            raise ValueError("invalid installer_version")
        return v.strip()


class DestroyRequest(BaseModel):
    name: str
    confirm_token: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v.strip()):
            raise ValueError(
                "container name must be lowercase a-z/digits/hyphens, 2-41 chars, "
                "start with a letter"
            )
        return v.strip()


@router.post("/provision", dependencies=[Depends(requires("test_nodes.provision"))])
def provision_node(body: ProvisionRequest,
                   principal: Principal = Depends(_require_cross_tenant)):
    """Create a test LXC container and run the Smart Node installer inside it.

    Idempotent: if the container already exists and is running, reports its
    state and returns without re-provisioning. Credential isolation is enforced
    by admin-runner before lxc launch: any seed_manifest value that matches a
    live node secret is refused with exit code 5. Omit seed_manifest or pass
    null for disconnected mode (no live credentials, safe default).
    """
    result = runner_op(
        remote_user=principal.username,
        op="lxc_node_provision",
        args={
            "name": body.name,
            "installer_version": body.installer_version,
            **({"seed_manifest": body.seed_manifest} if body.seed_manifest is not None else {}),
        },
        timeout=660,  # provision may take 10+ minutes for a full install
    )
    return result


@router.post("/destroy", dependencies=[Depends(requires("test_nodes.destroy"))])
def destroy_node(body: DestroyRequest,
                 principal: Principal = Depends(_require_cross_tenant)):
    """Stop and delete a test LXC container (two-call confirmation gate).

    First call (no confirm_token): admin-runner returns
      {needs_confirm: true, confirm_token: "...", message: "..."}
    Re-submit with the returned confirm_token to execute the deletion.

    Hard guards that run before any lxc command:
      - Refused if the container name matches the live node hostname.
      - Refused if the container is not in the test-node registry.
    """
    result = runner_op(
        remote_user=principal.username,
        op="lxc_node_destroy",
        args={"name": body.name},
        confirm_token=body.confirm_token or None,
        timeout=120,
    )
    return result


class StatusRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v.strip()):
            raise ValueError(
                "container name must be lowercase a-z/digits/hyphens, 2-41 chars, "
                "start with a letter"
            )
        return v.strip()


@router.post("/status", dependencies=[Depends(requires("test_nodes.read"))])
def node_status(body: StatusRequest, principal: Principal = Depends(_require_cross_tenant)):
    """Read-only: lxc info state for a test container, plus its /health endpoint
    response if the container is running. Returns {found: false} if the
    container does not exist. No confirmation required.
    """
    result = runner_op(
        remote_user=principal.username,
        op="lxc_node_status",
        args={"name": body.name},
        timeout=30,
    )
    return result
