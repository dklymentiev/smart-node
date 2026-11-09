#!/usr/bin/env bash
# E2E: connecting a mailbox does not hand its backlog to an agent.
#
# The unit tests in companions/communication/tests cover the ingest decision
# with IMAP faked. This covers the two halves that only exist on a real node:
#
#   A. the watermark's LIFECYCLE through core-api -- taken on binding, kept
#      when the same mailbox is re-saved, renewed when it changes
#   B. that a row in state 'history' is invisible to BOTH consumers, asserted
#      by running their own queries rather than by reading the code
#
# CONSTRUCT THE FAILING STATE. Every negative here is paired with a positive in
# the same run: a check that a history row is not offered for work would pass
# just as well against an empty table or a typo'd query, so each one first
# proves the query DOES return a comparable live row. The runbook drills found
# three checks that passed whether or not the thing worked; this is the answer
# to that.
#
# Node-local. Read-only apart from one throwaway agent + two messages, all of
# which are removed at the end. Exit 0 = pass, 1 = fail.
set -uo pipefail

# SN_CORE_API is read from the environment and nothing on a node supplies it:
# run-all.sh sources none. Unset, every call here went to a URL with no host and
# came back 000, so a healthy node failed. Derive it from the integrations
# catalog, where core-api registers itself -- an address written in here would
# rot the first time the docker network is renumbered.
API="${SN_CORE_API:-}"
if [ -z "$API" ]; then
    _pgc="${SMART_NODE_PG_CLUSTER:-}"
    if [ -z "$_pgc" ] && [ -r /etc/smart-node/persistence.env ]; then
        # shellcheck source=/dev/null
        . /etc/smart-node/persistence.env
        _pgc="${SMART_NODE_PG_CLUSTER:-}"
    fi
    API=$(runuser -u postgres -- psql ${_pgc:+--cluster "$_pgc"} \
        -d "${SMART_NODE_PG_CORE_DB:-smartnode}" -At \
        -c "SELECT endpoint FROM sn_config.integrations WHERE slug = 'core-api' AND archived_at IS NULL" \
        2>/dev/null)
fi
PSQL="${SN_PSQL:-psql -d smartnode}"
SLUG="e2e-watermark-$$"
BOX_A="e2e-a-$$@example.invalid"
BOX_B="e2e-b-$$@example.invalid"

