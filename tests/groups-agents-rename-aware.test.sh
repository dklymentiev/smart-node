#!/usr/bin/env bash
#
# groups-agents-rename-aware.test.sh -- pin the three-branch dispatch of
# bootstrap/groups.sh:apply_agents_group().
#
# The concern: on a node that still carries the legacy `smart-node-agents`
# group, unconditionally creating `grp-agents` here makes a NEW GID and
# blocks the eventual `groupmod -n smart-node-agents grp-agents` from
# succeeding (it refuses when the target name already exists with a different
# GID). apply_agents_group() must therefore dispatch:
#
#   a. smart-node-agents present, grp-agents absent
#      -> DEFER with a named message ("PENDING group rename:..."), no
#         groupadd, no reconcile. Return 0.
#   b. grp-agents present (regardless of smart-node-agents)
#      -> reconcile membership only. Do NOT ensure_group.
#   c. Neither exists
#      -> ensure_group + reconcile.
#
# Method: awk-extract the apply_agents_group function definition from
# bootstrap/groups.sh, source it into a subshell where group_exists,
# ensure_group, reconcile_membership, require_db_or_skip, pg_run, user_exists
# and log are all stubbed. Emit MARK lines from the stubs; grep the output
# for the expected marks per branch. This avoids needing the whole script to
# be source-safe: the function is exercised in isolation.
#
# RED-on-main precondition: on origin/main today the function has
# no state check, so state (a) still emits `MARK ensure_group=grp-agents` and
# `MARK reconcile_membership=grp-agents`, and the PENDING message is absent.
# At least check 1 and check 4 fail on --baseline main.
#
# Usage:
#   tests/groups-agents-rename-aware.test.sh                  # working tree
#   tests/groups-agents-rename-aware.test.sh --baseline main  # git ref, expect fail
#
# Exit: 0 all pass, 1 any fail, 2 setup error.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

GROUPS_SH="$WORK/groups.sh"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" show "$BASELINE:bootstrap/groups.sh" > "$GROUPS_SH" 2>/dev/null \
        || { echo "  [setup] cannot read bootstrap/groups.sh at $BASELINE" >&2; exit 2; }
else
    [[ -r "$ROOT/bootstrap/groups.sh" ]] \
        || { echo "  [setup] missing bootstrap/groups.sh in working tree" >&2; exit 2; }
    cp "$ROOT/bootstrap/groups.sh" "$GROUPS_SH"
fi

WHAT="${BASELINE:-working tree}"
echo "groups-agents-rename-aware ($WHAT)"

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo "  ok   $*"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }

# ---- extract apply_agents_group definition ---------------------------------
#
# The function block starts at "apply_agents_group() {" and ends at the
# matching top-level closing brace on column 1. awk with a brace counter
# handles nested if/for blocks inside.

FN="$(awk '
    /^apply_agents_group\(\)[[:space:]]*{/ {
        in_fn = 1
        depth = 1
        print
        next
    }
    in_fn {
        # Count { and } occurrences on this line.
        line = $0
        o = gsub(/{/, "{", line)
        c = gsub(/}/, "}", line)
        depth += o - c
        print
        if (depth <= 0) {
            in_fn = 0
        }
    }
' "$GROUPS_SH")"

if [[ -z "$FN" ]]; then
    fail "check 0: apply_agents_group() not found in bootstrap/groups.sh"
    echo
    echo "FAIL  $FAIL/$((PASS + FAIL)) check(s) failed  [groups-agents-rename-aware @ $WHAT]"
    exit 1
fi
pass "check 0: apply_agents_group() extracted from bootstrap/groups.sh"

FN_FILE="$WORK/apply_agents_group.sh"
printf '%s\n' "$FN" > "$FN_FILE"

# ---- runner: exercise the function under a given (legacy, agents) state ----

run_branch() {
    local legacy="$1" agents="$2"
    bash <<BASH
set -uo pipefail

# Stubs that stand in for real host calls. Emit MARK lines so the test can
# grep the side effects.
group_exists() {
    case "\$1" in
        smart-node-agents) [[ "$legacy" == 1 ]] ;;
        grp-agents)        [[ "$agents" == 1 ]] ;;
        *) return 1 ;;
    esac
}
ensure_group()         { echo "MARK ensure_group=\$1"; }
reconcile_membership() { echo "MARK reconcile_membership=\$1"; }
require_db_or_skip()   { echo "MARK require_db_or_skip=\$1"; return 0; }
pg_run()               { printf 'agent-alex\nagent-foreman\n'; }
user_exists()          { return 0; }
log()                  { echo "LOG \$*"; }

OWNER_USER="admin"

source "$FN_FILE"
apply_agents_group
BASH
}

# ---- check 1: state (a) DEFER (legacy present, grp-agents absent) ----------

out1="$(run_branch 1 0)"
if grep -q 'PENDING group rename' <<< "$out1" \
   && ! grep -q 'MARK ensure_group=grp-agents' <<< "$out1" \
   && ! grep -q 'MARK reconcile_membership=grp-agents' <<< "$out1" \
   && ! grep -q 'MARK require_db_or_skip' <<< "$out1"; then
    pass "check 1: state (a) smart-node-agents present, grp-agents absent -> DEFER (no ensure, no reconcile, no DB probe)"
else
    fail "check 1: state (a) did not defer cleanly. Output:"
    while IFS= read -r line; do echo "         | $line"; done <<< "$out1"
fi

# ---- check 2: state (b) grp-agents present (with smart-node-agents) --------

out2a="$(run_branch 1 1)"
if grep -q 'MARK reconcile_membership=grp-agents' <<< "$out2a" \
   && ! grep -q 'MARK ensure_group=grp-agents' <<< "$out2a" \
   && ! grep -q 'PENDING group rename' <<< "$out2a"; then
    pass "check 2: state (b) grp-agents present (with legacy) -> reconcile only, no ensure_group"
else
    fail "check 2: state (b) did not reconcile without ensure. Output:"
    while IFS= read -r line; do echo "         | $line"; done <<< "$out2a"
fi

# ---- check 3: state (b) variant, grp-agents alone -------------------------

out2b="$(run_branch 0 1)"
if grep -q 'MARK reconcile_membership=grp-agents' <<< "$out2b" \
   && ! grep -q 'MARK ensure_group=grp-agents' <<< "$out2b" \
   && ! grep -q 'PENDING group rename' <<< "$out2b"; then
    pass "check 3: state (b) grp-agents present alone -> reconcile only, no ensure_group"
else
    fail "check 3: state (b) variant. Output:"
    while IFS= read -r line; do echo "         | $line"; done <<< "$out2b"
fi

# ---- check 4: state (c) neither present ------------------------------------

out3="$(run_branch 0 0)"
if grep -q 'MARK ensure_group=grp-agents' <<< "$out3" \
   && grep -q 'MARK reconcile_membership=grp-agents' <<< "$out3" \
   && ! grep -q 'PENDING group rename' <<< "$out3"; then
    pass "check 4: state (c) neither present -> ensure_group + reconcile"
else
    fail "check 4: state (c) did not create+reconcile. Output:"
    while IFS= read -r line; do echo "         | $line"; done <<< "$out3"
fi

# ---- summary ---------------------------------------------------------------

echo
TOTAL=$((PASS + FAIL))
if [[ $FAIL -eq 0 ]]; then
    echo "PASS  $PASS/$TOTAL check(s) passed  [groups-agents-rename-aware @ $WHAT]"
    exit 0
fi
echo "FAIL  $FAIL/$TOTAL check(s) failed  [groups-agents-rename-aware @ $WHAT]"
exit 1
