#!/usr/bin/env bash
# voice spends through the same budgets as everything else.
#
# SMART_NODE_SOURCE_ROOT points this at another tree; without it, the tree this
# script lives in. Source-level: it says the path exists and is wired. That a
# real call is refused when the node is capped needs a stand with voice up, and
# that run belongs to the task.
set -uo pipefail

ROOT="${SMART_NODE_SOURCE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
VOICE="$ROOT/companions/voice/backend/main.py"
VOICE_SH="$ROOT/companions/voice.sh"
ROUTES="$ROOT/companions/gateway/external_spend_routes.py"
GW_MAIN="$ROOT/companions/gateway/main.py"

pass=0; fail=0
ok() { echo "ok - $1"; pass=$((pass+1)); }
no() { echo "not ok - $1"; fail=$((fail+1)); }
check() { if grep -q -- "$3" "$2" 2>/dev/null; then ok "$1"; else no "$1"; fi; }

# The door itself.
if [ -f "$ROUTES" ]; then ok "the gateway has a door for spend it did not make"; else no "the gateway has a door for spend it did not make"; fi
check "the gateway serves that door" "$GW_MAIN" "external_spend_router"
check "a caller can ask before it spends" "$ROUTES" "/allowance"
check "a caller can report what it spent" "$ROUTES" "def record_external_spend"
check "reported spend lands on the same budget rows" "$ROUTES" "budget.record_spend"
check "the allowance is the same question chat asks" "$ROUTES" "budget.check_budget"
check "spend is attributed to the agent, not only the node" "$ROUTES" "_agent_id"

# Voice uses it, at both ends of a call.
check "voice asks before a call starts" "$VOICE" "spend_allowed"
check "voice reports when a call ends" "$VOICE" "report_spend"
check "a capped node refuses the call" "$VOICE" "voice.call.refused"
check "the installer tells voice where the gateway is" "$VOICE_SH" "GATEWAY_URL="

# Honesty about the number: voice prices its own audio from byte counts.
check "voice says its figure is an estimate" "$VOICE" '"estimated": True'
check "the estimate stays labelled in the budget" "$ROUTES" "estimated"

# It must not break a call.
if grep -A3 'async def spend_allowed' "$VOICE" 2>/dev/null | grep -q 'Fails OPEN\|fails open'; then
  ok "an unreachable gateway does not drop a person's call"
else
  no "an unreachable gateway does not drop a person's call"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
