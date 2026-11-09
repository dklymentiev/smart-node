#!/usr/bin/env bash
# Source contract for clean opaque-session handoff.
#
# This file guards the few decisions that are invisible in a unit test because
# they live in schema, seed and install scripts.  The BEHAVIOUR of the handoff
# (order of write/close, the queue, backoff, injection) is covered by
# companions/gateway/tests/test_context_handoff.py, which runs the real code.
# A grep for a line the author just wrote proves nothing; these greps assert
# things that must not silently disappear.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCHEMA="$ROOT/bootstrap/B02-sn-core-schema.sql"
SEED="$ROOT/bootstrap/B02-sn-core-seed.sql"
PERSIST="$ROOT/bootstrap/B02-persistence.sh"
GWUNIT="$ROOT/companions/gateway.sh"
GW="$ROOT/companions/gateway/context_handoff.py"
ROUTES="$ROOT/companions/gateway/sessions_v2_routes.py"
BRIN="$ROOT/companions/brin/core/executor.py"
API="$ROOT/companions/core-api/routes/session_context.py"
ENV_API="$ROOT/companions/core-api/routes/gateway_secrets.py"

fail() { echo "FAIL: $*" >&2; exit 1; }
has() { grep -Fq -- "$2" "$1" || fail "$1 missing: $2"; }
lacks() { if grep -Fq -- "$2" "$1"; then fail "$1 still contains: $2"; fi; }
line_of() { grep -n -F -- "$2" "$1" | head -1 | cut -d: -f1; }

# -- the setting exists on fresh installs AND on update runs -------------------
has "$SCHEMA" "CREATE TABLE IF NOT EXISTS sn_config.session_context_limits"
has "$SCHEMA" "UNIQUE NULLS NOT DISTINCT (scope_type, scope_id)"
has "$SEED" "VALUES ('global', NULL, 120000, true)"
has "$SEED" '"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"'
has "$PERSIST" "session context global limit present"
has "$PERSIST" "claude-cli auto-compact safety window present"
# The gateway unit must be able to find memory, or no handoff can ever be stored.
has "$GWUNIT" "EnvironmentFile=-/etc/smart-node/mesh.env"

# -- the reset is ordered, audited and reversible ------------------------------
# Mesh first, terminal state second, process cleanup last: any other order can
# retire a session whose context was never written down.
write_line=$(line_of "$GW" 'guid = await _write_handoff')
final_line=$(line_of "$GW" 'event_id = _finalize_reset')
close_line=$(line_of "$GW" 'await backend.close')
[ -n "$write_line" ] && [ -n "$final_line" ] && [ -n "$close_line" ] \
    || fail "the reset steps are no longer identifiable in $GW"
[ "$write_line" -lt "$final_line" ] || fail "session is finalized before the Mesh write"
[ "$final_line" -lt "$close_line" ] || fail "backend is closed before the reset is durable"
has "$GW" '"session.handoff_failed"'
has "$GW" '"session.handoff_injection_failed"'
# The CLI backend owns the deadline because it also kills the child process.
# Cancelling send() here would leave unread output queued for the next turn.
has "$GW" 'handoff_response = await backend.send('
lacks "$GW" 'CONTEXT_HANDOFF_GENERATION_TIMEOUT'

# -- a person's message is never dropped for a lifecycle reason ----------------
# The claim queues overlapping turns; it must not refuse them. A refusal here
# ends as a failed inbound and no reply at all on brin's side.
lacks "$ROUTES" "another session turn or context handoff is in progress"
has "$ROUTES" "async with _turn_lock(session_id)"
has "$ROUTES" '"error": "session_reset"'
has "$BRIN" "class SessionResetError"
has "$BRIN" 'detail.get("error") == "session_reset"'

# -- the answer is not held behind the handoff ---------------------------------
# The future is resolved inside the turn task, before maintenance runs.
answer_line=$(line_of "$ROUTES" 'result_future.set_result(result)')
maint_line=$(line_of "$ROUTES" 'context_handoff.maybe_handoff')
[ -n "$answer_line" ] && [ -n "$maint_line" ] \
    || fail "the answer/maintenance boundary is no longer identifiable in $ROUTES"
[ "$answer_line" -lt "$maint_line" ] || fail "the handoff runs before the answer is published"

# -- memory is addressed the way Mesh stores it --------------------------------
has "$GW" 'def _lc('
has "$GW" '_lc(identity['"'"'conversation'"'"'])'
has "$GW" "def _predecessor_handoff_guid"

# -- configuration stays owner-only and audited --------------------------------
has "$API" '@router.put("/global")'
has "$API" '@router.put("/agents/{agent_slug}")'
has "$API" 'event_type="config.changed"'
has "$API" 'require_session(principal)'
has "$ENV_API" '@router.patch("/cli/{slug}/env-vars"'
has "$ENV_API" 'return require_session(_require_owner(request))'
has "$ENV_API" '"changed_keys": changed, "removed_keys": removed'

echo "PASS: session context handoff source contract"
