#!/usr/bin/env bash
# When the Runner gives up on a message (every wake ended with it unread, e.g.
# cut by the turn limit), whoever wrote it is told, once. Until now the Runner
# recorded agent.wake_exhausted for the operator and nothing reached the
# colleague who was waiting (trials step S14). The message stays open: giving
# up on waking is not an answer.
# Supports --baseline REF to show RED on main.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0
ok() { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}
if [[ -n "$BASELINE" ]]; then echo "sender told when wakes run out (git:$BASELINE)"
else echo "sender told when wakes run out (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/brin/runner.py > "$D/runner.py"
read_src companions/core-api/routes/messages.py > "$D/messages.py"

python3 - "$D/runner.py" "$D/messages.py" <<'PY'
import sys
r, m = open(sys.argv[1]).read(), open(sys.argv[2]).read()
bad = 0
def check(name, cond):
    global bad
    print("  %s %s" % ("ok  " if cond else "FAIL", name)); bad += not cond
i = r.find("async def _report_exhausted")
body = r[i:r.find("\nasync def ", i + 10)]
check("the Runner reports the message as unhandled when it gives up",
      i > 0 and "/api/messages/%s/unhandled" in body)
j = m.find('"/{message_id}/unhandled"')
ep = m[j:m.find("\ndef ", j + 300)]
check("core-api says it once (a tag, set only if absent)",
      j > 0 and "NOT (%s = ANY(tags))" in ep and 'return {"ok": True, "told": False}' in ep)
check("an agent sender gets a note in its inbox", "_tell_agent(" in ep and "sender_is_agent" in ep)
check("it is journalled as message.unhandled", '"message.unhandled"' in ep)
check("the message is not closed", "status = 'processed'" not in ep and "status='processed'" not in ep)
sys.exit(1 if bad else 0)
PY
[[ $? -eq 0 ]] || FAILED=$((FAILED + 1))
echo
[[ $FAILED -eq 0 ]] && echo "PASS" || echo "FAIL ($FAILED)"
exit $FAILED
