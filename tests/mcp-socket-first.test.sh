#!/usr/bin/env bash
# an agent's MCP servers must reach core-api over the unix socket.
#
# SMART_NODE_SOURCE_ROOT points this at another tree. Source-level: the
# behavioural half (a real agent getting 403) is in the task, run on the node.
set -uo pipefail

ROOT="${SMART_NODE_SOURCE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PLANNER="$ROOT/companions/tracking/mcp_planner.py"
MESSAGING="$ROOT/companions/brin/mcp_messaging.py"
SECRETS="$ROOT/companions/core-api/mcp_secrets.py"

pass=0; fail=0
ok() { echo "ok - $1"; pass=$((pass+1)); }
no() { echo "not ok - $1"; fail=$((fail+1)); }

# The order is the whole fix: whichever branch comes first is what a real node
# takes, because SN_CORE_API_URL is always set there.
socket_first() {
  local f="$1"
  local s u
  s=$(grep -n 'os.path.exists(CORE_API_SOCKET)' "$f" 2>/dev/null | head -1 | cut -d: -f1)
  u=$(grep -n 'CORE_API_URL.rstrip' "$f" 2>/dev/null | head -1 | cut -d: -f1)
  [ -n "$s" ] && [ -n "$u" ] && [ "$s" -lt "$u" ]
}

for pair in "planner:$PLANNER" "messaging:$MESSAGING" "secrets:$SECRETS"; do
  name="${pair%%:*}"; file="${pair##*:}"
  if [ ! -f "$file" ]; then no "$name MCP exists"; continue; fi
  if socket_first "$file"; then
    ok "$name asks for the socket before the address"
  else
    no "$name asks for the socket before the address"
  fi
done

# And the address stays available for the case it exists for.
for pair in "planner:$PLANNER" "messaging:$MESSAGING"; do
  name="${pair%%:*}"; file="${pair##*:}"
  if grep -q 'elif CORE_API_URL' "$file" 2>/dev/null; then
    ok "$name still works on a node with no socket"
  else
    no "$name still works on a node with no socket"
  fi
done

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
