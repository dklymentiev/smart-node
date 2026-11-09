#!/usr/bin/env bash
#
# The model's answer to a runtime reminder is not delivered to the person.
#
# WHAT WENT WRONG. The CLI injects task-tool reminders into an agent's session and
# tells the model never to mention them. Models answer the reminder anyway, in the
# text of the turn: "Ignoring the reminder, planner is my task system." Every text
# block of a turn is delivered, so the sentence reached the person. Measured on this
# node over three days of web-chat replies: 30 of 421 began or contained it (one
# desk 17 of 112, another 8 of 106, a third 5 of 91), and people quoted it back
# asking what it meant.
#
# THE PROPERTIES, each run on the real helper with sentences copied from replies:
#
#   1. the acknowledgement is removed whether it ends at a full stop, a colon or
#      the end of the line, and with repeated dots
#   2. the answer that follows it is kept, on the same line or the next
#   3. a block that held only the acknowledgement becomes empty (and is skipped)
#   4. a sentence that merely contains the word "ignoring", or a reminder that is
#      the subject of the reply, is left alone
#   5. the gather point uses it for the text blocks and for the final result
#
#   tests/reminder-line-not-delivered.test.sh                  # working tree, PASS
#   tests/reminder-line-not-delivered.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "reminder line not delivered (git:$BASELINE)"
else echo "reminder line not delivered (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/gateway/backends/cli_claude.py > "$D/cli_claude.py"

if python3 - "$D/cli_claude.py" <<'PY'
import ast, re, sys

src = open(sys.argv[1], encoding="utf-8").read()
tree = ast.parse(src)
keep = []
for n in tree.body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "_REMINDER_ACK" for t in n.targets):
        keep.append(n)
    if isinstance(n, ast.FunctionDef) and n.name == "_strip_reminder_ack":
        keep.append(n)
failed = []
if len(keep) != 2:
    print("   - helper and pattern not found")
    sys.exit(1)
mod = ast.Module(body=keep, type_ignores=[])
ast.fix_missing_locations(mod)
ns = {"re": re}
exec(compile(mod, "cli_claude", "exec"), ns)
f = ns["_strip_reminder_ack"]

cases = [
    # (input, expected)
    ("Ignoring the reminder, planner is my task system.\n\nChecking now.", "Checking now."),
    ("Ignoring that reminder... Twenty one of thirty three rejected. Two edge cases left.",
     "Twenty one of thirty three rejected. Two edge cases left."),
    ("Ignoring the built-in TaskCreate reminder: my queue is the node planner MCP.\nAll set.", "All set."),
    ("Ignoring the built-in task reminder, I use planner", ""),
    ("Done.\nIgnoring the reminder, planner is my task system.\nNext step.", "Done.\nNext step."),
    ("Ignoring the typo in the title, I changed the heading.",
     "Ignoring the typo in the title, I changed the heading."),
    ("The reminder email was sent to the owner.", "The reminder email was sent to the owner."),
    ("I am setting a reminder for the morning.", "I am setting a reminder for the morning."),
    ("", ""),
]
for text, want in cases:
    got = f(text)
    if got != want:
        failed.append("%r -> %r, wanted %r" % (text[:50], got[:50], want[:50]))

for x in failed:
    print("   - " + x)
sys.exit(1 if failed else 0)
PY
then ok "acknowledgement removed, the answer and ordinary sentences kept"
else bad "the acknowledgement of a runtime reminder is delivered to the person"; fi

# 5. the gather point uses it
if python3 - "$D/cli_claude.py" <<'PY'
import sys
src = open(sys.argv[1], encoding="utf-8").read()
assert '_t = _strip_reminder_ack(block.get("text", ""))' in src, "text blocks are not filtered"
assert 'final_answer = _strip_reminder_ack(' in src, "final result is not filtered"
assert 'content = _strip_reminder_ack(data.get("result"' in src, "fallback result is not filtered"
PY
then ok "text blocks, final result and fallback result all go through it"
else bad "a delivery path still carries the acknowledgement"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
