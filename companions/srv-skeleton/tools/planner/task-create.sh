#!/usr/bin/env bash
# Create a planner task via the node-local planner REST API.
# Usage: task-create.sh "<title>" [--assignee X] [--description D] [--priority P] [--project-id N] [--requester R] [--status S]
# Attribution comes from the agent's OS user on the core-api Unix socket.
set -euo pipefail
# Through core-api, NOT straight at the planner. That door is
# where the agent's `planner` grant is checked and where the call lands in the
# ledger; the planner's own port now refuses an unauthenticated caller anyway.
# The base carries the /api/planner prefix and the paths below are unchanged --
# core-api proxies any subpath through.
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api/api/planner"
[[ -n "${SN_AGENT_SLUG:-}" ]] || { echo "SN_AGENT_SLUG not set" >&2; exit 2; }

title="${1:?usage: task-create.sh <title> [--assignee X] [--description D] [--priority P] [--project-id N] [--requester R] [--status S]}"
shift || true
assignee="" desc="" priority="medium" project_id="" requester="" tstatus=""
while [ $# -gt 0 ]; do
  case "$1" in
    --assignee)    assignee="$2"; shift 2;;
    --description) desc="$2"; shift 2;;
    --priority)    priority="$2"; shift 2;;
    --project-id)  project_id="$2"; shift 2;;
    --requester)   requester="$2"; shift 2;;
    --status)      tstatus="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

jq -n --arg t "$title" --arg a "$assignee" --arg d "$desc" --arg p "$priority" \
      --arg r "$requester" --arg s "$tstatus" --argjson pid "${project_id:-null}" \
  '{title:$t, priority:$p}
   + (if $a != "" then {assignee:$a}     else {} end)
   + (if $d != "" then {description:$d}   else {} end)
   + (if $r != "" then {requester:$r}     else {} end)
   + (if $s != "" then {status:$s}        else {} end)
   + (if $pid != null then {project_id:$pid} else {} end)' \
| curl --unix-socket "$SOCK" -fsS -X POST "$API/api/v1/tasks" \
       -H "Content-Type: application/json" -d @-
echo
