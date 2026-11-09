#!/usr/bin/env bash
#
# The owner can name the doer when approving a task that has none.
#
# WHAT WENT WRONG. The planner now refuses to start work on a task with nobody to do it, so
# approving an agent's task that named no doer is refused. The Studio approval gate only sent
# a note, so the owner had no way to name one: the refusal would have been a dead end.
#
# THE PROPERTIES, each run on the real code:
#   1. a task with no assignee shows a "who does it" choice in the approval gate, with the
#      filer's proposal already chosen; a task that has a doer does not; the choice takes the
#      theme's tokens, so it follows light and dark (a bare select is white on the dark theme)
#   2. the choice is sent with the decision as `assignee`, and only when one was made
#
#   tests/studio-approve-names-a-doer.test.sh                  # working tree, PASS
#   tests/studio-approve-names-a-doer.test.sh --baseline REF   # git:REF, expect FAIL
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}
if [[ -n "$BASELINE" ]]; then echo "Studio approval names a doer (git:$BASELINE)"
else echo "Studio approval names a doer (working tree)"; fi
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/studio/public/next2/js/app.js > "$D/app.js"
read_src companions/studio/public/next2/css/app.css > "$D/app.css"

if python3 - "$D/app.js" "$D/app.css" <<'PY'
import sys
s = open(sys.argv[1], encoding="utf-8").read()
assert 'id="tdDoer"' in s, "no doer choice in the approval gate"
assert "_statusGate && !t.assignee" in s, "the choice is not limited to tasks with no doer"
assert "t.proposed_assignee" in s and "selected" in s, "the filer's proposal is not chosen"
assert 'id="tdDoer" class="td__doer"' in s and 'id="tdDoer" style' not in s, "the choice carries its own inline style"
css = open(sys.argv[2], encoding="utf-8").read()
rule = css[css.index(".td__doer {"):][:400]
assert "var(--sn-surface-2)" in rule and "var(--sn-ink)" in rule and "var(--sn-rule)" in rule, "the choice does not use the theme's tokens"
assert "#fff" not in rule.lower() and "white" not in rule.lower(), "the choice hard-codes a light colour"
PY
then ok "a task with no doer shows the choice, with the filer's proposal chosen"
else bad "the approval gate cannot name a doer"; fi

if python3 - "$D/app.js" <<'PY'
import sys
s = open(sys.argv[1], encoding="utf-8").read()
i = s.index("function decisionBody")
fn = s[i:s.index("async function gateDecide")]
assert "body.assignee = doer.value" in fn and "if (doer && doer.value)" in fn, "the choice is not sent, or is sent empty"
assert "JSON.stringify(decisionBody(note, host))" in s, "the decision does not carry the choice"
PY
then ok "the choice goes with the decision as assignee, only when made"
else bad "the choice does not reach the planner"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
