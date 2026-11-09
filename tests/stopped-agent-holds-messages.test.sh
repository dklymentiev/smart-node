#!/usr/bin/env bash
# A message to a STOPPED agent (disabled, not archived) waits for it instead of
# being dead-lettered, and the sender hears so once. On the trials stand it was
# routed as no-agent / not-permitted and marked processed: after Enable nobody
# answered it and nobody had been told (steps S11, S32, S64).
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
check() {  # check "<name>" <file> <<'PY' ... PY  (python gets the source as s)
    local name="$1" file="$2"
    if python3 -c "
import sys
s = open(sys.argv[1]).read()
$(cat)
" "$file"; then ok "$name"; else bad "$name"; fi
}

if [[ -n "$BASELINE" ]]; then echo "stopped agent holds its messages (git:$BASELINE)"
else echo "stopped agent holds its messages (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"
read_src companions/brin/services/message_watcher.py > "$D/watcher.py"
read_src companions/communication/dispatch.py > "$D/dispatch.py"

check "core-api: an existing, stopped agent is recipient-stopped, not no-agent" "$D/messages.py" <<'PY'
i = s.find("def resolve_recipient")
body = s[i:i + 4000]
a, b = body.find('"recipient-stopped"'), body.find('"no-agent"')
sys.exit(0 if i > 0 and -1 not in (a, b) and a < b else 1)
PY

check "core-api: a channel bound to a stopped agent is recipient-stopped, not not-permitted" "$D/messages.py" <<'PY'
i = s.find("def resolve_recipient")
body = s[i:i + 4000]
a, b = body.find("_stopped_channel_receiver("), body.find('"not-permitted"')
sys.exit(0 if -1 not in (a, b) and a < b and "NOT a.enabled AND a.archived_at IS NULL" in s else 1)
PY

check "core-api: /held says it once (tag on a 'new' row), records message.held, notes the agent sender" "$D/messages.py" <<'PY'
i = s.find('"/{message_id}/held"')
body = s[i:i + 4000]
sys.exit(0 if i > 0 and "status = 'new' AND NOT (%s = ANY(tags))" in body
          and '"message.held"' in body and "is stopped" in body else 1)
PY

check "brin: recipient-stopped is held (left new, not seen, not processed)" "$D/watcher.py" <<'PY'
i = s.find('reason == "recipient-stopped"')
body = s[i:i + 900]
term = s.find("if reason in self._TERMINAL")
sys.exit(0 if i > 0 and i < term and "_held_notice(" in body and "return" in body
          and "_mark_processed" not in body.split("return")[0]
          and '"recipient-stopped"' not in s[s.find("_TERMINAL = ("):s.find("_TERMINAL = (") + 200] else 1)
PY

check "fast channels: a stopped agent's message waits and the person is told once" "$D/dispatch.py" <<'PY'
i = s.find("def _admit")
body = s[i:]
a, b = body.find("_hold_for_stopped("), body.find("no agent bound to this channel instance")
h = s.find("def _hold_for_stopped")
hb = s[h:s.find(chr(10) + "def ", h + 10)]
sys.exit(0 if -1 not in (a, b, h) and a < b and "NOT (%s = ANY(tags))" in hb
          and "_set_state" not in hb else 1)
PY

read_src companions/communication/common/contacts.py > "$D/contacts.py"
read_src companions/communication/send_outbound.py > "$D/send_outbound.py"

check "the notice does not take the real answer's id (held:, not reply:)" "$D/dispatch.py" <<'PY2'
sys.exit(0 if '("held:%s" if notice else "reply:%s")' in s else 1)
PY2

check "the gate admits a held notice only to the writer, on the stopped agent's grant" "$D/contacts.py" <<'PY2'
i = s.find("def decide_held_notice")
body = s[i:s.find(chr(10) + "def ", i + 10)]
sys.exit(0 if i > 0 and "is not the writer of held message" in body
          and "_HELD_NOTICE_SQL" in body and "NOT a.enabled AND a.archived_at IS NULL" in s else 1)
PY2

check "send-outbound asks the held-notice rule before the ordinary one" "$D/send_outbound.py" <<'PY2'
a, b = s.find("contacts.decide_held_notice("), s.find("contacts.decide(channel_type, sender")
sys.exit(0 if -1 not in (a, b) and a < b else 1)
PY2

echo
[[ $FAILED -eq 0 ]] && echo "PASS" || echo "FAIL ($FAILED)"
exit $FAILED
