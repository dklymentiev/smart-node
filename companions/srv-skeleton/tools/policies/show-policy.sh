#!/usr/bin/env bash
# Show one policy with its body. Usage: show-policy.sh <slug>
set -euo pipefail
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api"
slug="${1:?usage: show-policy.sh <slug>}"
curl --unix-socket "$SOCK" -fsS "$API/api/policies" \
| jq -r --arg s "$slug" '.policies[] | select(.slug==$s) | "\(.slug) [\(.severity)] \(.category)\n\(.title)\n\n\(.body_md)"'
