#!/usr/bin/env bash
# B02 role catalogue and central permission check, against a disposable DB.
# --baseline REF runs the same assertions on an older tree (expected red).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PY" ]] || { echo "cannot run: $PY not found (set CORE_API_PY)" >&2; exit 2; }
BASELINE=""
if [[ "${1:-}" == --baseline ]]; then BASELINE="${2:?baseline ref required}"; fi
WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >"$WORK/init.log"
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1 -d smartnode)
"$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1 \
    -c 'CREATE ROLE smartnode LOGIN SUPERUSER' postgres
"$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1 \
    -c 'CREATE DATABASE smartnode OWNER smartnode' postgres

mkdir -p "$WORK/bootstrap" "$WORK/core-api"
for f in B02-schema.sql B02-sn-core-schema.sql B02-sn-core-seed.sql; do
    if [[ -n "$BASELINE" ]]; then
        git -C "$ROOT" show "$BASELINE:bootstrap/$f" >"$WORK/bootstrap/$f"
    else
        cp "$ROOT/bootstrap/$f" "$WORK/bootstrap/$f"
    fi
done
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/core-api | tar -x -C "$WORK"
    CORE_API="$WORK/companions/core-api"
else
    CORE_API="$ROOT/companions/core-api"
fi

for f in B02-schema.sql B02-sn-core-schema.sql B02-sn-core-seed.sql; do
    "${PSQL[@]}" -f "$WORK/bootstrap/$f" >"$WORK/apply.log" 2>&1 || {
        tail -25 "$WORK/apply.log"; exit 1;
    }
done

# 1.12.48-shaped update: users and agents existed before actor_roles.
"${PSQL[@]}" -c "
INSERT INTO sn_config.agents (tenant_id, slug, title, capability) VALUES
  (1, 'testjohn', 'Test John', 'configurator'),
  (1, 'testmax', 'Test Max', 'sysadmin'),
  (1, 'testplain', 'Test Plain', '');
INSERT INTO sn_config.users (tenant_id, email, role, active) VALUES
  (1, 'testjohn', 'agent', true),
  (1, 'testmax', 'agent', true),
  (1, 'testplain', 'agent', true);
" >"$WORK/fixture.log" 2>&1
"${PSQL[@]}" -f "$WORK/bootstrap/B02-sn-core-schema.sql" >"$WORK/reapply.log" 2>&1 || {
    tail -25 "$WORK/reapply.log"; exit 1;
}
"${PSQL[@]}" -f "$WORK/bootstrap/B02-sn-core-seed.sql" >>"$WORK/reapply.log" 2>&1 || {
    tail -25 "$WORK/reapply.log"; exit 1;
}

PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    SN_AGENTS_ROOT="$WORK/agents" SN_AGENT_TEMPLATE="$WORK/template" \
    PYTHONPATH="$CORE_API" "$PY" "$ROOT/tests/roles_4b_behavior.py"

# An owner's explicit role change survives B02 reapply.
"${PSQL[@]}" -c "UPDATE sn_core.actor_roles SET role='user', source='owner_grant' WHERE actor_id='agent:testmax'" >/dev/null
"${PSQL[@]}" -f "$WORK/bootstrap/B02-sn-core-schema.sql" >"$WORK/third-apply.log" 2>&1 || {
    tail -25 "$WORK/third-apply.log"; exit 1;
}
role_after="$("${PSQL[@]}" -A -t -c "SELECT role FROM sn_core.actor_roles WHERE actor_id='agent:testmax'")"
[[ "$role_after" == user ]] || { echo "FAIL: B02 replaced owner role with $role_after"; exit 1; }
echo "PASS: owner role change survives B02 reapply"
