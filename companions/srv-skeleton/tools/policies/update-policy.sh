#!/usr/bin/env bash
# Edit a node policy. Usage: update-policy.sh <slug> [--title X] [--body X] [--category X] [--severity X] [--enabled true|false]
set -euo pipefail
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api"
slug="${1:?usage: update-policy.sh <slug> [--title X] [--body X] [--category X] [--severity X] [--enabled true|false]}"; shift
args=(); while [ $# -gt 0 ]; do
  case "$1" in
    --title) args+=(--arg title "$2"); shift 2;;
    --body) args+=(--arg body_md "$2"); shift 2;;
    --category) args+=(--arg category "$2"); shift 2;;
    --severity) args+=(--arg severity "$2"); shift 2;;
    --enabled) args+=(--argjson enabled "$2"); shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
jq -n "${args[@]}" '$ARGS.named' \
| curl --unix-socket "$SOCK" -fsS -X PATCH "$API/api/policies/$slug" -H "Content-Type: application/json" -d @-
echo
