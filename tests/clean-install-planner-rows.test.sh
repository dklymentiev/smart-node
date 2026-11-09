#!/usr/bin/env bash
#
# A fresh node gets the planner api rows without anyone restarting core-api.
#
# WHAT WENT WRONG. seed-tools (core-api ExecStartPre) writes the planner rows for
# agents on an HTTP model with a foreign key to the planner integration.
# On a clean install core-api is installed before the tracking stage registers
# that integration, so the insert died on the key, seed-tools exited 1, core-api
# never started and the install stopped at its first Phase 2 companion. The same
# failure the 2026-08-16 core-api integration fix in the same file describes.
# An update never saw it: the integration was there from an earlier release.
#
# WHAT IS CHECKED. seed-tools from the tree under test, run for real against a
# throwaway postgres built from the tree's B02 files (nothing on the host is
# touched), and the tracking stage read from source.
#
#   fresh-seed     no planner integration yet: seed-tools exits 0
#   fresh-skip     and writes no planner rows, saying why in its log
#   registered     after the integration is registered, the next seed writes all
#                  five planner rows (planner_task, _comments, _search, _comment,
#                  _advance)
#   tracking       tracking.sh reseeds by itself right after it registers the
#                  planner integration: reseed_planner_rows follows the planner
#                  sn_integration_register, restarts core-api only when the rows
#                  are missing, and waits for core-api to answer
#
# USAGE
#   tests/clean-install-planner-rows.test.sh                  # working tree, expect PASS
#   tests/clean-install-planner-rows.test.sh --baseline REF   # git:REF, expect FAIL
#
# Needs postgres server binaries (PG_BIN) and the core-api venv (CORE_API_PY).
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PG_BIN/initdb" ]] || { echo "cannot run: no initdb (set PG_BIN)" >&2; exit 2; }
"$PY" -c "import psycopg2" 2>/dev/null \
    || { echo "cannot run: $PY lacks psycopg2 (set CORE_API_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

# The code under test comes from the ref; the database is always the tree's.
mkdir -p "$WORK/src"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/core-api companions/tracking.sh \
        | tar -x -C "$WORK/src" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$WORK/src/companions"
    cp -r "$ROOT/companions/core-api" "$ROOT/companions/tracking.sh" "$WORK/src/companions/"
fi

echo "clean install: planner api rows (${BASELINE:+git:}${BASELINE:-working tree})"

PASS=0; FAIL=0
res() { if [[ "$2" == 1 ]]; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1  ${3:-}"; fi; }

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null 2>&1 \
    || { echo "cannot run: initdb failed" >&2; exit 2; }
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null 2>&1 \
    || { echo "cannot run: postgres did not start" >&2; cat "$WORK/pg.log" >&2; exit 2; }
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN SUPERUSER" >/dev/null
"${PSQL[@]}" -c "CREATE DATABASE smartnode OWNER smartnode" >/dev/null
for f in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$ROOT/$f" >>"$WORK/apply.log" 2>&1 \
        || { echo "cannot run: $f did not apply" >&2; tail -5 "$WORK/apply.log" >&2; exit 2; }
done
"${PSQL[@]}" -d smartnode -c "DELETE FROM sn_config.integrations WHERE slug = 'planner'" >/dev/null

seed() {
    ( cd "$WORK/src/companions/core-api" \
      && PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode SN_CORE_API="http://127.0.0.1:8030" \
         "$PY" seed-tools.py ) >"$WORK/seed.$1.log" 2>&1
}
rows() {
    "${PSQL[@]}" -d smartnode -tAc "SELECT count(*) FROM sn_core.tools WHERE name IN
        ('planner_task','planner_comments','planner_search','planner_comment','planner_advance')"
}

# fresh node: the tracking stage has not registered the planner integration yet
seed fresh; rc=$?
res "fresh-seed: seed-tools exits 0 with no planner integration" \
    "$([[ $rc == 0 ]] && echo 1 || echo 0)" \
    "rc=$rc: $(grep -m1 -E 'Error|violates' "$WORK/seed.fresh.log")"
n="$(rows)"
res "fresh-skip: no planner rows written, and the log says why" \
    "$([[ "$n" == 0 ]] && grep -q 'planner integration not registered' "$WORK/seed.fresh.log" && echo 1 || echo 0)" \
    "rows=$n"

# the tracking stage registers the integration; the next seed writes the rows
"${PSQL[@]}" -d smartnode -c "INSERT INTO sn_config.integrations (slug, label, kind, status)
    VALUES ('planner', 'Planner', 'app', 'available')" >/dev/null
seed registered; rc=$?
n="$(rows)"
res "registered: the next seed writes all five planner rows" \
    "$([[ $rc == 0 && "$n" == 5 ]] && echo 1 || echo 0)" "rc=$rc rows=$n"

# tracking.sh triggers that seed by itself
python3 - "$WORK/src/companions/tracking.sh" <<'PY' >"$WORK/tracking.out"
import re, sys
s = open(sys.argv[1]).read()
reg = s.find("--slug             planner")
nxt = s.find("# node-admin tool", reg)
fn = re.search(r"\nreseed_planner_rows\(\) \{\n(.*?)\n\}\n", s, re.S)
called = reg != -1 and nxt != -1 and "reseed_planner_rows" in s[reg:nxt]
body = fn.group(1) if fn else ""
only_missing = "planner_advance" in body and '"0"' in body
restarts = "systemctl restart smart-node-core-api" in body and "/api/health" in body
print("ok" if (fn and called and only_missing and restarts) else
      f"defined={bool(fn)} called_after_register={called} only_when_missing={only_missing} restart_and_wait={restarts}")
PY
t="$(cat "$WORK/tracking.out")"
res "tracking: reseeds by itself after registering the planner integration" \
    "$([[ "$t" == ok ]] && echo 1 || echo 0)" "$t"

echo
if (( FAIL == 0 )); then echo "PASS: $PASS checks"; exit 0; fi
echo "FAIL: $FAIL of $((PASS+FAIL))"; exit 1
