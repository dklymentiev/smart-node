#!/usr/bin/env bash
# E2E smoke: agent-to-agent communication round-trip on a LIVE Smart Node.
#
# Seeds an internal message <SENDER> -> <RECIP>, waits for the recipient to
# reply, then asserts the loop is healthy:
#   1. round-trip      -- the recipient actually replied (recipient -> sender).
#   2. delivery-by-read -- the seed went new -> routed (marked by the recipient's
#                          inbox read), i.e. NOT consumed-on-wake and NOT lost.
#   3. no dropped wakes -- zero FAILED agent sessions during the window.
#   4. no process storm -- live claude procs stayed within the spawn cap.
#
# Node-local: uses psql (postgres peer) + the comm CLI. Exit 0 = pass, 1 = fail.
# Usage: agent-comms.sh [wait_seconds]   (default 150)
set -uo pipefail

DB="smartnode"
Q(){ sudo -u postgres psql -d "$DB" -tAc "$1"; }
COMM="${SN_COMM:-/srv/tools/comm/comm.sh}"
# No agent slug is named here: a node names its own agents. Default to the two
# oldest enabled ones, which exist on any node; override with E2E_SENDER /
# E2E_RECIP to exercise a specific pair.
SENDER="${E2E_SENDER:-$(Q "SELECT slug FROM sn_config.agents WHERE enabled AND archived_at IS NULL ORDER BY id LIMIT 1")}"
RECIP="${E2E_RECIP:-$(Q "SELECT slug FROM sn_config.agents WHERE enabled AND archived_at IS NULL ORDER BY id OFFSET 1 LIMIT 1")}"
WAIT="${1:-150}"

if [ -z "$SENDER" ] || [ -z "$RECIP" ]; then
  echo "[e2e] SKIP: a round-trip needs two enabled agents on this node"
  exit 0
fi

fail=0
chk(){ if [ "$1" = 1 ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }

cap=$(systemctl show smart-node-gateway -p Environment 2>/dev/null | grep -o 'SN_CLI_MAX_PROCS=[0-9]*' | cut -d= -f2); cap="${cap:-4}"
base=$(Q "SELECT COALESCE(max(id),0) FROM sn_core.messages;")
start_ts=$(date -u +'%Y-%m-%d %H:%M:%S')
echo "[e2e] agent-comms: $SENDER -> $RECIP | baseline id=$base | cap=$cap | wait=${WAIT}s"

# SEND AS THE AGENT, NOT AS ROOT. core-api accepts an agent asserting its own
# name only over the unix socket, from that agent's own user; root exporting
# SN_AGENT_SLUG over TCP is refused with a 403, which is the hardening working
# and is separately asserted by permissions.sh. This suite runs as root, so the
# seed used to fail with an empty body and the run stopped at the first line.
# The os_user comes from the agents table, like everything else about an agent.
SENDER_USER=$(Q "SELECT os_user FROM sn_config.agents WHERE slug = '$SENDER' AND archived_at IS NULL")
if [ -n "$SENDER_USER" ] && [ "$(id -u)" = 0 ]; then
    seed_json=$(sudo -n -u "$SENDER_USER" env SN_AGENT_SLUG="$SENDER" "$COMM" send --to "$RECIP" --body "E2E smoke probe ($base): quick check, are you still interested in the quote? Please reply briefly.")
else
    seed_json=$(SN_AGENT_SLUG="$SENDER" "$COMM" send --to "$RECIP" --body "E2E smoke probe ($base): quick check, are you still interested in the quote? Please reply briefly.")
fi
seed_id=$(printf '%s' "$seed_json" | jq -r '.id // empty')
if [ -z "$seed_id" ]; then echo "  FAIL: seed send failed: $seed_json"; exit 1; fi
echo "[e2e] seeded id=$seed_id; waiting up to ${WAIT}s for $RECIP -> $SENDER ..."

reply_id=""; storm=0; max_procs=0
deadline=$(( $(date +%s) + WAIT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  # `pgrep -c` prints its count AND exits 1 when the count is zero, so the old
  # `|| echo 0` produced a two-line value, a zero on each line, and every
  # comparison below
  # died with "integer expression expected" -- on an idle node, which is most of
  # the loop. Same shape as the run-e2e counters and the capabilities-check
  # herestring note.
  np=$(pgrep -c claude 2>/dev/null || true); np=${np:-0}
  [ "$np" -gt "$max_procs" ] && max_procs=$np
  [ "$np" -gt "$((cap+1))" ] && storm=1
  reply_id=$(Q "SELECT id FROM sn_core.messages WHERE id>$seed_id AND sender='$RECIP' AND recipient='$SENDER' ORDER BY id LIMIT 1;")
  [ -n "$reply_id" ] && break
  sleep 5
done

echo "[e2e] assertions:"
rt=0; [ -n "$reply_id" ] && rt=1
chk "$rt" "round-trip: $RECIP replied (msg #${reply_id:-none})"

seed_state=$(Q "SELECT status FROM sn_core.message_state WHERE message_id=$seed_id;")
ds=0; case "$seed_state" in routed|processed) ds=1;; esac
chk "$ds" "delivery-by-read: seed #$seed_id status='${seed_state:-?}' (read by recipient, not consumed-on-wake)"

failed=$(Q "SELECT count(*) FROM sn_core.ai_sessions WHERE created_at >= timestamp '$start_ts' AND status='failed';")
nf=0; [ "${failed:-1}" = 0 ] && nf=1
chk "$nf" "no dropped wakes: failed sessions in window = ${failed:-?}"

ns=0; [ "$storm" = 0 ] && ns=1
chk "$ns" "no storm: peak claude procs=$max_procs (cap=$cap)"

# cleanup: close the probe messages so they do not linger as open work
for id in "$seed_id" $reply_id; do
  [ -n "$id" ] && SN_AGENT_SLUG=admin "$COMM" close --to "$id" >/dev/null 2>&1 || true
done

if [ "$fail" = 0 ]; then echo "[e2e] RESULT: PASS"; else echo "[e2e] RESULT: FAIL"; fi
exit $fail
