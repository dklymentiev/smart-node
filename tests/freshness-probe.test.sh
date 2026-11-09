#!/usr/bin/env bash
#
# The installer must be able to tell an install from an update.
#
# WHAT WENT WRONG. install-on.sh decides `_WAS_FRESH` by asking the target
# whether /var/lib/smart-node/stages exists, and writes release evidence -- the
# artifact tools/release.sh trusts to prove a node was built from nothing -- only
# when the answer is "no". The guard was written correctly. It had never fired.
#
# The probe sat ABOVE the line that creates SSH_OPTS, so `"${SSH_OPTS[@]}"`
# expanded to nothing and the probe ran as a bare `ssh` with no key, no BatchMode
# and no known_hosts. It failed on every node, and the failure was read as "the
# directory is not there". Every run, on every machine, called itself a clean
# install. Measured 2026-08-25: two updates of nodes installed six days earlier
# each overwrote the release evidence claiming a from-nothing build.
#
# So this checks the two properties that make the answer trustworthy, and it
# checks them as PROPERTIES OF THE SOURCE, because the real probe needs a node:
#
#   1. ORDER. SSH_OPTS is assigned before the probe uses it. This is the whole
#      original bug and it is invisible at runtime -- an empty array is not an
#      error in bash, it is just fewer arguments.
#   2. UNREACHABLE IS NOT EMPTY. A node that cannot be reached must abort, not
#      be assumed fresh. Without this the first bug is undetectable: "cannot
#      tell" and "nothing there" give the same verdict, and the wrong one is the
#      one that manufactures proof.
#
# Then it demonstrates (3) the behaviour itself against a real sshd-less target,
# showing that a failing probe used to mean "fresh".
#
#   tests/freshness-probe.test.sh                 # working tree, expect PASS
#   tests/freshness-probe.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REL="orchestrator/install-on.sh"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

if [[ -n "$BASELINE" ]]; then
    SRC="$(git -C "$ROOT" show "$BASELINE:$REL" 2>/dev/null)" \
        || { echo "cannot read $REL at $BASELINE" >&2; exit 2; }
    WHAT="$BASELINE"
else
    SRC="$(cat "$ROOT/$REL")"
    WHAT="working tree"
fi

fails=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

echo "$REL ($WHAT)"

# --- 1. order -------------------------------------------------------------
assign_line=$(printf '%s\n' "$SRC" | grep -n '^SSH_OPTS=(' | head -1 | cut -d: -f1)
probe_line=$(printf '%s\n' "$SRC" \
    | grep -nE 'test -[edf] /var/lib/smart-node/stages' | head -1 | cut -d: -f1)

if [[ -z "$assign_line" || -z "$probe_line" ]]; then
    bad "both SSH_OPTS and the stages probe are still present (renamed? then rename this test with them)"
elif (( assign_line < probe_line )); then
    ok "SSH_OPTS is assigned (line $assign_line) before the probe uses it (line $probe_line)"
else
    bad "the probe (line $probe_line) runs BEFORE SSH_OPTS exists (line $assign_line) -- it expands to nothing and the probe always fails"
fi

# --- 2. unreachable is not empty -----------------------------------------
if grep -q 'whether the target is empty' <<< "$SRC"; then
    ok "an unreachable target aborts instead of being assumed empty"
else
    bad "nothing distinguishes 'cannot reach the node' from 'the node is empty'"
fi

# --- 2b. the marker belongs to Layer C, not Layer A -----------------------
# The property that was missing, and it cost a release cut. Fixing the ssh in
# step 1 only revealed this one, which it had been hiding: /var/lib/smart-node
# /stages is created by LAYER A, which on an --all run finishes three seconds
# before the probe. So the moment the probe could actually reach the node, that
# directory was always there, every CLEAN INSTALL called itself an update, and
# the release-evidence gate went from always-satisfied to unsatisfiable.
#
# Both bugs give the same answer every time. THAT is the shape to test for: an
# answer that cannot vary is not an answer. The marker has to be state LAYER C
# writes, because "was this empty before Layer C" is the question being asked.
probe_test=$(grep -oE 'test -[edf] [^"]*smart-node/stages[^"]*' <<< "$SRC" | head -1)
if [[ -z "$probe_test" ]]; then
    bad "no freshness probe found at all (renamed? then rename this check with it)"
elif [[ "$probe_test" =~ stages/?$ ]]; then
    bad "the probe tests the STAGES DIRECTORY ($probe_test) -- Layer A creates it, so on an --all run the answer is always 'update' and no clean install can ever be recognised"
else
    ok "the probe tests a Layer C stage file, not the directory Layer A creates ($probe_test)"
fi

# --- 3. the behaviour, against a target that cannot answer ----------------
# 127.0.0.1:1 refuses instantly. This is what the broken probe did on EVERY
# node: the ssh fails, and the old code turned that into "clean install".
if ssh -o BatchMode=yes -o ConnectTimeout=2 -p 1 nobody@127.0.0.1 \
       "test -d /var/lib/smart-node/stages" >/dev/null 2>&1; then
    bad "the unreachable-target probe unexpectedly succeeded -- this check proves nothing"
else
    ok "a failing probe is indistinguishable from an absent directory by exit code alone (which is why step 2 exists)"
fi

echo
if (( fails )); then
    echo "FAILED ($fails)"
    exit 1
fi
echo "PASS"
