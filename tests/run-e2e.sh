#!/usr/bin/env bash
# run-e2e.sh -- run the node test suite and record the result as evidence.
#
# Wraps tests/run-all.sh (per-repo unit tests + live e2e scripts).
# On success, writes e2e-evidence/YYYY-MM-DD.json so the release gate (6d
# in tools/release.sh) can confirm the suite was passing before a release
# is published.
#
# Intended callers:
#   1. Before each release, from the dev machine:
#        bash tests/run-e2e.sh
#      After a passing run, commit and push e2e-evidence/YYYY-MM-DD.json to the
#      repo. The release gate (gate-6d in tools/release.sh) reads committed files
#      only; a file written but not yet committed will not satisfy the gate on a
#      fresh clone or on the CI machine that runs the release.
#   2. Manual (ad-hoc, on a node with live services):
#        bash tests/run-e2e.sh
#
# Requirements: same as tests/run-all.sh -- a running node with psql access
# and the component services up. The individual e2e scripts say what they
# need (some require root, some skip gracefully without it).
#
# Exit 0 = suite passed (evidence written).
# Exit 1 = suite failed (evidence NOT written; fix the node first).
# Exit 2 = setup error (cannot determine commit or write evidence dir).
#
# Usage:
#   run-e2e.sh [--evidence-dir DIR] [--only NAME.sh ...]
# A selected run writes DATE-selected.json and cannot satisfy the full-suite gate.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

EVIDENCE_DIR="$REPO_ROOT/e2e-evidence"
SELECTED=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --evidence-dir) EVIDENCE_DIR="$2"; shift 2 ;;
        --only)
            name="${2:-}"
            if [[ ! "$name" =~ ^[a-z0-9-]+\.sh$ || ! -f "$HERE/e2e/$name" ]]; then
                echo "--only requires an existing tests/e2e/<name>.sh" >&2; exit 2
            fi
            SELECTED+=("$name"); shift 2 ;;
        --help|-h) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
NODE="$(hostname -s 2>/dev/null || echo unknown)"
DATE_KEY="$(date -u +%Y-%m-%d)"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "[run-e2e] node=$NODE commit=$COMMIT date=$DATE_KEY"
echo "[run-e2e] selected=${SELECTED[*]:-full suite}"
echo

mkdir -p "$EVIDENCE_DIR" || { echo "[run-e2e] cannot create $EVIDENCE_DIR" >&2; exit 2; }
suite_out="$(mktemp "$EVIDENCE_DIR/.run-XXXXXX")" || exit 2
trap 'rm -f "$suite_out"' EXIT

run_suite() {
    if (( ${#SELECTED[@]} == 0 )); then
        bash "$HERE/run-all.sh"
    else
        local name failures=0
        for name in "${SELECTED[@]}"; do
            echo "-- $name --"
            bash "$HERE/e2e/$name" || failures=1
        done
        return "$failures"
    fi
}

passed=0; failed=0; suite_rc=0
if run_suite 2>&1 | tee "$suite_out"; then
    suite_rc=0
else
    suite_rc=1
fi

# Count e2e script results from the run-all.sh output.
# Scripts use two formats: "RESULT: PASS/FAIL" (agent-comms) and
# "ALL GREEN / FAILURES" (history-watermark, permissions, returning-user).
# `grep -c` prints its count AND exits 1 when the count is zero, so the old
# `$(grep -c ... || echo 0)` produced the two-line value "0\n0" and the
# arithmetic below died with "syntax error in expression". It only ever showed
# up on a run where one of the two categories was empty -- which is to say, on
# the first run anybody actually looked at.
passed=$(grep -cE '\[e2e\].*(RESULT: PASS|ALL GREEN)' "$suite_out" 2>/dev/null || true)
failed=$(grep -cE '\[e2e\].*(RESULT: FAIL|: FAILURES)' "$suite_out" 2>/dev/null || true)
passed=$(( ${passed:-0} ))
failed=$(( ${failed:-0} ))
rm -f "$suite_out"

echo
if [[ "$suite_rc" -eq 0 ]]; then
    result="pass"
    echo "[run-e2e] PASS (e2e: $passed passed, $failed failed)"
else
    result="fail"
    echo "[run-e2e] FAIL (e2e: $passed passed, $failed failed)"
fi

# Write evidence only on a clean pass.
if [[ "$result" == "pass" ]]; then
    ev_file="$EVIDENCE_DIR/$DATE_KEY.json"
    if (( ${#SELECTED[@]} > 0 )); then
        # A focused regression run cannot satisfy the full-suite release gate.
        ev_file="$EVIDENCE_DIR/$DATE_KEY-selected.json"
    fi
    printf '%s\n' "{
  \"date\": \"$DATE_KEY\",
  \"timestamp\": \"$TS\",
  \"commit\": \"$COMMIT\",
  \"node\": \"$NODE\",
  \"result\": \"$result\",
  \"scope\": \"${SELECTED[*]:-full suite}\",
  \"e2e_passed\": $passed,
  \"e2e_failed\": $failed
}" > "$ev_file"
    echo "[run-e2e] evidence written: $ev_file"
    if (( ${#SELECTED[@]} == 0 )); then
        echo "[run-e2e] commit and push e2e-evidence/$DATE_KEY.json to the repo so the release gate can see it"
    else
        echo "[run-e2e] selected regression evidence does not satisfy the full-suite release gate"
    fi
    exit 0
else
    echo "[run-e2e] no evidence written -- fix the failures above first"
    exit 1
fi
