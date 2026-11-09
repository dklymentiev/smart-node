#!/usr/bin/env bash
# List planner tasks via the node-local planner REST API.
# Usage: task-list.sh [--assignee X] [--status S] [--priority P] [--limit N]
# Typical agent use: task-list.sh --assignee "$SN_AGENT_SLUG" --status open
set -euo pipefail
# Through core-api, NOT straight at the planner. That door is
# where the agent's `planner` grant is checked and where the call lands in the
# ledger; the planner's own port now refuses an unauthenticated caller anyway.
# The base carries the /api/planner prefix and the paths below are unchanged --
# core-api proxies any subpath through.
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api/api/planner"
[[ -n "${SN_AGENT_SLUG:-}" ]] || { echo "SN_AGENT_SLUG not set" >&2; exit 2; }

q=""
while [ $# -gt 0 ]; do
  case "$1" in
    --assignee) q="${q}&assignee=$2"; shift 2;;
    --status)   q="${q}&status_filter=$2"; shift 2;;
    --priority) q="${q}&priority=$2"; shift 2;;
    --limit)    q="${q}&limit=$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
q="${q#&}"

curl --unix-socket "$SOCK" -fsS "$API/api/v1/tasks${q:+?$q}" \
| jq -r '.[] | "#\(.id)\t[\(.status)\(if .wait_reason then ":" + .wait_reason elif .close_outcome then ":" + .close_outcome else "" end)/\(.priority)]\t\(.assignee // "-")\t\(.title)"'
