#!/usr/bin/env bash
# Add a comment to a planner task via the node-local planner REST API.
# Usage: task-comment.sh <task_id> "<content>" [--type TYPE]
# Author comes from the agent OS user on the core-api Unix socket.
set -euo pipefail
# Through core-api, NOT straight at the planner. That door is
# where the agent's `planner` grant is checked and where the call lands in the
# ledger; the planner's own port now refuses an unauthenticated caller anyway.
# The base carries the /api/planner prefix and the paths below are unchanged --
# core-api proxies any subpath through.
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api/api/planner"
[[ -n "${SN_AGENT_SLUG:-}" ]] || { echo "SN_AGENT_SLUG not set" >&2; exit 2; }

task_id="${1:?usage: task-comment.sh <task_id> <content> [--type TYPE]}"
content="${2:?content required}"
shift 2 || true
ctype=""
while [ $# -gt 0 ]; do
  case "$1" in
    --type) ctype="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

jq -n --arg c "$content" --arg ty "$ctype" \
  '{content:$c} + (if $ty != "" then {comment_type:$ty} else {} end)' \
| curl --unix-socket "$SOCK" -fsS -X POST "$API/api/v1/tasks/$task_id/comments" \
       -H "Content-Type: application/json" -d @-
echo
