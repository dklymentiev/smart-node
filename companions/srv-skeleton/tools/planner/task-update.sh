#!/usr/bin/env bash
# Update raw fields of a planner task via the node-local planner REST API.
# Usage: task-update.sh <task_id> [--status S] [--assignee X] [--priority P] [--description D]
# Common reassign/restatus path (e.g. pm hands back: task-update.sh <id> --assignee secretary).
# Acts as the agent OS user on the core-api Unix socket; fails closed without it.
set -euo pipefail
# Through core-api, NOT straight at the planner. That door is
# where the agent's `planner` grant is checked and where the call lands in the
# ledger; the planner's own port now refuses an unauthenticated caller anyway.
# The base carries the /api/planner prefix and the paths below are unchanged --
# core-api proxies any subpath through.
SOCK="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
API="http://core-api/api/planner"
[[ -n "${SN_AGENT_SLUG:-}" ]] || { echo "SN_AGENT_SLUG not set" >&2; exit 2; }

task_id="${1:?usage: task-update.sh <task_id> [--status S] [--assignee X] [--priority P] [--description D]}"
shift || true
tstatus="" assignee="" priority="" desc=""
while [ $# -gt 0 ]; do
  case "$1" in
    --status)      tstatus="$2"; shift 2;;
    --assignee)    assignee="$2"; shift 2;;
    --priority)    priority="$2"; shift 2;;
    --description) desc="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

body=$(jq -n --arg s "$tstatus" --arg a "$assignee" --arg p "$priority" --arg d "$desc" \
  '{}
   + (if $s != "" then {status:$s}      else {} end)
   + (if $a != "" then {assignee:$a}    else {} end)
   + (if $p != "" then {priority:$p}    else {} end)
   + (if $d != "" then {description:$d} else {} end)')
if [ "$body" = "{}" ]; then echo "nothing to update (pass at least one field)" >&2; exit 2; fi

printf '%s' "$body" \
| curl --unix-socket "$SOCK" -fsS -X PATCH "$API/api/v1/tasks/$task_id" \
       -H "Content-Type: application/json" -d @-
echo
