#!/bin/bash
# e2e: creating an agent auto-registers it as a planner actor (type=agent). Then clean up.
set -u
C=http://172.18.0.1:8030
P=http://172.18.0.5:8080
echo "=== create agent ztest-actor ==="
curl -s -H "Remote-User: admin" -H "Content-Type: application/json" -X POST "$C/api/agents" \
  -d '{"slug":"ztest-actor","title":"ZTest Actor","role":"test","model":"sonnet","tenant_id":1,"specialist_body":"You are a test."}' \
  | head -c 200; echo
echo "=== planner actor ztest-actor ==="
A=$(curl -s -H "Remote-User: admin" "$P/api/v1/actors/ztest-actor")
echo "$A"
echo "=== cleanup ==="
curl -s -H "Remote-User: admin" -X DELETE "$C/api/agents/ztest-actor" -o /dev/null -w "agent_delete=%{http_code}\n"
curl -s -H "Remote-User: admin" -X DELETE "$P/api/v1/actors/ztest-actor" -o /dev/null -w "actor_delete=%{http_code}\n"
echo "$A" | grep -q '"type":"agent"' && echo "E2E_CREATE_ACTOR: PASS" || echo "E2E_CREATE_ACTOR: FAIL"
