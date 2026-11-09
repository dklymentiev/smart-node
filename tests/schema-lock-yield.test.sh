#!/usr/bin/env bash
#
# The schema stage must yield to live traffic, not deadlock against it.
#
# WHAT WENT WRONG. On 2026-09-04 an update of a working node died here:
#
#   psql:<stdin>:719: ERROR:  deadlock detected
#   DETAIL: Process 2343887 waits for AccessExclusiveLock on relation 17246;
#           blocked by process 2319096. Process 2319096 waits for
#           AccessShareLock on relation 16956; blocked by process 2343887.
#
# B02 wanted an exclusive lock on sn_config.users while a live agent session
# held it and was waiting to read sn_config.contact_channels. Postgres broke
# the cycle by killing the migration. The stand had taken the same release
# minutes earlier and passed, because the stand is idle. So the failure is
# intermittent by construction, and it teaches everyone to re-run rather than
# to look.
#
# WHAT IS CHECKED. Not the source -- the behaviour, with psql and systemctl
# stubbed, so this runs anywhere and needs no cluster:
#
#   yields           a lock timeout is retried, and a later attempt that gets
#                    the lock still applies the file
#   refuses-loudly   a lock it never gets ends the stage with the relation and
#                    the holding process named, not with a bare deadlock
#   not-a-lock       an error that is NOT about locks fails on the first
#                    attempt: retrying a syntax error only lengthens an outage
#   quiesce          only units that are actually running are paused, and each
#                    one is started again
#   reports          the pause names the services and the duration, so an
#                    update that stopped the agents says so
#   resumes-on-death the wakers come back even when the apply dies
#
#   tests/schema-lock-yield.test.sh                 # working tree, expect PASS
#   tests/schema-lock-yield.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REL="bootstrap/B02-persistence.sh"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" show "$BASELINE:$REL" > "$WORK/variant-src.sh" 2>/dev/null \
        || { echo "cannot read $REL at $BASELINE" >&2; exit 2; }
    WHAT="$BASELINE"
else
    cp "$ROOT/$REL" "$WORK/variant-src.sh"
    WHAT="working tree"
fi

# The stage is made sourceable without touching the shipped file: its lib.sh
# is pointed at the stub, and the mode dispatch is cut off the end so sourcing
# it does not run an install.
sed -i "s#^LIB=\"/usr/local/lib/smart-node/lib.sh\"#LIB=\"$WORK/lib.sh\"#" "$WORK/variant-src.sh"
sed -i '/^case "\$MODE" in$/,$d' "$WORK/variant-src.sh"

cat > "$WORK/lib.sh" <<'STUB'
log() { echo "$*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }
manifest_record() { :; }
require_root() { :; }
require_stage() { :; }
log_init() { :; }
STUB

fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

echo "$REL ($WHAT)"

# One scenario per subshell, with no arguments: the stage parses $1 as its own
# mode, so the work directory arrives by environment instead.
run_case() {   # body on stdin -> combined output on stdout
    cat > "$WORK/case.sh"
    SN_TEST_W="$WORK" bash "$WORK/case.sh" 2>&1
}

# ---------------------------------------------------------------- yields ---
out=$(run_case <<'CASE'
set -uo pipefail
W="$SN_TEST_W"
source "$W/variant-src.sh"
declare -F apply_sql_file >/dev/null || { echo "NO-APPLY-SQL-FILE"; exit 0; }
SCHEMA_APPLY_BACKOFF=0
SCHEMA_APPLY_ATTEMPTS=4
# The counter lives in a file: psql_core runs on the right of a pipeline, so a
# variable it sets would be lost with the subshell.
echo 0 > "$W/attempts"
psql_core() {
    local n; n=$(< "$W/attempts"); n=$((n + 1)); echo "$n" > "$W/attempts"
    cat >/dev/null
    if (( n < 3 )); then
        echo "psql:<stdin>:721: ERROR:  canceling statement due to lock timeout"
        return 3
    fi
    return 0
}
report_lock_blockers() { :; }
echo "x" > "$W/schema.sql"
if apply_sql_file "$W/schema.sql" "test schema"; then
    echo "APPLIED after $(< "$W/attempts") attempts"
else
    echo "GAVE UP after $(< "$W/attempts") attempts"
fi
CASE
)
case "$out" in
    *NO-APPLY-SQL-FILE*)          bad "yields (the stage has no retry at all)" ;;
    *"APPLIED after 3 attempts"*) ok  "yields: a lock timeout is retried and the file still lands" ;;
    *)                            bad "yields: expected APPLIED after 3 attempts, got: $out" ;;
esac

