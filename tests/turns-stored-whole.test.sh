#!/usr/bin/env bash
#
# A conversation turn is stored whole.
#
# WHAT WENT WRONG. The record of the web chat and of the voice line cut every turn
# at 4000 characters when it was written. The owner's long messages (a pasted plan,
# a quoted conversation) and the agent's long answers lived in the record as their
# first 4000 characters, with no mark that anything was missing, and every later
# recap, search and audit read the cut copy: 67 of the owner's web-chat messages
# and 45 replies had exactly 4000 characters. Email and internal mail, which were
# not cut, held messages of 27,000 characters.
#
# THE PROPERTIES, each run on the real code:
#
#   1. a turn of 20,000 characters is stored as it is
#   2. a pasted megabyte is capped, so one message cannot fill the table
#   3. no turn goes into the record through a hard-coded 4000 cut any more: the web
#      chat, and both voice paths, use the one helper
#
#   tests/turns-stored-whole.test.sh                  # working tree, PASS
#   tests/turns-stored-whole.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "turns stored whole (git:$BASELINE)"
else echo "turns stored whole (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"

if python3 - "$D/messages.py" <<'PY'
import ast, sys
src = open(sys.argv[1], encoding="utf-8").read()
keep = []
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "_STORED_TURN_CHARS" for t in n.targets):
        keep.append(n)
    if isinstance(n, ast.FunctionDef) and n.name == "_stored_turn":
        keep.append(n)
if not any(isinstance(n, ast.FunctionDef) for n in keep):
    print("   - there is no stored-turn helper; turns are still cut by hand")
    sys.exit(1)
mod = ast.Module(body=keep, type_ignores=[])
ast.fix_missing_locations(mod)
ns = {}
exec(compile(mod, "messages", "exec"), ns)
f = ns["_stored_turn"]
failed = []
if f("x" * 20000) != "x" * 20000:
    failed.append("a 20,000 character turn is cut")
if len(f("x" * 1_000_000)) != ns["_STORED_TURN_CHARS"]:
    failed.append("a megabyte is not capped")
if f(None) != "" or f("") != "":
    failed.append("an empty turn is not an empty string")
for x in failed:
    print("   - " + x)
sys.exit(1 if failed else 0)
PY
then ok "a long turn is stored whole, a pasted megabyte is capped"
else bad "turns are cut before they are stored"; fi

if python3 - "$D/messages.py" <<'PY'
import sys
src = open(sys.argv[1], encoding="utf-8").read()
assert "[:4000]" not in src, "a hard-coded 4000 cut is still in the file"
assert src.count("_stored_turn(t.text)") == 3, "the voice and web-chat inserts do not all use the helper"
PY
then ok "the web chat and both voice paths use the one helper, no hand-written cut is left"
else bad "a turn still goes into the record through a hard-coded cut"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
