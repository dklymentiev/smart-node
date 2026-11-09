#!/usr/bin/env bash
#
# When a send is refused, the caller must see WHY, not just the HTTP status.
#
# WHAT WENT WRONG. core-api writes the reason in the 400 body, e.g.
#   "no active agent 'pm (project-manager)' on this node: internal message cannot be
#    delivered. Check the recipient slug against sn_config.agents."
# Two callers threw that text away:
#   - comm.sh used `curl -f`, which exits 22 and prints nothing from the body
#   - voice/backend/main.py built the tool result from the status code only
# On 2026-09-13 three colleagues guessed "transient" while the cause was in the
# body the whole time.
#
# THE PROPERTIES:
#
#   1. comm.sh _api() does NOT use curl -f; body is captured and printed to stderr
#      on >=400 so the calling agent can read the refusal text
#   2. comm.sh _api() captures HTTP status separately (curl -w '%{http_code}')
#      and uses it as the gate for the error path
#   3. voice send_message: on a non-200 response the result string includes the
#      response body text, not just the status code
#   4. voice send_message: the error detail reaches the voice.tool.failed event
#      payload (the event already carries result[:300], so fixing result is enough)
#   5. callers where body loss is acceptable are listed here:
#      brin/runner.py (_release_reply/_send_originate): raise_for_status(); body
#        reachable as e.response.text but not agent-facing (booking/broadcast path)
#      gateway/sessions_v2_routes.py (_send_budget_message): explicitly best-effort;
#        body intentionally swallowed
#      mcp_messaging.py: already correct -- returns {"error":"HTTP N","detail":...}
#
#   tests/send-error-body.test.sh                  # working tree, PASS
#   tests/send-error-body.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "send error body (git:$BASELINE)"
else echo "send error body (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/communication/tools/comm.sh > "$D/comm.sh"
read_src companions/voice/backend/main.py       > "$D/voice.py"

[[ -s "$D/comm.sh" && -s "$D/voice.py" ]] \
    || { bad "sources are readable"; echo; echo "FAIL"; exit 1; }

# ------------------------------------------------------------------ 1
# curl -f suppresses the body entirely on 4xx/5xx.  It must not appear in _api().
if grep -q 'curl -fsS' "$D/comm.sh" || grep -E 'curl -[a-zA-Z]*f[a-zA-Z]*s' "$D/comm.sh" | grep -v '# ' | grep -q .; then
    bad "comm.sh _api() does not use curl -f (body would be suppressed on error)"
else
    ok "comm.sh _api() does not use curl -f"
fi

# ------------------------------------------------------------------ 2
# The fix: body goes to a tmpfile, http_code comes from -w '%{http_code}'.
if grep -q "w '%{http_code}'" "$D/comm.sh" || grep -q 'w "%{http_code}"' "$D/comm.sh"; then
    ok "comm.sh _api() reads HTTP status from curl -w"
else
    bad "comm.sh _api() reads HTTP status from curl -w (needed to gate the error path)"
fi
if grep -q 'cat.*>&2' "$D/comm.sh" || grep -q 'cat.*1>&2' "$D/comm.sh"; then
    ok "comm.sh _api() prints body to stderr on error"
else
    bad "comm.sh _api() prints body to stderr on error (the detail must reach the caller)"
fi

# ------------------------------------------------------------------ 3
# voice send_message result must include the response body, not just the code.
# The buggy form: result = "[ERROR] send failed: HTTP %s" % _r.status_code
# The fixed form includes _r.text (or a variable derived from it).
if grep -q '_r\.text\|_err_body' "$D/voice.py"; then
    # Check that it is in the result line, not just somewhere else in the file.
    if grep 'send failed' "$D/voice.py" | grep -q '_r\.text\|_err_body'; then
        ok "voice send_message result includes response body text"
    else
        bad "voice send_message result includes response body text (_r.text not in result line)"
    fi
else
    bad "voice send_message result includes response body text (_r.text absent from send_message block)"
fi

# ------------------------------------------------------------------ 4
# voice.tool.failed event: result[:300] is already what the event carries (line
# that builds the payload), so fixing result also fixes the event.  Confirm the
# event still carries result in its payload.
if grep 'voice.tool.failed' "$D/voice.py" | grep -q '"result".*result'; then
    ok "voice.tool.failed event payload carries result (body reaches the event)"
elif grep -A5 'voice.tool.failed' "$D/voice.py" | grep -q '"result"'; then
    ok "voice.tool.failed event payload carries result (body reaches the event)"
else
    bad "voice.tool.failed event payload carries result (event must include the tool result)"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
