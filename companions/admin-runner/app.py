"""Smart Node admin-runner -- the privileged operations service.

Trust anchor for the node-admin agent. Bound host-local; every privileged op is
permission-gated per op, audited, and (when dangerous) confirm-gated. The
model never holds privilege -- it calls this service, which enforces the
boundary and does the work.

Every operation declares its permission in `operations.py:REGISTRY`. The
central check lives in `permissions.require_permission`: it resolves the
Principal, reads the effective role from `sn_core.actor_roles`, and admits
only when `sn_config.role_permissions` has the (role, permission) pair. The
old `require_owner`/OWNER_ROLES path is gone: the role matrix decides.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Header
from pydantic import BaseModel

from audit import audit
from auth import Principal, get_actor, get_principal
from confirm import store
from db import cursor, close_pool, init_pool
from operations import REGISTRY, validate_registry
from permissions import require_permission, validate_catalogue
from version import __version__

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("admin-runner")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    # Refuse to boot on a drift between the shipped REGISTRY and the DB
    # catalogue: an op whose permission is missing from `sn_config.permissions`
    # (or has an object axis admin-runner cannot enforce) is a deployment
    # failure that must be visible at startup, not at the first denied call.
    validate_registry()
    validate_catalogue([op.permission for op in REGISTRY.values()])
    log.info(
        "admin-runner %s ready (operations=%d, permissions=%d)",
        __version__,
        len(REGISTRY),
        len({op.permission for op in REGISTRY.values()}),
    )
    yield
    close_pool()


app = FastAPI(title="Smart Node admin-runner", version=__version__, lifespan=lifespan)


class OpRequest(BaseModel):
    args: dict = {}
    confirm_token: str = None


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.get(
    "/operations",
    dependencies=[Depends(require_permission("admin.node_status"))],
)
def list_operations():
    """List what the admin-runner can do. Gated on `admin.node_status` --
    revealing the op catalogue is at the same read level as reading node
    state; roles that cannot see one cannot see the other."""
    return {"operations": [
        {"name": o.name, "summary": o.summary, "dangerous": o.dangerous,
         "input_schema": o.input_schema, "permission": o.permission}
        for o in REGISTRY.values()
    ]}


@app.post("/operations/{name}")
def run_operation(
    name: str,
    req: OpRequest,
    request: Request,
    remote_user: str = Header(default=None),
):
    """Execute one operation. Permission is enforced per-op from the registry:
    the handler must hold `op.permission` in `sn_config.role_permissions`,
    checked by `permissions.check_permission`. Dangerous ops still go through
    the confirm-gate as before."""
    op = REGISTRY.get(name)
    if op is None:
        # 404 before authn so an unknown op does not leak the role matrix.
        raise HTTPException(404, "unknown operation " + name)

    # Per-op permission check -- one chokepoint, named by op.permission.
    # require_permission audits every decision through audit.audit().
    principal: Principal = require_permission(op.permission)(request, remote_user)

    actor = "user:" + principal.username
    args = req.args

    is_dangerous = op.is_dangerous(args)
    if is_dangerous:
        if not req.confirm_token:
            token = store.stage(name, args, principal.username)
            with cursor(commit=True) as cur:
                audit(cur, actor=actor, event_type="admin.op.requested",
                      entity_type="operation", entity_id=name,
                      payload={"args": args, "dangerous": True})
            return {"needs_confirm": True, "confirm_token": token, "summary": op.summary,
                    "message": name + " is destructive; re-submit with confirm_token to execute."}
        staged = store.consume(req.confirm_token, name, principal.username)
        if staged is None:
            with cursor(commit=True) as cur:
                audit(cur, actor=actor, event_type="admin.op.confirm_invalid",
                      entity_type="operation", entity_id=name, payload={})
            raise HTTPException(409, "confirm_token invalid, expired, or mismatched")
        args = staged

    try:
        result = op.handler(args)
    except Exception as e:
        with cursor(commit=True) as cur:
            audit(cur, actor=actor, event_type="admin.op.failed",
                  entity_type="operation", entity_id=name, payload={"error": str(e)})
        raise HTTPException(500, name + " failed: " + str(e))

    with cursor(commit=True) as cur:
        audit(cur, actor=actor, event_type="admin.op.executed",
              entity_type="operation", entity_id=name, payload={"args": args})
    return {"ok": True, "result": result}
