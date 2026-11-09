#!/usr/bin/env bash
# Registered MCP servers need one grantable catalogue row each, including on
# nodes registered before that rule existed. Exercise the real seed against a
# disposable PostgreSQL cluster, with no running-node access.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PY" ]] || { echo "cannot run: $PY not found (set CORE_API_PY)" >&2; exit 2; }
BASELINE=""
if [[ "${1:-}" == "--baseline" ]]; then BASELINE="${2:?missing baseline ref}"; fi
WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN SUPERUSER" >/dev/null
"${PSQL[@]}" -c "CREATE DATABASE smartnode OWNER smartnode" >/dev/null
for f in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$ROOT/$f" >"$WORK/schema.log" 2>&1
done
"${PSQL[@]}" -d smartnode -c "
  INSERT INTO sn_config.mcp_servers (slug, name, transport)
  SELECT slug, slug, 'stdio' FROM unnest(ARRAY[
    'screenbox','analytics','google-ads','laptop-operator','secrets','operator'
  ]) AS slug;" >/dev/null

SEED="$ROOT/companions/core-api/seed-tools.py"
if [[ -n "$BASELINE" ]]; then
    SEED="$WORK/seed-tools.py"
    git -C "$ROOT" show "$BASELINE:companions/core-api/seed-tools.py" > "$SEED"
fi
PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    SN_CORE_API=http://127.0.0.1:8030 "$PY" "$SEED" >"$WORK/seed.log" 2>&1

count="$("${PSQL[@]}" -d smartnode -tAc "
  SELECT count(*) FROM sn_config.mcp_servers s JOIN sn_core.tools t
    ON t.name=s.slug AND t.kind='mcp' AND t.mcp_server_id=s.id
  WHERE s.slug IN ('screenbox','analytics','google-ads',
                   'laptop-operator','secrets','operator')")"
if [[ "$count" == 6 ]]; then
    echo "PASS: all 6 registered MCP servers have grantable umbrella rows"
else
    echo "FAIL: only $count of 6 registered MCP servers have umbrella rows"
    exit 1
fi