# -------------------------------------------------------- refuses loudly ---
out=$(run_case <<'CASE'
set -uo pipefail
W="$SN_TEST_W"
source "$W/variant-src.sh"
declare -F apply_sql_file >/dev/null || { echo "NO-APPLY-SQL-FILE"; exit 0; }
SCHEMA_APPLY_BACKOFF=0
SCHEMA_APPLY_ATTEMPTS=2
# A deadlock every time: the stage must stop, and must say who held what.
psql_core() {
    cat >/dev/null
    echo "psql:<stdin>:719: ERROR:  deadlock detected"
    echo "DETAIL: Process 2343887 waits for AccessExclusiveLock on relation 17246"
    return 3
}
report_lock_blockers() {
    echo "    relation 17246 is sn_config.users"
    echo "    lock holder: 2319096 | active | sn_config.contact_channels | SELECT ..."
}
echo "x" > "$W/schema.sql"
apply_sql_file "$W/schema.sql" "test schema" && echo "UNEXPECTED-SUCCESS"
CASE
)
if grep -q "sn_config.users" <<<"$out" \
   && grep -q "lock holder: 2319096" <<<"$out" \
   && grep -qi "after 2 attempts" <<<"$out" \
   && ! grep -q "UNEXPECTED-SUCCESS" <<<"$out"; then
    ok "refuses-loudly: names the relation and the process, and stops"
else
    bad "refuses-loudly: expected the relation and the holder named, got: $out"
fi

# ------------------------------------------------------------ not a lock ---
out=$(run_case <<'CASE'
set -uo pipefail
W="$SN_TEST_W"
source "$W/variant-src.sh"
declare -F apply_sql_file >/dev/null || { echo "NO-APPLY-SQL-FILE"; exit 0; }
SCHEMA_APPLY_BACKOFF=0
SCHEMA_APPLY_ATTEMPTS=5
echo 0 > "$W/attempts2"
psql_core() {
    local n; n=$(< "$W/attempts2"); n=$((n + 1)); echo "$n" > "$W/attempts2"
    cat >/dev/null
    echo "psql:<stdin>:12: ERROR:  syntax error at or near \"CRATE\""
    return 3
}
report_lock_blockers() { :; }
echo "x" > "$W/schema.sql"
apply_sql_file "$W/schema.sql" "test schema"
CASE
)
tried=$(cat "$WORK/attempts2" 2>/dev/null || echo "?")
if [[ "$tried" == "1" ]]; then
    ok "not-a-lock: a real error fails on the first attempt"
else
    bad "not-a-lock: expected one attempt, the stage made $tried"
fi

# --------------------------------------------------- quiesce and resume ----
out=$(run_case <<'CASE'
set -uo pipefail
W="$SN_TEST_W"
source "$W/variant-src.sh"
declare -F quiesce_wakers >/dev/null || { echo "NO-QUIESCE"; exit 0; }
: > "$W/systemctl.log"
# The runner is up, the dispatcher is not: only the running one may be touched.
systemctl() {
    case "$1" in
        is-active)  [[ "$2" == smart-node-runner.service ]] ;;
        stop|start) echo "$1 $2" >> "$W/systemctl.log" ;;
        *)          return 0 ;;
    esac
}
quiesce_wakers
sleep 1
resume_wakers
echo "LOG:"; cat "$W/systemctl.log"
echo "REPORT: $QUIESCE_REPORT"
CASE
)
if grep -q "NO-QUIESCE" <<<"$out"; then
    bad "quiesce (the stage never pauses the wakers)"
    bad "reports (there is no pause to report)"
else
    if grep -q "stop smart-node-runner.service" <<<"$out" \
       && grep -q "start smart-node-runner.service" <<<"$out" \
       && ! grep -q "dispatch" <<<"$out"; then
        ok "quiesce: pauses only what is running, and starts it again"
    else
        bad "quiesce: expected stop+start of the running unit only, got: $out"
    fi
    if grep -qE "REPORT: smart-node-runner\.service paused for [0-9]+s" <<<"$out"; then
        ok "reports: the pause names the service and the duration"
    else
        bad "reports: expected a named service and a duration, got: $out"
    fi
fi

# ---------------------------------------------------- resumes on death -----
rm -f "$WORK/systemctl2.log"
out=$(run_case <<'CASE'
set -uo pipefail
W="$SN_TEST_W"
source "$W/variant-src.sh"
declare -F quiesce_wakers >/dev/null || { echo "NO-QUIESCE"; exit 0; }
: > "$W/systemctl2.log"
systemctl() {
    case "$1" in
        is-active)  [[ "$2" == smart-node-runner.service ]] ;;
        stop|start) echo "$1 $2" >> "$W/systemctl2.log" ;;
        *)          return 0 ;;
    esac
}
quiesce_wakers
trap resume_wakers EXIT
# The apply dies the way B02 dies. The wakers must still come back.
die "schema could not take its lock"
CASE
)
if grep -q "start smart-node-runner.service" "$WORK/systemctl2.log" 2>/dev/null; then
    ok "resumes-on-death: the wakers come back when the apply dies"
else
    bad "resumes-on-death: the node would be left with its wakers stopped ($out)"
fi

echo
if (( fails > 0 )); then
    echo "FAIL  $fails check(s) failed  [$REL @ $WHAT]"
    exit 1
fi
echo "PASS  all checks  [$REL @ $WHAT]"
