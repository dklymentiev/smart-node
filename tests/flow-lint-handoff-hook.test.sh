#!/usr/bin/env bash
#
# The linter refuses a flow whose hand-offs nothing writes, and knows time budgets.
#
# WHY THIS EXISTS. Until 2026-10-10 the Runner applied a flow's planner.on_block
# map itself. It no longer does: a flow moves its own task, in the post hook of
# each block. A flow written before that declares its hand-offs and carries no
# hook. Nothing fails when it runs: it goes block after block and the task never
# moves. A node that is updated has such flows if anybody on it wrote one, so
# the linter has to name them (rule S12) instead of a person finding a task that
# stands still.
#
# The same linter read one engine schema, 3.3.0, so a flow that set a time
# budget (schema 3.4.0) could not be linted or published at all.
#
# THE PROPERTIES:
#   1. a block that declares an effect and has no post hook is an S12 error
#   2. a flow that declares effects and no on_error is an S12 error
#   3. with the hook on the block and on_error on the flow there is no S12
#   4. a flow that declares no effects is not asked for either
#   5. a 3.4.0 flow may set time_budget; a 3.3.0 flow that sets one is refused
#
#   tests/flow-lint-handoff-hook.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINT="${1:-$HERE/../companions/flows/tools/flow-lint.py}"
export SN_REIN_SCHEMAS="${SN_REIN_SCHEMAS:-$HERE/../companions/rein/schemas}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "flow-lint: a declared hand-off has something that writes it"

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
mkdir -p "$ROOT/teams" "$ROOT/specialists"
printf 'name: team-demo\n' > "$ROOT/teams/team-demo.yaml"

flow() {  # flow <name> <schema version> <on_error line or ""> <post line or ""> <block extra or ""> <planner map: yes|no>
    local d="$ROOT/flows/$1"
    mkdir -p "$d/logic"
    printf 'import sys\n' > "$d/logic/act.py"
    printf 'import sys\n' > "$d/logic/planner_effect.py"
    printf 'import sys\n' > "$d/logic/on_error.py"
    {
        printf 'schema_version: "%s"\nname: %s\nteam: team-demo\nmetadata:\n  sample: true\n' "$2" "$1"
        [[ -n "$3" ]] && printf '%s\n' "$3"
        if [[ "$6" == yes ]]; then
            printf 'planner:\n  on_block:\n    act:\n      _default: {assignee: owner, status: pending_review}\n'
        fi
        printf 'blocks:\n  - name: act\n    depends_on: []\n    max_runs: 2\n'
        [[ -n "$5" ]] && printf '    %s\n' "$5"
        printf '    logic:\n      custom: logic/act.py\n'
        [[ -n "$4" ]] && printf '      %s\n' "$4"
        printf '    routing:\n      _default: _stop\n'
    } > "$d/$1.yaml"
}

lint() { python3 "$LINT" --root "$ROOT" --flow "$1" 2>&1; }
has()  { grep -q "$2" <<< "$1"; }

flow bare      3.3.0 ""                              ""                              "" yes
flow nohandler 3.3.0 ""                              "post: logic/planner_effect.py" "" yes
flow whole     3.3.0 "on_error: logic/on_error.py"   "post: logic/planner_effect.py" "" yes
flow plain     3.3.0 ""                              ""                              "" no
flow budget34  3.4.0 "on_error: logic/on_error.py"   "post: logic/planner_effect.py" "time_budget: 2m" yes
flow budget33  3.3.0 "on_error: logic/on_error.py"   "post: logic/planner_effect.py" "time_budget: 2m" yes

out=$(lint bare)
has "$out" "ERROR S12 bare/act" && ok "a block that declares an effect and has no post hook is refused" \
    || bad "a block with a declared effect and no post hook passed the lint"
has "$out" "no on_error" && ok "and so is a flow that declares effects and no on_error" \
    || bad "a flow with effects and no on_error passed the lint"

out=$(lint nohandler)
{ has "$out" "no on_error" && ! has "$out" "S12 nohandler/act"; } \
    && ok "the hook alone does not stand in for the error handler" \
    || bad "hook without on_error: $(grep S12 <<< "$out" | tr '\n' ' ')"

out=$(lint whole)
! has "$out" "S12" && ok "with the hook and the handler there is no S12" \
    || bad "a whole flow was refused: $(grep -A1 S12 <<< "$out" | tr '\n' ' ')"

out=$(lint plain)
! has "$out" "S12" && ok "a flow that declares no effects is asked for neither" \
    || bad "a flow with no effects was asked for a hook"

out=$(lint budget34)
! has "$out" "ERROR S0" && ok "a 3.4.0 flow may set a time budget" \
    || bad "a 3.4.0 flow with a time budget was refused: $(grep -A1 'ERROR S0' <<< "$out" | tr '\n' ' ')"

out=$(lint budget33)
has "$out" "time_budget" && ok "a 3.3.0 flow that sets one is refused by the schema it declares" \
    || bad "a 3.3.0 flow with a time budget passed"

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; exit 1; fi
