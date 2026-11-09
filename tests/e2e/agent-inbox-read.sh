#!/usr/bin/env bash
# E2E on a LIVE node: what an agent's inbox read hands the model.
#
# WHAT WENT WRONG. Two things, one read.
#   1. The contact gate marks a letter from a sender the agent was never granted
#      'blocked' and wakes nobody -- and that was all. Every agent read kept
#      returning the row, body included. On example-node content-agent read six refused
#      letters on a wake for something else, took a verification token out of one
#      and used it, then dismissed two of them.
#   2. The agent tool inbox_read returned the last 20 messages in every state with
#      their metadata. Measured on example-node over 24h it was 38% of all the text
#      agents read, 28k characters a call; a typical call held 20 rows of which 19
#      were already handled. Everything read stays in the session and is re-read
#      by every later request.
#
# THE FIXTURE is the real shape, taken from example-node rows 3406-3415 (a refused
# email: channel email, recipient the agent's mailbox, sender a stranger, status
# blocked, error "agent 'x' was not granted ... as a contact"), plus 20 handled
# internal messages from a colleague and one still waiting. All inserted in one
# transaction with their final state, so the Runner never sees a 'new' row and
# wakes nobody (the waiting one is 'routed', which is still "waiting" to the read).
#
# ASKED AS THE AGENT, over the core-api unix socket from the agent's own OS user,
# which is the only door an agent identity is accepted on -- and through the
# agent's real MCP tool, run as that user.
#
# PROPERTIES
#   1. the tool's default read does not contain the refused letter
#   2. the tool's default read holds the waiting message and none of the 20 handled
#   3. the tool's default read is smaller than the full window, sizes printed
#   4. the waiting message's thread can be read by thread_id
#   5. the full window (Runner, ledger, voice) still has the handled rows ...
#   6. ... and no blocked inbound stranger letter, while the agent's own
#      blocked outbound attempt remains visible for diagnosis
#   7. the owner seat still sees the refused letter (it is evidence to release)
#   8. dismiss and reply on the refused letter answer 404 from the agent's seat
#
# Exit 0 = pass, 1 = fail. Node-local, run as root. Leaves no rows behind.
# Usage: tests/e2e/agent-inbox-read.sh
set -uo pipefail

DB=smartnode
Q(){ sudo -u postgres psql -d "$DB" -tAc "$1"; }
SOCK="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
MCP_PY="${SN_MCP_MESSAGING:-/opt/smart-node/dispatcher/mcp_messaging.py}"
VENV_PY="${SN_DISPATCHER_PY:-/opt/smart-node/dispatcher/venv/bin/python}"

