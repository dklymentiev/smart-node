#!/bin/bash
# e2e for agent_runtime status (claim/release/broken + lease recovery) + /state endpoint.
# No slug is named here: pass E2E_AGENT, else the oldest enabled agent is used.
set -u
C=http://172.18.0.1:8030
A="${E2E_AGENT:-$(su - postgres -c "psql -d smartnode -tA -c \"select slug from sn_config.agents where enabled and archived_at is null order by id limit 1\"")}"
if [ -z "$A" ]; then echo "SKIP: no agent on this node"; exit 0; fi
ct='-H Content-Type:application/json'
H='-H Remote-User:admin'
post(){ curl -s $H $ct -X POST "$C/api/agents/$1/runtime" -d "$2"; }
statusof(){ curl -s $H "$C/api/agents/state" | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(a['status']+('/stale' if a['stale'] else '') for a in d['agents'] if a['slug']=='$1'))"; }
PASS=1; chk(){ if [ "$2" = "$3" ]; then echo "  ok: $1 ($2)"; else echo "  FAIL: $1 expected[$3] got[$2]"; PASS=0; fi; }

echo "== claim free agent =="
R=$(post "$A" '{"action":"claim","ref":"t1","lease_seconds":300}'); echo "  $R"
chk "claim free -> claimed" "$(echo $R|grep -o true || echo false)" "true"
chk "state after claim" "$(statusof "$A")" "busy"

echo "== claim busy agent (must refuse) =="
R=$(post "$A" '{"action":"claim","ref":"t2"}'); echo "  $R"
chk "double claim -> refused" "$(echo $R|grep -o false || echo true)" "false"

echo "== release =="
post "$A" '{"action":"release"}' >/dev/null
chk "state after release" "$(statusof "$A")" "free"

echo "== lease expiry recovery (claim 1s, wait, reclaim) =="
post "$A" '{"action":"claim","ref":"t3","lease_seconds":1}' >/dev/null
sleep 2
R=$(post "$A" '{"action":"claim","ref":"t4"}'); echo "  reclaim after expiry: $R"
chk "expired lease reclaimable" "$(echo $R|grep -o true || echo false)" "true"
post "$A" '{"action":"release"}' >/dev/null

echo "== broken + recovery =="
post "$A" '{"action":"broken","error":"e2e forced"}' >/dev/null
chk "state broken" "$(statusof "$A")" "broken"
R=$(post "$A" '{"action":"claim","ref":"t5"}'); echo "  claim while broken: $R"
chk "broken blocks claim" "$(echo $R|grep -o false || echo true)" "false"
post "$A" '{"action":"release"}' >/dev/null
chk "release clears broken" "$(statusof "$A")" "free"

[ "$PASS" = 1 ] && echo "E2E_STATUS: PASS" || echo "E2E_STATUS: FAIL"
