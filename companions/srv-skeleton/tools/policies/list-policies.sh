#!/usr/bin/env bash
# List node policies. Usage: list-policies.sh
set -euo pipefail
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api"
curl --unix-socket "$SOCK" -fsS "$API/api/policies" \
| jq -r '.policies[] | "\(.slug)\t[\(.severity)] \(.category)\t\(.title)"'
