#!/bin/bash
# e2e GET /api/agents/with-pending-work correctly returns FREE agents that have
# new inbound work, and excludes busy agents and agents with no pending work.
# No slug is named here: pass the agent to exercise, or the oldest enabled one is used.
set -u
C=http://172.18.0.1:8030; H='-H Remote-User:admin'; ct='-H Content-Type:application/json'
q(){ su - postgres -c "psql -d smartnode -tA -c \"$1\""; }
A="${E2E_AGENT:-$(q "select slug from sn_config.agents where enabled and archived_at is null order by id limit 1")}"
if [ -z "$A" ]; then echo "SKIP: no agent on this node"; exit 0; fi
runtime(){ curl -s $H $ct -X POST "$C/api/agents/$1/runtime" -d "$2" >/dev/null; }
pend(){ curl -s $H "$C/api/agents/with-pending-work" | python3 -c "import sys,json;print(','.join(a['slug'] for a in json.load(sys.stdin)['agents']))"; }
has(){ case ",$1," in *,$A,*) echo yes;; *) echo no;; esac; }
PASS=1; chk(){ if [ "$2" = "$3" ]; then echo "  ok: $1"; else echo "  FAIL: $1 want[$3] got[$2]"; PASS=0; fi; }

# 0. make the agent busy so the dispatcher will NOT grab the probe message while we test
runtime "$A" '{"action":"claim","ref":"e2e-pending","lease_seconds":120}'

# 1. inject a new internal message for the agent (stays new: dispatcher skips a busy target)
RESP=$(curl -s $H $ct -X POST "$C/api/messages/inbound" -d "{\"recipient\":\"$A\",\"content\":\"e2e pending probe\",\"channel_type\":\"internal\",\"sender\":\"e2e\"}")
MID=$(echo "$RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "probe msg id=$MID"

# 2. busy + pending -> must be EXCLUDED
chk "busy agent excluded" "$(has "$(pend)")" "no"

# 3. release -> free + pending -> must be INCLUDED ; then immediately clean up to avoid a wake
runtime "$A" '{"action":"release"}'
INCL="$(has "$(pend)")"
q "UPDATE sn_core.message_state SET status='processed' WHERE message_id=$MID" >/dev/null
chk "free+pending agent included" "$INCL" "yes"

# 4. no pending -> must be EXCLUDED
chk "no-pending agent excluded" "$(has "$(pend)")" "no"

# cleanup safety
runtime "$A" '{"action":"release"}'
q "UPDATE sn_core.message_state SET status='processed' WHERE message_id=$MID" >/dev/null

[ "$PASS" = 1 ] && echo "E2E_PENDING: PASS" || echo "E2E_PENDING: FAIL"
