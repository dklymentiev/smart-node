#!/bin/bash
# e2e regression: a vague delegation email must route FRONT -> SPEC (draft) -> FRONT replies to
# the requester, with NO routing_denied. The subject of the request is NOT named (no rigging).
#
# No agent slug is written into this test: a node names its own agents. Give it the pair to
# exercise, and the mailbox the request arrives on:
#   E2E_FRONT=<slug> E2E_SPEC=<slug> E2E_INBOX=<address> ./e2e_reorder.sh
set -u
q(){ su - postgres -c "psql -d smartnode -tA -c \"$1\""; }

FRONT="${E2E_FRONT:-}"; SPEC="${E2E_SPEC:-}"; INBOX="${E2E_INBOX:-}"
if [ -z "$FRONT" ] || [ -z "$SPEC" ] || [ -z "$INBOX" ]; then
  echo "SKIP: set E2E_FRONT, E2E_SPEC and E2E_INBOX to the agents and mailbox to exercise"
  exit 0
fi

# pre-clean: the test reply to the fake owner@ address bounces back into the front agent's inbox;
# clear stale/bounce messages for both agents so each run starts clean (repeatable).
q "update sn_core.message_state set status='processed' where status<>'processed' and message_id in (select id from sn_core.messages where recipient like '$FRONT%' or recipient in ('$FRONT','$SPEC'))" >/dev/null

BASE=$(q "select max(id) from sn_core.events")
echo "baseline events=$BASE"
RESP=$(curl -s -X POST http://172.18.0.1:8030/api/messages/inbound \
  -H "Content-Type: application/json" -H "Remote-User: admin" \
  -d "{\"recipient\":\"$INBOX\",\"content\":\"Hi, we are running low on the usual again, please get that sorted for whatever is short.\",\"channel_type\":\"email\",\"sender\":\"owner@example.com\",\"metadata\":{\"subject\":\"e2e running low\"}}")
MID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "injected msg id=$MID"

for i in $(seq 1 18); do
  st=$(q "select status from sn_core.message_state where message_id=$MID")
  echo "[t=$((i*15))s] msg$MID=$st"
  [ "$st" = processed ] && break
  [ "$st" = failed ] && break
  sleep 15
done

echo "=== events since base ==="
q "select id||'|'||actor||'|'||event_type||'|'||coalesce(entity_id,'') from sn_core.events where id>$BASE and actor not like 'cron%' order by id"

WOKEN=$(q "select count(*) from sn_core.events where id>$BASE and event_type='agent.woken' and entity_id='$FRONT'")
DRAFT=$(q "select count(*) from sn_core.events where id>$BASE and event_type='message.sent' and actor='agent:$SPEC'")
REPLY=$(q "select count(*) from sn_core.events where id>$BASE and event_type='message.reply'")
DENIED=$(q "select count(*) from sn_core.events where id>$BASE and event_type='message.routing_denied' and payload->>'recipient'='owner'")
echo "woken_$FRONT=$WOKEN draft_$SPEC=$DRAFT reply=$REPLY denied=$DENIED"

if [ "${WOKEN:-0}" -ge 1 ] && [ "${DRAFT:-0}" -ge 1 ] && [ "${REPLY:-0}" -ge 1 ] && [ "${DENIED:-0}" -eq 0 ]; then
  echo "E2E_REORDER: PASS"; exit 0
else
  echo "E2E_REORDER: FAIL"; exit 1
fi
