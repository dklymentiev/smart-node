#!/usr/bin/env bash
#
# Gate: B02 verify "session context global limit present" proves the global
# row EXISTS, not that it still holds the seed value.
#
# WHY THIS EXISTS. The check used to ask for threshold_tokens=120000. The seed
# keeps an owner-set value and the panel lets the owner change it, so on
# example-node (owner set 150000 on 2026-09-22) the 1.12.45 update stopped at B02
# with exit 3. An update must never fail because the owner changed a setting
# through the product.
#
# WHAT IS CHECKED. The query is taken out of B02-persistence.sh as it ships and
#   1. it must not pin the seed value;
#   2. when a postgres is reachable, it is RUN against a throwaway database:
#      a global row of 150000 must satisfy it, no global row must not.
#   Without a postgres, part 2 prints SKIP (never PASS) and only part 1 counts.
#
# USAGE:
#   tests/b02-global-threshold.test.sh                 # working tree
#   tests/b02-global-threshold.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
B02_REL="bootstrap/B02-persistence.sh"
CHECK_NAME="session context global limit present"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
if [[ -n "$BASELINE" ]]; then
    SRC="$(git -C "$ROOT" show "$BASELINE:$B02_REL" 2>/dev/null)" \
        || { echo "cannot read $B02_REL at $BASELINE" >&2; exit 2; }
    WHAT="$BASELINE"
else
    SRC="$(cat "$ROOT/$B02_REL")"
    WHAT="working tree"
fi
echo "$B02_REL ($WHAT)"

FAILS=0
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }

# The query sits two lines under the check's name, between \" and \".
QUERY="$(printf '%s\n' "$SRC" | grep -F -A4 "verify_check \"$CHECK_NAME\"" \
    | grep -o 'SELECT [^"\\]*' | head -1)"
if [[ -z "$QUERY" ]]; then
    echo "FAIL: verify check \"$CHECK_NAME\" or its query not found"
    exit 1
fi
echo "query: $QUERY"

# --- 1: the value is the owner's, the check must not pin the seed ---
if [[ "$QUERY" =~ threshold_tokens[[:space:]]*=[[:space:]]*[0-9] ]]; then
    fail "the check pins threshold_tokens to one value; an owner-set threshold fails the update"
else
    echo "ok    the check does not pin the seed value"
fi

# --- 2: run the query against a scratch database ---
if [[ $EUID -eq 0 ]] && command -v runuser >/dev/null && runuser -u postgres -- psql -tAc 'SELECT 1' >/dev/null 2>&1; then
    PSQL=(runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q)
elif psql -tAc 'SELECT 1' >/dev/null 2>&1; then
    PSQL=(psql -v ON_ERROR_STOP=1 -q)
else
    PSQL=()
fi

if [[ ${#PSQL[@]} -eq 0 ]]; then
    echo "SKIP  no reachable postgres: the query was not run"
else
    DB="t4850_$$"
    "${PSQL[@]}" -d postgres -c "CREATE DATABASE $DB" >/dev/null || { echo "FAIL: cannot create $DB"; exit 1; }
    trap '"${PSQL[@]}" -d postgres -c "DROP DATABASE IF EXISTS $DB" >/dev/null 2>&1' EXIT
    # The table as B02-sn-core-schema.sql declares it, the parts the query reads.
    "${PSQL[@]}" -d "$DB" -c "
        CREATE SCHEMA sn_config;
        CREATE TABLE sn_config.session_context_limits (
            scope_type text NOT NULL, scope_id text,
            threshold_tokens integer NOT NULL DEFAULT 120000
                CHECK (threshold_tokens BETWEEN 10000 AND 1000000),
            enabled boolean NOT NULL DEFAULT true);" >/dev/null

    run_q() { "${PSQL[@]}" -d "$DB" -tAc "$QUERY" 2>/dev/null; }

    "${PSQL[@]}" -d "$DB" -c "INSERT INTO sn_config.session_context_limits VALUES ('global', NULL, 150000, true)" >/dev/null
    if [[ "$(run_q)" == "1" ]]; then
        echo "ok    owner-set global 150000 passes"
    else
        fail "an owner-set global threshold (150000) fails the check"
    fi

    "${PSQL[@]}" -d "$DB" -c "DELETE FROM sn_config.session_context_limits" >/dev/null
    "${PSQL[@]}" -d "$DB" -c "INSERT INTO sn_config.session_context_limits VALUES ('agent', '7', 150000, true)" >/dev/null
    if [[ -z "$(run_q)" ]]; then
        echo "ok    no global row fails the check"
    else
        fail "the check passes with no global row"
    fi
fi

if [[ $FAILS -gt 0 ]]; then
    echo "RESULT: FAIL ($FAILS)"
    exit 1
fi
echo "RESULT: PASS"
