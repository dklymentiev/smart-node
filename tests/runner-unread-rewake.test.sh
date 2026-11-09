#!/usr/bin/env bash
#
# A message that is still unread is still a reason to wake, a bounded number of times.
#
# WHY THIS EXISTS. On 2026-10-10 an agent got two messages a second apart, was
# launched once, answered one, and the other sat unread for seven and a half
# minutes with the agent free: a flow stood still until a person woke the agent
# by hand. The Runner kept this in its own memory, which a restart lost; since
# 2026-10-29 it is kept on the message and answered by core-api's poll.
#
# THE PROPERTIES:
#   1. a new message wakes at once
#   2. an unread message with nothing newer does not wake again at once
#   3. after the interval it wakes again
#   4. and again, each no sooner than the interval after the last
#   5. after the last allowed attempt it stops, and says so once
#   6. a newer message starts over
#   7. an agent with nothing unread is not woken
#   8. the storm guard holds: the same unread message never wakes on every tick
#   9. the interval runs from the end of the turn, not from the launch
#  10. a wake that failed before the model uses up no reminder, marks nothing
#      broken, and waits longer each time
#  11. a wake's working text is not posted as its answer
#
# Real tick, poll and claim on a disposable Postgres. Needs server binaries and a
# Python with core-api's libraries (PG_BIN, CORE_API_PY), and a non-root user.
#
#   tests/runner-unread-rewake.test.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PY" ]] || { echo "cannot run: $PY not found (set CORE_API_PY)" >&2; exit 2; }
WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "runner: an unread message is delivered again, a bounded number of times"

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >"$WORK/init.log"
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c 'CREATE ROLE smartnode LOGIN SUPERUSER' postgres
"${PSQL[@]}" -c 'CREATE DATABASE smartnode OWNER smartnode' postgres
for f in B02-schema.sql B02-sn-core-schema.sql B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$ROOT/bootstrap/$f" >"$WORK/apply.log" 2>&1 \
        || { tail -20 "$WORK/apply.log"; exit 1; }
done
"${PSQL[@]}" -d smartnode -c "INSERT INTO sn_config.agents (tenant_id, slug, title) VALUES (1, 'hazel', 'Hazel')"

PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    SN_AGENTS_ROOT="$WORK/agents" SN_AGENT_TEMPLATE="$WORK/template" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/companions/core-api" \
    "$PY" "$ROOT/tests/runner_unread_rewake.py" "$ROOT/companions/brin/runner.py"
