#!/usr/bin/env bash
# Apply a policy to an agent. Usage: apply-policy.sh <agent-slug> <policy-slug> [include|clear]
set -euo pipefail
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api"
agent="${1:?usage: apply-policy.sh <agent> <policy-slug> [include|clear]}"
slug="${2:?policy-slug required}"
action="${3:-include}"
jq -n --arg a "$action" '{action:$a}' \
| curl --unix-socket "$SOCK" -fsS -X PUT "$API/api/agents/$agent/policies/$slug" -H "Content-Type: application/json" -d @-
echo
