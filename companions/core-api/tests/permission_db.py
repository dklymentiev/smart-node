"""A throwaway Postgres for the per-route permission tests.

These tests measure the real chain -- identity, role row, role x permission
matrix, object grant -- so they need the schema and the seed the installer
bakes, not a stubbed decision. The database is built from the tree's own
bootstrap files in a temporary directory, reached over a unix socket in that
directory, and removed when the test process exits. Nothing outside that
directory is read or written.

Run with `--noconftest`: the package conftest opens a pool against whatever
database the environment points at, and these tests must only ever see their
own.
"""

from __future__ import annotations

import asyncio
import atexit
import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_BOOTSTRAP = (
    "bootstrap/B02-schema.sql",
    "bootstrap/B02-sn-core-schema.sql",
    "bootstrap/B02-sn-core-seed.sql",
)
_state: dict[str, str] = {}

# The proof the reverse proxy stamps on a request it authenticated. A person
# reaches core-api through the proxy and by no other road: a name typed in a
# header over TCP is refused for anything that changes state.
PROXY_SECRET = "p" * 64


def session(who: str) -> dict:
    """Headers of a request the proxy authenticated for `who`."""
    return {"Remote-User": who, "X-SN-Proxy-Auth": PROXY_SECRET}


# The three callers every test file uses. OWNER is the seed's own account.
OWNER = "admin"
ADMIN = "perm-admin"
PLAIN = "perm-plain"


def _pg_bin() -> str | None:
    explicit = os.environ.get("PG_BIN")
    candidates = [explicit] if explicit else sorted(
        glob.glob("/usr/lib/postgresql/*/bin"),
        key=lambda p: int(p.split("/")[-2]) if p.split("/")[-2].isdigit() else 0,
        reverse=True,
    )
    for candidate in candidates:
        if os.access(os.path.join(candidate, "initdb"), os.X_OK):
            return candidate
    return None


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed: {(proc.stderr or proc.stdout)[-600:]}")


def _stop() -> None:
    work = _state.get("work")
    if not work:
        return
    import db
    db.close_pool()
    subprocess.run([os.path.join(_state["bin"], "pg_ctl"), "-D", work + "/data",
                    "stop", "-m", "fast"], capture_output=True)
    shutil.rmtree(work, ignore_errors=True)
    _state.clear()


def _start() -> str:
    bin_dir = _pg_bin()
    if bin_dir is None:
        pytest.skip("no postgres server binaries here (set PG_BIN): the "
                    "permission tests build their own database and will not "
                    "borrow another one")
    work = tempfile.mkdtemp(prefix="sn-perm-")
    _state.update(work=work, bin=bin_dir)
    atexit.register(_stop)
    _run([bin_dir + "/initdb", "-D", work + "/data", "-A", "trust", "-U", "postgres"], "initdb")
    _run([bin_dir + "/pg_ctl", "-D", work + "/data", "-o", f"-k {work} -c listen_addresses=",
          "-l", work + "/pg.log", "-w", "start"], "postgres start")
    psql = [bin_dir + "/psql", "-h", work, "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"]
    _run(psql + ["-c", "CREATE ROLE smartnode LOGIN SUPERUSER"], "create role")
    _run(psql + ["-c", "CREATE DATABASE smartnode OWNER smartnode"], "create database")
    for rel in _BOOTSTRAP:
        _run(psql + ["-d", "smartnode", "-f", str(_ROOT / rel)], rel)
    return work


@pytest.fixture(scope="session")
def permission_db():
    """The seeded throwaway database, with the pool pointed at it.

    Yields `db.cursor`. Adds two accounts next to the seed's owner: an admin
    (a real actor row and a real role row) and a plain account with an actor
    row and NO role row, which is how the role `user` is spelled.
    """
    work = _state.get("work") or _start()
    import db

    saved = os.environ.get("PGHOST")
    saved_proxy = os.environ.get("SN_PROXY_AUTH_SECRET")
    os.environ["PGHOST"] = work
    os.environ["SN_PROXY_AUTH_SECRET"] = PROXY_SECRET
    db.close_pool()
    db.init_pool()
    with db.cursor(commit=True) as cur:
        # Fail closed: every statement below writes, and must only ever land
        # in the instance this module started.
        cur.execute("SHOW unix_socket_directories")
        where = cur.fetchone()["unix_socket_directories"]
        if where != work:
            db.close_pool()
            raise RuntimeError(f"pool is not on the throwaway database ({where})")
        for name, role in ((ADMIN, "admin"), (PLAIN, None)):
            cur.execute(
                "INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) "
                "VALUES (%s, %s, 'user', 'human', 'human', 'internal') ON CONFLICT (id) DO NOTHING",
                (f"user:{name}", name))
            cur.execute(
                "INSERT INTO sn_config.users (tenant_id, email, role, active, actor_id) "
                "VALUES (1, %s, 'user', true, %s) ON CONFLICT (tenant_id, email) DO NOTHING",
                (name, f"user:{name}"))
            if role:
                cur.execute(
                    "INSERT INTO sn_core.actor_roles (actor_id, role, source) "
                    "VALUES (%s, %s, 'owner_grant') ON CONFLICT (actor_id) DO NOTHING",
                    (f"user:{name}", role))
    try:
        yield db.cursor
    finally:
        db.close_pool()
        if saved_proxy is None:
            os.environ.pop("SN_PROXY_AUTH_SECRET", None)
        else:
            os.environ["SN_PROXY_AUTH_SECRET"] = saved_proxy
        if saved is None:
            os.environ.pop("PGHOST", None)
        else:
            os.environ["PGHOST"] = saved


def call(app, method: str, path: str, who: str, *, proved: bool = True, **kwargs):
    """One request as `who`, arriving the way the panel's proxy delivers it.

    The stock TestClient reports a peer that is not an address, which the
    identity layer rightly treats as untrusted. This sends from loopback with
    the forwarded-identity header and the proxy's proof, so the real
    `get_principal` resolves the account as a signed-in session and nothing in
    the identity or permission path is replaced.

    `proved=False` sends the name alone, as anything on the trusted network
    could: what a test uses to show that the name alone is not enough.
    """
    import httpx

    async def _go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as client:
            return await client.request(method, path, headers=session(who) if proved else {"Remote-User": who}, **kwargs)

    return asyncio.run(_go())


def declared(router) -> dict[tuple[str, str], tuple[str, bool] | None]:
    """(method, path) -> (permission, passes an object) for every route."""
    from permissions import route_permission

    out: dict[tuple[str, str], tuple[str, bool] | None] = {}
    for route in router.routes:
        for method in route.methods:
            if method != "HEAD":
                out[(method, route.path)] = route_permission(route)
    return out
