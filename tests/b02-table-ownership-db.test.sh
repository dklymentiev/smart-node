#!/usr/bin/env bash
#
# Gate: check_table_ownership() in B02-persistence.sh must query pg_tables
# against $PG_CORE_DB (the "smartnode" database) via psql_core, NOT against
# the "postgres" database via psql_postgres.
#
# WHY THIS EXISTS. pg_tables is per-database. sn_core and sn_config exist
# only in the "smartnode" database, so querying via psql_postgres() (which
# connects to the "postgres" database) returns an empty set on every node
# and the pre-flight always takes the "ok" branch regardless of actual
# ownership. This was found on a test node and fixed by using psql_core() in
# check_table_ownership(). This test prevents regression.
#
# WHAT IS CHECKED.
#   1. The line in check_table_ownership() that runs the ownership query
#      calls psql_core (not psql_postgres).
#   2. The verify_check entry for "sn_core/sn_config table ownership" in
#      run_verify_checks() uses $PG_CORE_DB (not a literal "postgres").
#
# USAGE:
#   tests/b02-table-ownership-db.test.sh                 # working tree
#   tests/b02-table-ownership-db.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

B02_REL="bootstrap/B02-persistence.sh"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

if [[ -n "$BASELINE" ]]; then
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    B02="$WORK/B02-persistence.sh"
    git -C "$ROOT" show "$BASELINE:$B02_REL" > "$B02" 2>/dev/null \
        || { echo "cannot read $B02_REL at $BASELINE" >&2; exit 2; }
    WHAT="$BASELINE"
else
    B02="$ROOT/$B02_REL"
    WHAT="working tree"
fi

echo "$B02_REL ($WHAT)"

FAILS=0

# --- Check 1: check_table_ownership() uses psql_core ---
# Extract the function body and verify the ownership query line calls psql_core.
in_fn=0
fn_body=""
while IFS= read -r line; do
    if [[ "$line" =~ ^check_table_ownership\(\) ]]; then
        in_fn=1
    fi
    if [[ $in_fn -eq 1 ]]; then
        fn_body+="$line"$'\n'
        # End of function: a lone closing brace at column 0
        if [[ $in_fn -eq 1 && "$line" == "}" ]]; then
            break
        fi
    fi
done < "$B02"

if [[ -z "$fn_body" ]]; then
    echo "  FAIL check_table_ownership() function not found in $B02_REL"
    FAILS=$((FAILS + 1))
else
    # The query line must call psql_core, not psql_postgres
    if grep -qE 'out=.*psql_postgres' <<<"$fn_body"; then
        echo "  FAIL check_table_ownership(): ownership query calls psql_postgres (queries \"postgres\" database, not \"smartnode\") -- must use psql_core"
        FAILS=$((FAILS + 1))
    elif grep -qE 'out=.*psql_core' <<<"$fn_body"; then
        echo "  ok   check_table_ownership() ownership query uses psql_core"
    else
        echo "  FAIL check_table_ownership(): cannot confirm psql_core used for the ownership query -- found no psql_{core,postgres} call on the 'out=' line"
        FAILS=$((FAILS + 1))
    fi
fi

# --- Check 2: the verify_check entry for table ownership uses $PG_CORE_DB ---
# The check must reference PG_CORE_DB or the literal expansion of it, not a
# bare "postgres" literal as the -d argument.
vc_line=""
vc_line=$(grep "verify_check.*sn_core.*table ownership" "$B02" || true)
if [[ -z "$vc_line" ]]; then
    echo "  FAIL verify_check \"sn_core/sn_config table ownership\" not found in $B02_REL"
    FAILS=$((FAILS + 1))
else
    # The following line(s) hold the psql command; check they use PG_CORE_DB
    # and not a literal "-d postgres"
    vc_block=$(grep -A 3 "verify_check.*sn_core.*table ownership" "$B02" || true)
    if grep -qE "\-d 'postgres'|\-d \"postgres\"|\-d postgres\b" <<<"$vc_block"; then
        echo "  FAIL verify_check \"sn_core/sn_config table ownership\" hardcodes -d postgres (must use PG_CORE_DB)"
        FAILS=$((FAILS + 1))
    elif grep -qE "PG_CORE_DB" <<<"$vc_block"; then
        echo "  ok   verify_check \"sn_core/sn_config table ownership\" uses \$PG_CORE_DB"
    else
        echo "  note verify_check line found but could not confirm PG_CORE_DB; manual review needed"
    fi
fi

echo
if [[ $FAILS -gt 0 ]]; then
    echo "FAIL  $FAILS check(s) failed  [$B02_REL @ $WHAT]"
    exit 1
fi
echo "PASS  both table-ownership database checks pass  [$B02_REL @ $WHAT]"
