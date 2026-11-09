#!/usr/bin/env bash
# admin-runner's door, on a disposable database built from the bootstrap files:
# the caller must be proved by core-api, AND the account it names must hold the
# operation's permission by role. Neither check replaces the other.
#
# The two chains that create an agent end here: the installer (root on
# core-api's socket, which core-api names as the owner) and the configurator
# (an agent whose capability carries the admin role). Both must be admitted to
# ensure_agent_users. No operation is executed by this test.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
if ! "$PY" -c 'import fastapi, httpx, psycopg2' 2>/dev/null; then
    for candidate in "$ROOT/.venv/bin/python" "$HOME"/.venvs/*/bin/python python3; do
        if "$candidate" -c 'import fastapi, httpx, psycopg2' 2>/dev/null; then PY="$candidate"; break; fi
    done
fi
"$PY" -c 'import fastapi, httpx, psycopg2' 2>/dev/null \
    || { echo "cannot run: no Python with fastapi, httpx and psycopg2 (set CORE_API_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >"$WORK/init.log"
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" -l "$WORK/pg.log" -w start >/dev/null
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c 'CREATE ROLE smartnode LOGIN SUPERUSER' postgres
"${PSQL[@]}" -c 'CREATE DATABASE smartnode OWNER smartnode' postgres
for f in B02-schema.sql B02-sn-core-schema.sql B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$ROOT/bootstrap/$f" >"$WORK/apply.log" 2>&1 \
        || { tail -20 "$WORK/apply.log"; exit 1; }
done

# The two agents are created the way core-api creates them: an agents row, and
# the account, actor and capability role core-api derives for it.
export PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode
export SN_AGENTS_ROOT="$WORK/agents" SN_AGENT_TEMPLATE="$WORK/template"
OWNER="$(PYTHONPATH="$ROOT/companions/core-api" "$PY" - <<'PY'
import db
from routes.agents import _ensure_agent_identity

with db.cursor(commit=True) as cur:
    cur.execute("INSERT INTO sn_config.agents (tenant_id, slug, title, capability) VALUES "
                "(1, 'roleconfig', 'Configurator', 'configurator'), (1, 'roleplain', 'Plain', '')")
    _ensure_agent_identity(cur, "roleconfig", "Configurator", 1)
    _ensure_agent_identity(cur, "roleplain", "Plain", 1)
    cur.execute("SELECT u.email FROM sn_config.users u JOIN sn_core.actor_roles r "
                "ON r.actor_id = u.actor_id WHERE r.role = 'super-admin' AND u.active")
    print(cur.fetchone()["email"])
PY
)"
[[ -n "$OWNER" ]] || { echo "cannot run: the seed has no owner account" >&2; exit 2; }

echo "admin-runner roles (working tree)"
SN_ADMIN_DB_USER=smartnode SN_ADMIN_DB_NAME=smartnode \
    PYTHONPATH="$ROOT/companions/admin-runner" \
    "$PY" "$ROOT/tests/admin_runner_roles.py" "$OWNER" roleconfig roleplain
