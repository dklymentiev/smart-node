#!/usr/bin/env bash
# Operator registration through core-api, against a disposable database that
# holds nothing but a fresh B02 schema and seed.
# --baseline REF runs the same checks on an older tree (expected red).
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

mkdir -p "$WORK/bootstrap"
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

PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    SN_AGENTS_ROOT="$WORK/agents" SN_AGENT_TEMPLATE="$WORK/template" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$CORE_API" "$PY" "$ROOT/tests/operator_registration_behavior.py"
