#!/usr/bin/env bash
#
# A component that stops creating an artifact must retire its manifest
# entry.
#
# WHAT WENT WRONG. /var/lib/smart-node/manifest.jsonl only ever grew.
# 1.12.25 moved mesh, mesh-mcp and planner off Docker onto systemd; the
# three docker-container rows stayed, and validate.sh replays every row
# whose stage is still installed. Every node that UPDATED rather than
# installed fresh therefore failed validation on three containers it is
# CORRECT not to have: 220/223, exit 6, finalize never ran, so the node's
# own release file kept reading the version before the update. Permanent,
# not transient -- a re-run only appended.
#
# The fix is a rule, not three names: on the run that completes a stage,
# a row of that stage which the run did not re-record and whose artifact
# is not there any more is retired. Both halves are load-bearing, and
# this test pins both, because either one alone is worse than the bug:
#
#   - retiring on "not re-recorded" alone would withdraw the row for a
#     container the stage still creates but that is DOWN -- silencing
#     exactly the failure validation exists to catch;
#   - retiring on "artifact absent" alone would let any run that never
#     touched a stage (--verify, --status, an early exit) speak for it.
#
# The checks run against a sandbox manifest, not a node: lib.sh is
# sourced and its paths repointed at a temp dir, so the behaviour itself
# is exercised, with `dir` and `file` rows standing in for the container
# rows that started this (their liveness is the same predicate).
#
#   tests/manifest-retire.test.sh                 # working tree, expect PASS
#   tests/manifest-retire.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REL="bootstrap/lib.sh"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

LIB="$SANDBOX/lib.sh"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" show "$BASELINE:$REL" > "$LIB" 2>/dev/null \
        || { echo "cannot read $REL at $BASELINE" >&2; exit 2; }
    VALIDATE_SRC="$(git -C "$ROOT" show "$BASELINE:validate.sh" 2>/dev/null)" \
        || { echo "cannot read validate.sh at $BASELINE" >&2; exit 2; }
    WHAT="$BASELINE"
else
    cp "$ROOT/$REL" "$LIB"
    VALIDATE_SRC="$(cat "$ROOT/validate.sh")"
    WHAT="working tree"
fi

fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

echo "manifest retirement ($WHAT)"

# shellcheck source=/dev/null
source "$LIB"

# Repoint the library at the sandbox. Everything below is unprivileged.
SMART_NODE_STATE_DIR="$SANDBOX/stages"
SMART_NODE_MANIFEST="$SANDBOX/manifest.jsonl"
SMART_NODE_LOG=""
mkdir -p "$SMART_NODE_STATE_DIR"

if ! declare -F manifest_retire_stale >/dev/null; then
    bad "lib.sh defines manifest_retire_stale (nothing retires a row)"
    echo "$fails failure(s)"
    exit 1
fi

# seed_row TYPE VALUE STAGE -- a row already on disk from an earlier run.
seed_row() {
    printf '{"type":"%s","value":"%s","stage":"%s","ts":"%s"}\n' \
        "$1" "$2" "$3" "2026-01-01T00:00:00+00:00" >> "$SMART_NODE_MANIFEST"
}

# retired_p TYPE VALUE -- true if that row now carries a retired stamp.
retired_p() {
    grep -q "\"type\":\"$1\",\"value\":\"$2\",.*\"retired\":" "$SMART_NODE_MANIFEST"
}

GONE="$SANDBOX/gone"          # never created -- the ex-container's stand-in
STILL="$SANDBOX/still"        # created, and still there
DOWN="$SANDBOX/down"          # claimed by this run, but absent: a real fault
LIVE_UNTOUCHED="$SANDBOX/live-untouched"
mkdir -p "$STILL" "$LIVE_UNTOUCHED"

seed_row file "$GONE"           memory
seed_row dir  "$STILL"          memory
seed_row file "$DOWN"           memory
seed_row dir  "$LIVE_UNTOUCHED" memory
seed_row file "$SANDBOX/absent-elsewhere" tracking

# The run: stage `memory` declares STILL and DOWN, and no longer declares
# GONE or LIVE_UNTOUCHED. Stage `tracking` records nothing at all.
STAGE_ID="memory"
manifest_record dir  "$STILL"
manifest_record file "$DOWN"
stage_mark memory done

# --- 1. the bug itself ----------------------------------------------------
if retired_p file "$GONE"; then
    ok "a row the stage stopped creating, whose artifact is gone, is retired"
else
    bad "a row the stage stopped creating, whose artifact is gone, is retired"
fi

# --- 2. a live artifact is never withdrawn --------------------------------
if retired_p dir "$STILL"; then
    bad "a row the stage still creates stays"
else
    ok "a row the stage still creates stays"
fi

# --- 3. no masking of a real failure --------------------------------------
# DOWN is claimed by this run and is not there. That is the fault
# validation must keep reporting, so the row must survive.
if retired_p file "$DOWN"; then
    bad "a claimed-but-missing artifact keeps its row (no masking)"
else
    ok "a claimed-but-missing artifact keeps its row (no masking)"
fi

# --- 4. absence alone is not enough ---------------------------------------
if retired_p dir "$LIVE_UNTOUCHED"; then
    bad "an undeclared row whose artifact is still live stays"
else
    ok "an undeclared row whose artifact is still live stays"
fi

# --- 5. a run with no standing retires nothing ----------------------------
# `tracking` recorded nothing this run -- --verify and --status modes mark
# a stage done without walking the install path.
STAGE_ID="tracking"
stage_mark tracking done
if retired_p file "$SANDBOX/absent-elsewhere"; then
    bad "a stage that recorded nothing this run retires nothing"
else
    ok "a stage that recorded nothing this run retires nothing"
fi

# --- 6. retirement is annotation, not deletion ----------------------------
if [[ $(wc -l < "$SMART_NODE_MANIFEST") -eq 5 ]]; then
    ok "no row is deleted -- the manifest keeps its history"
else
    bad "no row is deleted -- the manifest keeps its history"
fi

# --- 7. the reader honours it ---------------------------------------------
# validate.sh walks the manifest with jq; a retired row must not reach the
# check loop, or the retirement changes nothing where it matters.
# HERESTRING, NOT A PIPE. `printf ... | grep -q` is a race under
# `set -o pipefail`: grep -q exits at the first match, printf dies of SIGPIPE,
# and the PIPELINE status becomes 141 -- so a thing that IS present reads as
# missing, at random, under load. tools/capabilities-check.sh carries the same
# note and it cost two refused publications of 1.12.9 before it was believed.
if grep -q 'select(\.retired == null)' <<< "$VALIDATE_SRC"; then
    ok "validate.sh skips retired rows"
else
    bad "validate.sh skips retired rows"
fi

echo "================================"
if [[ $fails -eq 0 ]]; then
    echo "PASS"
    exit 0
fi
echo "$fails failure(s)"
exit 1
