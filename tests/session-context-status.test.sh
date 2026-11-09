#!/usr/bin/env bash
# Source contract for the canonical context read-model and its Studio consumer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="$ROOT/companions/core-api/routes/context_status.py"
LIMITS="$ROOT/companions/core-api/routes/session_context.py"
GW="$ROOT/companions/gateway/context_handoff.py"
ROUTES="$ROOT/companions/gateway/sessions_v2_routes.py"
STUDIO="$ROOT/companions/studio/public/next2/js/app.js"
HTML="$ROOT/companions/studio/public/next2/index.html"
SERVER="$ROOT/companions/studio/src/server.js"

fail() { echo "FAIL: $*" >&2; exit 1; }
has() { grep -Fq -- "$2" "$1" || fail "$1 missing: $2"; }
lacks() { if grep -Fq -- "$2" "$1"; then fail "$1 still contains: $2"; fi; }

# One canonical metric: latest request, three input-side components, never output.
has "$API" "usage->'requests'->-1"
has "$API" 'for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens")'
lacks "$API" 'output_tokens'
has "$API" '_ACTIVE_STATUSES = ("active", "streaming", "idle_released")'
has "$API" '"error": "context_status_unavailable"'
has "$API" '"error": "context_status_forbidden"'

# Product paths must never reach into a provider's private transcript store.
for f in "$API" "$GW" "$ROUTES" "$STUDIO" "$SERVER"; do
  lacks "$f" '.claude/projects'
  lacks "$f" 'jsonl'
done

# Threshold writes and manual clean have distinct, complete audit events.
for field in '"old": old' '"new": new' '"actor": actor' '"reason": payload.reason'; do
  has "$LIMITS" "$field"
done
has "$GW" "session.context_cleared"
has "$GW" "metadata->>'handoff_doc_id' IS NOT NULL"
has "$GW" "OR metadata->>'manual_context_clear' = 'true'"
has "$GW" "DESC NULLS LAST"
has "$ROUTES" '@router.post("/{session_id}/clean")'

# Studio gets the read-model in one bulk call and exposes clean separately from Reload.
has "$SERVER" "app.get('/api/context-status'"
has "$SERVER" "app.post('/api/desks/:id/clean-context'"
# The meter is a badge on the chat and agent rows, and Start clean is on the
# agent page, not above the composer.
lacks "$HTML" 'id="contextMeter"'
has "$STUDIO" "contextBadge(_contextByDesk[d.id])"
has "$STUDIO" "function confirmCleanContext("
has "$(dirname "$STUDIO")/agent-page.js" "confirmCleanContext(cl, cl.dataset.ctxclean)"
has "$STUDIO" "loadContextStatuses()"
has "$STUDIO" "loadContextStatuses(deskId)"
has "$STUDIO" "s.percent >= 80"
has "$STUDIO" "s.percent >= 60"
has "$STUDIO" "Your visible chat history, Mesh memory, and Documents stay intact"
has "$STUDIO" "/clean-context"

node --check "$STUDIO"
node --check "$SERVER"
echo "PASS: context status source contract"
