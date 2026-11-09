"""A throwaway Postgres carrying the tree's own schema and seed.

The per-route-file permission tests ask the real chokepoint, so the role
matrix, the actor roles and the object grants they read have to be the ones
the bootstrap files produce, not a stub that answers what the test expects.
The server lives in a temp directory, listens on a unix socket only, and is
removed afterwards; nothing outside that directory is touched.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = (
    "bootstrap/B02-schema.sql",
    "bootstrap/B02-sn-core-schema.sql",
    "bootstrap/B02-sn-core-seed.sql",
)
_PG_ENV = ("PGHOST", "PGDATABASE", "PGUSER", "PGPORT")


def _pg_bin() -> str:
    found = os.environ.get("PG_BIN") or next(
        iter(sorted(glob.glob("/usr/lib/postgresql/*/bin"),
                    key=lambda p: int(p.split("/")[-2]), reverse=True)), "")
    if not found or not os.access(os.path.join(found, "initdb"), os.X_OK):
        # Refuse rather than skip: a skipped permission test reads as a pass.
        pytest.fail("cannot run: no initdb (set PG_BIN)")
    return found


def _run(cmd: list[str], what: str) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        pytest.fail(f"cannot run: {what}: {done.stderr.strip()[-400:]}")
    return done.stdout


@contextmanager
def disposable_database():
    """Yield `sql(text) -> str` bound to a fresh database; point db.cursor at it."""
    import db

    for name in BOOTSTRAP:
        if not (ROOT / name).is_file():
            pytest.fail(f"cannot run: {name} is not in this tree")
    bin_dir = _pg_bin()
    work = tempfile.mkdtemp(prefix="snperm")
    saved = {k: os.environ.get(k) for k in _PG_ENV}
    try:
        _run([f"{bin_dir}/initdb", "-D", f"{work}/data", "-A", "trust", "-U", "postgres"],
             "initdb")
        _run([f"{bin_dir}/pg_ctl", "-D", f"{work}/data", "-o",
              f"-k {work} -c listen_addresses=", "-l", f"{work}/pg.log", "-w", "start"],
             "postgres start")
        psql = [f"{bin_dir}/psql", "-h", work, "-U", "postgres", "-X", "-q", "-At",
                "-v", "ON_ERROR_STOP=1"]
        _run(psql + ["-c", "CREATE ROLE smartnode LOGIN SUPERUSER"], "role")
        _run(psql + ["-c", "CREATE DATABASE smartnode OWNER smartnode"], "database")
        for name in BOOTSTRAP:
            _run(psql + ["-d", "smartnode", "-f", str(ROOT / name)], name)

        def sql(text: str) -> str:
            return _run(psql + ["-d", "smartnode", "-c", text], "fixture sql").strip()

        # The pool may already point elsewhere; it must be rebuilt on this socket.
        db.close_pool()
        os.environ.update(PGHOST=work, PGDATABASE="smartnode", PGUSER="smartnode")
        os.environ.pop("PGPORT", None)
        yield sql
    finally:
        db.close_pool()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        subprocess.run([f"{bin_dir}/pg_ctl", "-D", f"{work}/data", "stop", "-m", "fast"],
                       capture_output=True)
        shutil.rmtree(work, ignore_errors=True)


ACTORS_SQL = """
INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) VALUES
  ('agent:permadmin', 'permadmin', 'agent', 'specialist', 'agent', 'internal'),
  ('agent:permother', 'permother', 'agent', 'specialist', 'agent', 'internal'),
  ('agent:permuser',  'permuser',  'agent', 'specialist', 'agent', 'internal'),
  ('component:perminstaller', 'perminstaller', 'component', 'system', 'component', 'internal')
  ON CONFLICT (id) DO NOTHING;
INSERT INTO sn_core.actor_roles (actor_id, role, source) VALUES
  ('agent:permadmin', 'admin', 'owner_grant'),
  ('agent:permother', 'admin', 'owner_grant'),
  ('component:perminstaller', 'installer', 'seed')
  ON CONFLICT (actor_id) DO NOTHING;
"""


def principals(sql):
    """Callers with real actor rows: the seeded owner, two admins, an actor
    with NO role row, which the chokepoint resolves to `user`, and a component
    holding the installer role, which has none of the people-facing grants."""
    from auth import Principal

    sql(ACTORS_SQL)
    owner_actor = sql("SELECT actor_id FROM sn_core.actor_roles WHERE role = 'super-admin'")
    tenant = int(sql("SELECT min(id) FROM sn_config.tenants"))
    assert owner_actor, "the seed must leave exactly one super-admin"
    return {
        "owner": Principal(username="admin", user_id=1, tenant_id=tenant, role="super-admin",
                           actor_id=owner_actor, actor_kind="human", strength="session"),
        "admin": Principal(username="permadmin", user_id=2, tenant_id=tenant, role="agent",
                           actor_id="agent:permadmin", actor_kind="agent", strength="proved"),
        "other": Principal(username="permother", user_id=3, tenant_id=tenant, role="agent",
                           actor_id="agent:permother", actor_kind="agent", strength="proved"),
        "user": Principal(username="permuser", user_id=4, tenant_id=tenant, role="agent",
                          actor_id="agent:permuser", actor_kind="agent", strength="proved"),
        "installer": Principal(username="perminstaller", user_id=5, tenant_id=tenant,
                               role="agent", actor_id="component:perminstaller",
                               actor_kind="component", strength="proved"),
    }
