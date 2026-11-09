#!/usr/bin/env bash
#
# The recap of a person's conversation is not filled with the node's own notices.
#
# WHAT WENT WRONG. The node sends its notices (a task was assigned, you were
# mentioned, a daily digest, a budget warning) to the owner from the agent's own
# mailbox, so they are stored as that agent's mail. On one mailbox 78 of 100
# outbound mails in three days were such notices. The recap shows the newest twelve
# lines per channel, so the email channel was all notices and the owner's real
# thread (a document announced by mail, then "add this to our list") was pushed
# out: the agent answered the owner's mail as if the subject had never come up.
#
# THE PROPERTIES, each run on the real code:
#
#   1. the recap read asks for notices to be left out, and the route can leave
#      them out, by the message's own `source` field
#   2. the list of system sources is a constant and holds the three the node writes
#   3. nothing else is filtered: no condition on the sender or the content, and
#      the search routes are not touched
#
#   tests/recap-skips-system-notices.test.sh                  # working tree, PASS
#   tests/recap-skips-system-notices.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "recap skips system notices (git:$BASELINE)"
else echo "recap skips system notices (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"
read_src companions/brin/services/ledger_service.py > "$D/ledger.py"

if python3 - "$D/messages.py" "$D/ledger.py" <<'PY'
import sys
m = open(sys.argv[1], encoding="utf-8").read()
l = open(sys.argv[2], encoding="utf-8").read()
assert "_SYSTEM_NOTICE_SOURCES" in m, "no list of system sources"
line = m[m.index("_SYSTEM_NOTICE_SOURCES ="):]
line = line[:line.index("\n")]
for s in ("notify", "event-digest", "budget-exhaustion"):
    assert '"%s"' % s in line, "source %s is not listed" % s
i = m.index("def messages_by_contact")
fn = m[i:m.index("class _MessageSearch")]
assert "skip_notices: int" in fn[:400], "by-contact has no skip_notices"
assert "metadata->>'source'" in fn and "NOT IN" in fn, "notices are not left out by their source field"
assert "notice_sql" in fn.split("WHERE (m.sender")[1], "the condition is not applied to the query"
assert '"skip_notices": 1' in l, "the recap read does not ask to leave notices out"
# not touched: the search routes
s = m[m.index('@router.post("/search"'):m.index("def _desk_of_message")]
assert "_SYSTEM_NOTICE_SOURCES" not in s and "skip_notices" not in s, "search was changed too"
PY
then ok "the recap read leaves the node's notices out, by their source field, and nothing else"
else bad "the recap still shows the node's own notices"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
