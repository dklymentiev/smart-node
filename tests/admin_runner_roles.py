"""Driver for admin-runner-roles.test.sh: admin-runner's door on a real catalogue.

Run with PYTHONPATH on companions/admin-runner and the PG* variables on the
throwaway database. Nothing here executes an operation: the questions are who
is let through the door and who holds an operation's permission.
"""
import os
import sys

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

SECRET = "runner-proof"
os.environ["SN_ADMIN_CLIENT_SECRET"] = SECRET

import app as runner_app  # noqa: E402
import permissions  # noqa: E402
from db import cursor  # noqa: E402
from operations import REGISTRY  # noqa: E402

fails = 0


def res(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else "  " + str(detail)[:200]))
    fails += 0 if ok else 1


def proof(actor, secret=SECRET):
    return {"X-SN-Service": "core-api", "X-SN-Service-Auth": secret, "X-SN-On-Behalf-Of": actor}


def denials(reason):
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM sn_core.events WHERE event_type = "
                    "'admin.permission_denied' AND payload->>'reason' = %s", (reason,))
        return cur.fetchone()["n"]


OWNER, CONFIGURATOR, PLAIN = sys.argv[1:4]

# ---- boot: the registry against the seeded catalogue -------------------------
with TestClient(runner_app.app) as _boot:
    res("boot: admin-runner starts on the seeded catalogue (registry and catalogue agree)", True)
with cursor() as cur:
    cur.execute("SELECT name FROM sn_config.permissions WHERE name LIKE 'admin.%%'")
    seeded = {r["name"] for r in cur.fetchall()}
declared = {op.permission for op in REGISTRY.values()}
res("boot: every operation's permission is a catalogue row", declared <= seeded,
    sorted(declared - seeded))
res("boot: the one catalogue row with no operation is admin.lifecycle.reopen",
    seeded - declared == {"admin.lifecycle.reopen"}, sorted(seeded - declared))

# ---- the door: the same dependency the operations use, on a route that runs
# nothing. TestClient reports a peer that is no address, so the loopback rule
# is given the address a real caller on the node has.
door = FastAPI()


@door.post("/ensure", dependencies=[Depends(permissions.require_permission("admin.ensure_agent_users"))])
def _ensure():
    return {"ok": True}


@door.post("/command", dependencies=[Depends(permissions.require_permission("admin.run_command"))])
def _command():
    return {"ok": True}


import asyncio  # noqa: E402

import httpx  # noqa: E402


def call(path, headers, client=("127.0.0.1", 50000)):
    async def _go():
        transport = httpx.ASGITransport(app=door, client=client, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            return await c.post(path, headers=headers)
    return asyncio.run(_go())


before = denials("identity-or-proof")
r = call("/ensure", {"Remote-User": OWNER})
res("guard: the owner's name alone over loopback is refused", r.status_code == 403, (r.status_code, r.text))
r = call("/ensure", proof(OWNER, "not-the-proof"))
res("guard: a wrong core-api proof is refused", r.status_code == 403, (r.status_code, r.text))
r = call("/ensure", {"X-SN-Service": "core-api", "X-SN-Service-Auth": SECRET})
res("guard: the proof with nobody named is refused", r.status_code == 403, (r.status_code, r.text))
r = call("/ensure", proof(OWNER), client=("203.0.113.9", 50000))
res("guard: the proof from outside the trusted networks is refused", r.status_code == 403,
    (r.status_code, r.text))
res("guard: each refusal at the door is recorded", denials("identity-or-proof") == before + 4,
    denials("identity-or-proof") - before)

# ---- the two chains that create an agent --------------------------------------
r = call("/ensure", proof(OWNER))
res("install: the owner, named by core-api for the root installer, may ensure agent users",
    r.status_code == 200, (r.status_code, r.text))
r = call("/ensure", proof(CONFIGURATOR))
res("configurator: an admin-role agent, named by core-api, may ensure agent users",
    r.status_code == 200, (r.status_code, r.text))
r = call("/ensure", proof(PLAIN))
res("role: an agent with no role is refused, and the role and permission are named",
    r.status_code == 403 and "'user'" in r.text and "'admin.ensure_agent_users'" in r.text,
    (r.status_code, r.text))
r = call("/command", proof(CONFIGURATOR))
res("role: the configurator does not hold the shell", r.status_code == 403
    and "'admin'" in r.text and "'admin.run_command'" in r.text, (r.status_code, r.text))
r = call("/command", proof(OWNER))
res("role: the owner holds the shell", r.status_code == 200, (r.status_code, r.text))
r = call("/ensure", proof("nobody-here"))
res("role: a name core-api proves but no account answers to is refused", r.status_code == 403,
    (r.status_code, r.text))

print("PASS" if not fails else "FAIL (%d)" % fails)
sys.exit(1 if fails else 0)
