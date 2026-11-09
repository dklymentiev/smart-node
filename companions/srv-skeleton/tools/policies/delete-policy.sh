#!/usr/bin/env bash
# Archive a node policy. Usage: delete-policy.sh <slug>   (confirm with the owner first)
set -euo pipefail
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api"
slug="${1:?usage: delete-policy.sh <slug>}"
curl --unix-socket "$SOCK" -fsS -X DELETE "$API/api/policies/$slug" -o /dev/null -w "deleted %{http_code}\n"