fail=0
chk(){ if [ "$1" = 1 ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }

AGENT="${E2E_AGENT:-$(Q "SELECT slug FROM sn_config.agents WHERE enabled AND archived_at IS NULL AND os_user IS NOT NULL AND os_user <> '' ORDER BY id LIMIT 1")}"
# The colleague only has to be a sender on internal rows; a node with one agent
# (a fresh stand) still gets the test, under a name no agent has.
PEER="${E2E_PEER:-$(Q "SELECT slug FROM sn_config.agents WHERE enabled AND archived_at IS NULL AND slug <> '$AGENT' ORDER BY id LIMIT 1")}"
PEER="${PEER:-e2e-colleague}"
OSU=$(Q "SELECT os_user FROM sn_config.agents WHERE slug = '$AGENT' AND archived_at IS NULL")
if [ -z "$AGENT" ] || [ -z "$OSU" ] || ! id "$OSU" >/dev/null 2>&1; then
  echo "[e2e] SKIP: needs an enabled agent with an existing os_user"; exit 0
fi
# The refused letter goes where real mail goes: the agent's email mailbox when it
# has one. Without one, its slug -- the read matches both.
BOX=$(Q "SELECT ac.instance_ref FROM sn_config.agent_channels ac JOIN sn_config.agents a ON a.id=ac.agent_id JOIN sn_config.channels c ON c.id=ac.channel_id WHERE a.slug='$AGENT' AND c.slug='email' AND ac.enabled AND coalesce(ac.instance_ref,'')<>'' LIMIT 1")
BOX="${BOX:-$AGENT}"
TID=$(Q "SELECT tenant_id FROM sn_config.agents WHERE slug='$AGENT'"); TID="${TID:-1}"
RUN="e2e4659-$(date +%s)-$$"
MARK="BLOCKED-LETTER-$RUN"
AUTHOR_MARK="BLOCKED-AUTHOR-$RUN"
STRANGER="stranger-$RUN@example.invalid"
echo "[e2e] agent-inbox-read: agent=$AGENT (os_user $OSU, box $BOX) peer=$PEER run=$RUN"

cleanup(){
  # message_state is append-only by trigger (message_state_guard), which is right
  # for the product and wrong for a fixture's own rows: remove exactly this run's
  # rows with triggers off, in one transaction, as the postgres superuser.
  Q "BEGIN; SET LOCAL session_replication_role = replica;
     DELETE FROM sn_core.message_state WHERE message_id IN (SELECT id FROM sn_core.messages WHERE external_id LIKE '%$RUN%' OR thread_id LIKE '%$RUN%');
     DELETE FROM sn_core.messages WHERE external_id LIKE '%$RUN%' OR thread_id LIKE '%$RUN%';
     COMMIT;" >/dev/null
}
trap cleanup EXIT

FILLER=$(printf 'Handled earlier, nothing to do here. %.0s' $(seq 1 25))
# The seed-state trigger on sn_core.messages inserts into an unqualified
# message_state; without sn_core first on the path it lands in public's copy and
# fails its foreign key (the same note sits on /reply in messages.py).
sql="BEGIN; SET LOCAL search_path TO sn_core, sn_config, public;"
for i in $(seq 1 20); do
  sql+="INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, recipient, content, content_type, metadata, tenant_id)
        VALUES ('internal', '$AGENT', '<old-$i-$RUN@e2e>', '<old-$i-$RUN@e2e>', 'inbound', '$PEER', '$AGENT', 'OLD-$i-$RUN $FILLER', 'text/plain', json_build_object('source','comm-send'), $TID);
        UPDATE sn_core.message_state SET status='processed', processed_at=now() WHERE message_id=currval(pg_get_serial_sequence('sn_core.messages','id'));"
done
sql+="INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, recipient, content, content_type, metadata, tenant_id)
      VALUES ('internal', '$AGENT', '<wait-root-$RUN@e2e>', '<wait-$RUN@e2e>', 'inbound', '$PEER', '$AGENT', 'EARLIER-IN-THREAD-$RUN', 'text/plain', json_build_object('source','comm-send'), $TID);
      UPDATE sn_core.message_state SET status='processed', processed_at=now() WHERE message_id=currval(pg_get_serial_sequence('sn_core.messages','id'));
      INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, recipient, content, content_type, metadata, tenant_id)
      VALUES ('internal', '$AGENT', '<wait-now-$RUN@e2e>', '<wait-$RUN@e2e>', 'inbound', '$PEER', '$AGENT', 'WAITING-$RUN please confirm', 'text/plain', json_build_object('source','comm-send'), $TID);
      UPDATE sn_core.message_state SET status='routed' WHERE message_id=currval(pg_get_serial_sequence('sn_core.messages','id'));
      INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, recipient, content, content_type, metadata, tenant_id)
      VALUES ('email', '$BOX', '$BOX|<blk-$RUN@example.invalid>', 'blk-$RUN@example.invalid', 'inbound', '$STRANGER', '$BOX', 'Your code is 424242. $MARK', 'text/plain',
              json_build_object('subject','Confirm your email','from_name','stranger','history',false), $TID);
      UPDATE sn_core.message_state SET status='blocked', error='agent ''$AGENT'' was not granted $STRANGER as a contact on email -- recorded, not answered'
        WHERE message_id=currval(pg_get_serial_sequence('sn_core.messages','id'));
      INSERT INTO sn_core.messages (channel_type, channel_id, external_id, thread_id, direction, sender, recipient, content, content_type, metadata, tenant_id)
      VALUES ('internal', '$AGENT', '<blk-author-$RUN@e2e>', 'blk-author-$RUN@e2e', 'outbound', '$AGENT', '$STRANGER', 'Blocked row authored by agent. $AUTHOR_MARK', 'text/plain', json_build_object('source','comm-send'), $TID);
      UPDATE sn_core.message_state SET status='blocked', error='fixture blocked author row'
        WHERE message_id=currval(pg_get_serial_sequence('sn_core.messages','id'));
      COMMIT;"
Q "$sql" >/dev/null || { echo "  FAIL: could not seed the fixture"; exit 1; }
BLK_ID=$(Q "SELECT id FROM sn_core.messages WHERE thread_id='blk-$RUN@example.invalid'")
AUTHOR_ID=$(Q "SELECT id FROM sn_core.messages WHERE thread_id='blk-author-$RUN@e2e'")
WAIT_ID=$(Q "SELECT id FROM sn_core.messages WHERE external_id='<wait-now-$RUN@e2e>'")
echo "[e2e] seeded 23 rows, waiting #$WAIT_ID, refused #$BLK_ID"