fail=0
chk(){ if [ "$1" = 1 ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }
q(){ sudo -n -u postgres $PSQL -tAc "$1" 2>/dev/null; }
# The seed-state trigger on sn_core.messages INSERTs into `message_state`
# unqualified, so a session without sn_core on its search_path writes to
# public.message_state and the FK rejects it. core-api sets the same path
# for the same reason.
qw(){ sudo -n -u postgres $PSQL -tAc "SET search_path TO sn_core, sn_config, public; $1" 2>&1; }
# The owner's calls go through the unix socket as root. This test used to name
# the owner in a Remote-User header over TCP; from 1.12.49 a write that way is
# refused by design, the agent was never created and every later step fell over.
API="http://core-api"
api(){ sudo -n curl -sS -m 15 --unix-socket "${CORE_API_UDS:-/run/smart-node/api/core-api.sock}" "$@"; }

echo "[e2e] history watermark | api=$API | agent=$SLUG"

# --- 0. the column exists at all -------------------------------------------
have=$(q "SELECT count(*) FROM information_schema.columns
           WHERE table_schema='sn_config' AND table_name='agent_channels'
             AND column_name='history_before'")
[ "$have" = "1" ]; chk $((1-$?)) "agent_channels.history_before exists"
if [ "$have" != "1" ]; then
    echo "  (agent_channels.history_before is absent -- re-run B02 to apply the additive schema)"
    exit 1
fi

notnull=$(q "SELECT is_nullable FROM information_schema.columns
              WHERE table_schema='sn_config' AND table_name='agent_channels'
                AND column_name='history_before'")
[ "$notnull" = "NO" ]; chk $((1-$?)) "history_before is NOT NULL (no binding without a watermark)"

# --- 1. every existing binding already has one -----------------------------
# A node that has been running has bindings from before this change; the
# backfill must have reached all of them, or the guard is off for exactly the
# mailboxes that have been collecting mail the longest.
orphans=$(q "SELECT count(*) FROM sn_config.agent_channels WHERE history_before IS NULL")
[ "${orphans:-1}" = "0" ]; chk $((1-$?)) "no binding is left without a watermark (found: ${orphans:-?})"

# --- 2. the queue accepts 'history' ----------------------------------------
# On a node whose CHECK was never re-asserted this write fails, and the whole
# guard silently degrades to an exception in the poller.
for t in public.message_state sn_core.message_state; do
    exists=$(q "SELECT to_regclass('$t') IS NOT NULL")
    if [ "$exists" != "t" ]; then echo "  SKIP: $t not on this node"; continue; fi
    ok=$(q "SELECT pg_get_constraintdef(oid) LIKE '%history%'
              FROM pg_constraint
             WHERE conrelid = '$t'::regclass AND conname='message_state_status_chk'")
    [ "$ok" = "t" ]; chk $((1-$?)) "$t CHECK admits 'history'"
done

# --- 3. binding takes a watermark ------------------------------------------
api -X POST "$API/api/agents" -H 'Content-Type: application/json' \
    -d "{\"slug\":\"$SLUG\",\"title\":\"watermark e2e\"}" >/dev/null 2>&1
# Prove the agent exists before testing anything that needs it. Without
# this every check below fails for one reason and reports as nine.
have_agent=$(api -o /dev/null -w '%{http_code}' "$API/api/agents/$SLUG")
[ "$have_agent" = "200" ]; chk $((1-$?)) "throwaway agent created (HTTP $have_agent)"
if [ "$have_agent" != "200" ]; then
    echo "  (cannot bind a channel without an agent -- skipping the rest)"
    echo; echo "[e2e] history watermark: FAILURES"; exit 1
fi

before=$(date -u +%s)
r1=$(api -X PUT "$API/api/agents/$SLUG/channels/email" -H 'Content-Type: application/json' \
        -d "{\"enabled\":true,\"instance_ref\":\"$BOX_A\"}")
w1=$(printf '%s' "$r1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("history_before",""))' 2>/dev/null)
[ -n "$w1" ]; chk $((1-$?)) "binding returns its watermark ($w1)"

note=$(printf '%s' "$r1" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("note",""))' 2>/dev/null)
case "$note" in
    *"only what arrives"*|*"answering only what arrives"*) chk 1 "binding says what it will do with the backlog" ;;
    *) chk 0 "binding says what it will do with the backlog (note: '$note')" ;;
esac

got=$(q "SELECT extract(epoch FROM history_before)::bigint
           FROM sn_config.agent_channels WHERE instance_ref='$BOX_A'")
[ -n "$got" ] && [ "$got" -ge "$((before - 5))" ]
chk $((1-$?)) "the watermark is the moment of binding, not some default"

# --- 4. re-saving the SAME mailbox does not move it ------------------------
# A panel re-saves on every edit. Moving the watermark each time would swallow
# whatever arrived since the last poll -- silently, which is the whole disease.
sleep 1
api -X PUT "$API/api/agents/$SLUG/channels/email" -H 'Content-Type: application/json' \
    -d "{\"enabled\":true,\"instance_ref\":\"$BOX_A\"}" >/dev/null
same=$(q "SELECT extract(epoch FROM history_before)::bigint
            FROM sn_config.agent_channels WHERE instance_ref='$BOX_A'")
# Both values must EXIST before comparing them. Written as a bare
# equality this passed while every check around it failed, because two
# empty strings are equal -- the exact shape of a check that cannot fail.
[ -n "$same" ] && [ -n "$got" ] && [ "$same" = "$got" ]
chk $((1-$?)) "re-saving an unchanged mailbox keeps the original watermark (was: ${got:-none}, now: ${same:-none})"

# --- 5. pointing at a DIFFERENT mailbox renews it --------------------------
# The paired positive for check 4: if the code simply never wrote a watermark,
# check 4 would pass and this one would fail.
api -X PUT "$API/api/agents/$SLUG/channels/email" -H 'Content-Type: application/json' \
    -d "{\"enabled\":true,\"instance_ref\":\"$BOX_B\"}" >/dev/null
moved=$(q "SELECT extract(epoch FROM history_before)::bigint
             FROM sn_config.agent_channels WHERE instance_ref='$BOX_B'")
[ -n "$moved" ] && [ "$moved" -gt "$got" ]
chk $((1-$?)) "a new mailbox is a new connection and takes a fresh watermark"

# --- 6. a 'history' row is invisible to BOTH consumers ---------------------
#
# Written with the consumers' OWN predicates, and run INSIDE A TRANSACTION THAT
# ROLLS BACK.
#
# The rollback is not tidiness. The first version of this section committed a
# genuinely routable inbound row on a running node, and the node did what it is
# built to do: FAST_CHANNELS=email, the dispatch loop picks up a 'new' inbound
# row within DISPATCH_INTERVAL seconds, and by the time the assertion ran the
# status was no longer 'new'. So the test failed for the right reason and the
# wrong cause -- and, worse, it had fed a fake customer message to a live agent.
# A test that wakes the thing it is measuring is not a test.
#
# Uncommitted rows are invisible to the daemon and to every other session, and
# nothing survives the run, so the trigger-disabling cleanup this section used
# to need is gone with it -- disabling append-only triggers on a live node to
# tidy up after a test was the second-worst thing here.
cat > /tmp/.e2e-hist-$$.sql <<SQL
SET search_path TO sn_core, sn_config, public;
BEGIN;
INSERT INTO sn_core.messages
  (channel_type, channel_id, external_id, direction, sender, recipient,
   content, content_type, received_at)
VALUES ('email','$BOX_B','<e2e-live-$$@x>','inbound','them@example.invalid',
        '$BOX_B','live','text/plain', now());
INSERT INTO sn_core.messages
  (channel_type, channel_id, external_id, direction, sender, recipient,
   content, content_type, received_at)
VALUES ('email','$BOX_B','<e2e-old-$$@x>','inbound','them@example.invalid',
        '$BOX_B','backlog','text/plain', now() - interval '90 days');
UPDATE sn_core.message_state SET status='history'
 WHERE message_id = (SELECT id FROM sn_core.messages WHERE external_id='<e2e-old-$$@x>');

-- 1. the queue really stores it
SELECT 'stored=' || ms.status FROM sn_core.message_state ms
  JOIN sn_core.messages m ON m.id=ms.message_id
 WHERE m.external_id='<e2e-old-$$@x>';

-- 2. the Runner's own predicate, from routes/agents.py /with-pending-work
SELECT 'pending=' || count(*) FROM sn_core.messages m
  JOIN sn_core.message_state ms ON ms.message_id=m.id AND ms.status='new'
 WHERE m.direction='inbound' AND m.recipient='$BOX_B';

-- 3. the fast-dispatch predicate, from dispatch.py SCAN_SQL
SELECT 'scan=' || coalesce(string_agg(m.content, ',' ORDER BY m.id),'none')
  FROM sn_core.message_state ms
  JOIN sn_core.messages m ON m.id=ms.message_id
 WHERE ms.status='new' AND m.direction='inbound' AND m.recipient='$BOX_B';

-- 4. history is evidence, not a deletion: it is still there to be read
SELECT 'readable=' || count(*) FROM sn_core.messages
 WHERE external_id='<e2e-old-$$@x>';
ROLLBACK;
SQL
out=$(sudo -n -u postgres $PSQL -tA -f /tmp/.e2e-hist-$$.sql 2>&1)
rm -f /tmp/.e2e-hist-$$.sql

val(){ printf '%s\n' "$out" | grep -m1 "^$1=" | cut -d= -f2-; }

[ "$(val stored)" = "history" ]
chk $((1-$?)) "the queue actually stores 'history' (got: $(val stored))"

# Paired positive: the live row MUST be offered, or the negative below proves
# nothing -- an empty table would satisfy "the backlog is not offered".
[ "$(val pending)" = "1" ]
chk $((1-$?)) "the wake path offers the live message and only it (offered: $(val pending))"

[ "$(val scan)" = "live" ]
chk $((1-$?)) "fast-dispatch sees 'live' and not 'backlog' (saw: $(val scan))"

[ "$(val readable)" = "1" ]
chk $((1-$?)) "the backlog message is still recorded and readable"

# --- cleanup ---------------------------------------------------------------
# Only the agent: section 6 committed nothing.
api -X DELETE "$API/api/agents/$SLUG" >/dev/null 2>&1
left=$(q "SELECT count(*) FROM sn_core.messages WHERE external_id LIKE '<e2e-%-$$@x>'")
[ "${left:-1}" = "0" ]; chk $((1-$?)) "the run committed no message rows (found: ${left:-?})"

echo
if [ "$fail" = 0 ]; then echo "[e2e] history watermark: ALL GREEN"; else echo "[e2e] history watermark: FAILURES"; fi
exit $fail
