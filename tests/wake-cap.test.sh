#!/usr/bin/env bash
# the unattended wake cap. Source-level checks, RED on v1.12.42.
#
# Point SMART_NODE_SOURCE_ROOT at another tree to run the same file against the
# released version; without it, the tree this script lives in.
#
# These say the pieces exist and are wired to each other. What they cannot say
# is that a runaway actually stops, which needs a stand with a runner and two
# agents writing to each other -- that run belongs to the task, not here.
set -uo pipefail

ROOT="${SMART_NODE_SOURCE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
AGENTS="$ROOT/companions/core-api/routes/agents.py"
ALERT="$ROOT/companions/core-api/lib/owner_alert.py"
SCHEMA="$ROOT/bootstrap/B02-sn-core-schema.sql"
CORE_SH="$ROOT/companions/core-api.sh"

pass=0; fail=0
ok()  { echo "ok - $1"; pass=$((pass+1)); }
no()  { echo "not ok - $1"; fail=$((fail+1)); }
has() { grep -q -- "$2" "$1" 2>/dev/null; }

check() {  # check <description> <file> <pattern>
  if has "$2" "$3"; then ok "$1"; else no "$1"; fi
}

# The column the hold lives in, on a fresh node and on an existing one.
check "schema declares the wake hold" "$SCHEMA" "wake_suspended_until timestamp with time zone"
check "an existing node gets the wake hold too" "$SCHEMA" "ADD COLUMN IF NOT EXISTS wake_suspended_until"

# The caps are registry profiles, not process configuration. The old unit
# values are read only while B02 seeds an existing node for the first time.
check "the cap resolves a profile" "$AGENTS" "check_limit(cur, \"wakes_unattended\""
check "the cap uses the shared supervisor" "$AGENTS" "from lib.supervisor import"
check "the installer can carry old values" "$ROOT/bootstrap/B02-persistence.sh" "legacy_wake_hour"

# The decision, and where it is taken.
check "the pending-work answer applies the cap" "$AGENTS" "_apply_wake_cap"
check "the query knows whether a person is behind the work" "$AGENTS" "bool_or(person) AS attended"
check "a hold is recorded in the journal" "$AGENTS" "agent.wake_capped"
check "the hold lifts when the window drains" "$AGENTS" "oldest_hour"

# Order matters, not mere presence: the attended branch has to come BEFORE the
# counting, or a person's message would wait while the node tallies wakes.
_att=$(grep -n 'if r\["attended"\]' "$AGENTS" 2>/dev/null | head -1 | cut -d: -f1)
_cnt=$(grep -n '= _wake_counts(cur' "$AGENTS" 2>/dev/null | head -1 | cut -d: -f1)
if [ -n "$_att" ] && [ -n "$_cnt" ] && [ "$_att" -lt "$_cnt" ]; then
  ok "attended work is let through before any counting"
else
  no "attended work is let through before any counting"
fi

# Being told, and not pretending to have told.
if [ -f "$ALERT" ]; then ok "there is one way to reach the owner"; else no "there is one way to reach the owner"; fi
check "an unreachable owner is an error, not a silence" "$ALERT" "OwnerUnreachable"
check "no invented sender" "$ALERT" "SN_NOTIFY_SENDER"
check "a hold nobody could be told about is still journalled" "$AGENTS" "agent.wake_capped_unreported"
check "the cap uses that one way" "$AGENTS" "owner_alert"

# Found on a stand, not by reading: the alert was written and then BLOCKED at
# the egress gate, whose exemption was keyed to the budget source alone. A
# guard that cannot be reported is not protection, which is the whole point of
# the alert work it sits next to.
OUTBOUND="$ROOT/companions/communication/send_outbound.py"
check "the sender lets this alert out" "$OUTBOUND" "wake-cap"
check "the exemption is a named set, not one literal" "$OUTBOUND" "_SYSTEM_ALERT_SOURCES"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
