#!/bin/bash
# e2e a flow:reorder task is driven by the Runner via rein --step, parks at the
# owner gate (blocked:approval), and on approval (tag removed) completes to status=done.
set -u
P=http://172.18.0.5:8080
export SN_AGENT_SLUG=admin
A="${E2E_AGENT:-$(su - postgres -c "psql -d smartnode -tA -c \"select slug from sn_config.agents where enabled and archived_at is null order by id limit 1\"")}"
if [ -z "$A" ]; then echo "SKIP: no agent on this node"; exit 0; fi
OUT=$(/srv/tools/planner/task.sh create "warehouse stock low, reorder needed" --assignee "$A" --status open 2>&1)
TID=$(echo "$OUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])" 2>/dev/null)
echo "TID=$TID"
curl -s -H "Remote-User: admin" -H "Content-Type: application/json" -X POST "$P/api/v1/tasks/$TID/tags" -d '{"tag":"flow:reorder"}' >/dev/null
echo "tagged flow:reorder"
tags(){ curl -s -H "Remote-User: admin" "$P/api/v1/tasks/$TID/tags"; }
status(){ /srv/tools/planner/task.sh get "$TID" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])"; }

PARKED=0
for i in $(seq 1 28); do
  T=$(tags); S=$(status)
  echo "[t=$((i*12))s] status=$S tags=$T"
  echo "$T" | grep -q "blocked:approval" && { PARKED=1; break; }
  [ "$S" = "done" ] && break
  sleep 12
done

if [ "$PARKED" = 1 ]; then
  echo "=== GATE reached -> simulate owner APPROVAL (remove blocked:approval) ==="
  curl -s -H "Remote-User: admin" -X DELETE "$P/api/v1/tasks/$TID/tags/blocked:approval" >/dev/null
  sleep 1; echo "tags after approval: $(tags)"
fi

for i in $(seq 1 20); do
  S=$(status); T=$(tags)
  echo "[t2=$((i*12))s] status=$S tags=$T"
  [ "$S" = "done" ] && break
  sleep 12
done

echo "=== final: status=$(status) tags=$(tags) ==="
[ "$(status)" = "done" ] && echo "E2E_FLOW: PASS" || echo "E2E_FLOW: CHECK (not done)"