# As the agent: the socket, from its own user.
api(){ sudo -n -u "$OSU" curl -s --unix-socket "$SOCK" -H "Remote-User: $AGENT" -H 'Content-Type: application/json' "$@"; }
code(){ sudo -n -u "$OSU" curl -s -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" -H "Remote-User: $AGENT" -H 'Content-Type: application/json' "$@"; }
tool(){ sudo -n -u "$OSU" env SN_AGENT_SLUG="$AGENT" SN_CORE_API_SOCKET="$SOCK" "$VENV_PY" -c "
import asyncio, sys, importlib.util
spec = importlib.util.spec_from_file_location('mcp_messaging', '$MCP_PY')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
f = getattr(m.inbox_read, 'fn', m.inbox_read)
print(asyncio.run(f(*sys.argv[1:])))" "$@"; }

DEFAULT=$(tool)
FULL=$(api "http://localhost/api/messages/inbox?limit=100")
echo "[e2e] tool default read: ${#DEFAULT} chars; full window: ${#FULL} chars"

echo "[e2e] assertions:"
r=0; [ -n "$DEFAULT" ] && ! grep -q "$MARK" <<<"$DEFAULT" && ! grep -q "$AUTHOR_MARK" <<<"$DEFAULT" && r=1
chk "$r" "1. the tool's default read does not contain either refused letter (#$BLK_ID or agent-authored)"
old=$(grep -o "OLD-[0-9]*-$RUN" <<<"$DEFAULT" | sort -u | wc -l)
r=0; grep -q "WAITING-$RUN" <<<"$DEFAULT" && [ "$old" -eq 0 ] && r=1
chk "$r" "2. the tool's default read holds the waiting message and none of the 20 handled (found $old handled)"
r=0; [ "${#DEFAULT}" -gt 0 ] && [ "${#DEFAULT}" -lt "${#FULL}" ] && r=1
chk "$r" "3. default read ${#DEFAULT} chars < full window ${#FULL} chars"
THREAD=$(tool "<wait-$RUN@e2e>")
r=0; grep -q "EARLIER-IN-THREAD-$RUN" <<<"$THREAD" && grep -q "WAITING-$RUN" <<<"$THREAD" && r=1
chk "$r" "4. the waiting message's thread is readable by thread_id (${#THREAD} chars)"
fold=$(grep -o "OLD-[0-9]*-$RUN" <<<"$FULL" | sort -u | wc -l)
r=0; [ "$fold" -eq 20 ] && r=1
chk "$r" "5. the full window still has the 20 handled rows (found $fold)"
r=0; [ -n "$FULL" ] && ! grep -q "$MARK" <<<"$FULL" && grep -q "$AUTHOR_MARK" <<<"$FULL" && r=1
chk "$r" "6. full window hides blocked inbound stranger but shows agent's own blocked outbound"
# Checked BEFORE dismiss and reply: on the baseline a reply marks the refused
# letter processed (and queues an answer to the stranger), so afterwards it is no
# longer 'blocked' and the owner's filter cannot find it.
OWNER=$(Q "SELECT email FROM sn_config.users WHERE role='super-admin' AND active ORDER BY id LIMIT 1")
OWN=$(curl -s -H "Remote-User: $OWNER" "http://127.0.0.1:8030/api/messages?status=blocked&limit=1000")
r=0; grep -q "$MARK" <<<"$OWN" && r=1
chk "$r" "7. the owner seat ($OWNER) still sees the refused letter"

dc=$(code -X POST "http://localhost/api/messages/dismiss" -d "{\"message_id\": $BLK_ID}")
rc=$(code -X POST "http://localhost/api/messages/reply" -d "{\"to_message_id\": $BLK_ID, \"body\": \"e2e must not be sent\"}")
od=$(code -X POST "http://localhost/api/messages/dismiss" -d "{\"message_id\": $AUTHOR_ID}")
or=$(code -X POST "http://localhost/api/messages/reply" -d "{\"to_message_id\": $AUTHOR_ID, \"body\": \"e2e must not be sent\"}")
r=0; [ "$dc" = 404 ] && [ "$rc" = 404 ] && [ "$od" = 404 ] && [ "$or" = 404 ] && r=1
chk "$r" "8. dismiss/reply on blocked inbound ($dc/$rc) and own outbound ($od/$or) are 404"
[ "$fail" = 0 ] && echo "[e2e] PASS" || echo "[e2e] FAIL"
exit "$fail"
