#!/usr/bin/env bash
# Create a node policy. Usage: create-policy.sh "<title>" "<body>" [category] [severity]
# category: safety|workflow|communication|data-handling   severity: info|warn|block
set -euo pipefail
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api"
title="${1:?usage: create-policy.sh \"<title>\" \"<body>\" [category] [severity]}"
body="${2:?body required}"
category="${3:-communication}"
severity="${4:-warn}"
jq -n --arg t "$title" --arg b "$body" --arg c "$category" --arg s "$severity" \
   '{title:$t, body_md:$b, category:$c, severity:$s}' \
| curl --unix-socket "$SOCK" -fsS -X POST "$API/api/policies" -H "Content-Type: application/json" -d @-
echo
