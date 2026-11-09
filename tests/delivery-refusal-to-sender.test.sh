#!/usr/bin/env bash
#
# The delivery gate checks the agent that wrote the message, and a message that
# does not go out comes back to that agent (#9).
#
# WHAT WENT WRONG. A background answer to a call was queued as an outbound voice
# row with an empty sender. The contact gate finds the sending agent by the
# address, so an empty one matched whichever agent also had an empty voice
# binding, and the refusal named that agent, not the one that wrote it. The
# refusal became an event and a state on the row; the agent that wrote it had
# been told "ok" by /send and never heard otherwise.
#
# WHAT IS CHECKED. send_outbound's pass, run for real (common/contacts.py and
# common/db.py from the tree) against a throwaway postgres built from the tree's
# B02 files, with CONTACT_GATE on. Nothing is transmitted: the only rows that
# reach a transport are the owner letters, and there is no SMTP here.
#
#   empty-sender   a row with an empty sender and no named agent is refused as
#                  such, and the refusal names no other agent
#   named-agent    a row that names its agent is checked against THAT agent's
#                  grants, not the agent bound to the address
#   told           the agent that wrote a refused row gets an internal note from
#                  system saying it was not delivered and why
#   owner-letter   a voice-late-answer letter from the node to the owner passes
#                  the gate as a system letter, like the budget alert
#
# USAGE
#   tests/delivery-refusal-to-sender.test.sh                  # working tree, expect PASS
#   tests/delivery-refusal-to-sender.test.sh --baseline REF   # git:REF, expect FAIL
#
# Needs postgres server binaries (PG_BIN) and a python with psycopg2 (COMM_PY,
# default the core-api venv).
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PY="${COMM_PY:-${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}}"
[[ -x "$PG_BIN/initdb" ]] || { echo "cannot run: no initdb (set PG_BIN)" >&2; exit 2; }
"$PY" -c "import psycopg2" 2>/dev/null \
    || { echo "cannot run: $PY lacks psycopg2 (set COMM_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/src"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/communication \
        | tar -x -C "$WORK/src" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$WORK/src/companions"
    cp -r "$ROOT/companions/communication" "$WORK/src/companions/"
fi

echo "delivery refusal reaches the sender (${BASELINE:+git:}${BASELINE:-working tree})"

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null 2>&1 \
    || { echo "cannot run: initdb failed" >&2; exit 2; }
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null 2>&1 \
    || { echo "cannot run: postgres did not start" >&2; cat "$WORK/pg.log" >&2; exit 2; }
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN SUPERUSER" >/dev/null
"${PSQL[@]}" -c "CREATE DATABASE smartnode OWNER smartnode" >/dev/null
for f in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$ROOT/$f" >>"$WORK/apply.log" 2>&1 \
        || { echo "cannot run: $f did not apply" >&2; tail -5 "$WORK/apply.log" >&2; exit 2; }
done

# Agents: alex wrote the rows; bob is bound to voice with an empty address too
# (the kind of agent the old gate named). The email row names alex but went out
# from an address bound to carol, who HAS a grant for the recipient while alex
# does not: the address-based lookup checks carol and lets it through.
cat > "$WORK/fixture.sql" <<'SQL'
SET search_path TO sn_core, sn_config, public;
DO $$
DECLARE t bigint := (SELECT min(id) FROM sn_config.tenants);
        owner_id bigint;
        cid bigint;
BEGIN
  SELECT id INTO owner_id FROM sn_config.users WHERE role = 'super-admin' ORDER BY id LIMIT 1;
  IF owner_id IS NULL THEN
    INSERT INTO sn_config.users (id, email, role, tenant_id, active)
      VALUES (1, 'owner@example.test', 'super-admin', t, true) RETURNING id INTO owner_id;
  END IF;
  DELETE FROM sn_config.user_channels WHERE user_id = owner_id;
  INSERT INTO sn_config.user_channels (user_id, tenant_id, channel_slug, address, enabled)
    VALUES (owner_id, t, 'email', 'owner@example.test', true);
  INSERT INTO sn_config.agents (slug, title, model) VALUES
    ('alex', 'alex', 'claude-cli/sonnet'), ('bob', 'bob', 'claude-cli/sonnet'),
    ('carol', 'carol', 'claude-cli/sonnet');
  INSERT INTO sn_config.agent_channels (agent_id, channel_id, instance_ref, enabled)
    SELECT a.id, c.id,
           CASE WHEN c.slug <> 'email' THEN ''
                WHEN a.slug = 'carol' THEN 'shared@node.test' ELSE 'alex@node.test' END, true
      FROM sn_config.agents a, sn_config.channels c
     WHERE (a.slug IN ('bob', 'alex') AND c.slug = 'voice')
        OR (a.slug IN ('carol', 'alex') AND c.slug = 'email');
  INSERT INTO sn_config.contacts (kind, display_name, enabled) VALUES ('person', 'Client', true) RETURNING id INTO cid;
  INSERT INTO sn_config.contact_channels (contact_id, channel_id, handle, enabled)
    SELECT cid, id, 'client@example.test', true FROM sn_config.channels WHERE slug = 'email';
  INSERT INTO sn_config.agent_contacts (agent_id, contact_id, allowed_channels)
    SELECT a.id, cid, ARRAY[(SELECT id FROM sn_config.channels WHERE slug = 'email')]
      FROM sn_config.agents a WHERE a.slug = 'carol';
END $$;
INSERT INTO sn_core.messages (channel_type, channel_id, external_id, direction, sender, recipient,
                              content, content_type, metadata)
VALUES
  ('voice', '', '<v1@t>', 'outbound', '', 'voice:uid-123', 'the answer', 'text/plain',
   '{"source": "comm-send"}'),
  ('email', 'shared@node.test', '<e1@t>', 'outbound', 'shared@node.test', 'client@example.test',
   'hello', 'text/plain', '{"source": "comm-send", "agent": "alex", "subject": "s"}'),
  ('email', 'node@example.test', '<o1@t>', 'outbound', 'node@example.test', 'owner@example.test',
   'late answer', 'text/plain', '{"source": "voice-late-answer", "subject": "s"}');
SQL
"${PSQL[@]}" -d smartnode -f "$WORK/fixture.sql" >"$WORK/fixture.log" 2>&1 \
    || { echo "cannot run: fixture" >&2; tail -8 "$WORK/fixture.log" >&2; exit 2; }

mkdir -p "$WORK/state" "$WORK/log"
( cd "$WORK/src/companions/communication" \
  && SMARTNODE_DB_DSN="host=$WORK dbname=smartnode user=smartnode options='-c search_path=sn_core,sn_config,public'" \
     CONTACT_GATE=on COMM_STATE_DIR="$WORK/state" STATE_DIR="$WORK/state" LOG_DIR="$WORK/log" \
     AGENT_ACCOUNTS_FILE="$WORK/state/none" PYTHONPATH=. \
     "$PY" send_outbound.py ) >"$WORK/pass.log" 2>&1
PRC=$?

Q() { "${PSQL[@]}" -d smartnode -tAc "SET search_path TO sn_core, sn_config, public; $1" | tail -1; }
PASS=0; FAIL=0
res() { if [[ "$2" == 1 ]]; then PASS=$((PASS+1)); echo "  ok   $1"; else FAIL=$((FAIL+1)); echo "  FAIL $1  ${3:-}"; fi; }

v_reason="$(Q "SELECT payload->>'reason' FROM events WHERE event_type = 'message.blocked'
               AND entity_id = (SELECT id::text FROM messages WHERE external_id = '<v1@t>')")"
res "empty-sender: refused as having no sending address, naming no other agent" \
    "$([[ "$v_reason" == *"no sending address"* && "$v_reason" != *"'bob'"* ]] && echo 1 || echo 0)" \
    "reason=$v_reason (pass rc=$PRC)"

e_reason="$(Q "SELECT payload->>'reason' FROM events WHERE event_type = 'message.blocked'
               AND entity_id = (SELECT id::text FROM messages WHERE external_id = '<e1@t>')")"
res "named-agent: the row's own agent (alex, no grant) is checked, not carol" \
    "$([[ "$e_reason" == *"'alex'"* ]] && echo 1 || echo 0)" "reason=${e_reason:-<none: it was not refused>}"

note="$(Q "SELECT content FROM messages WHERE channel_type = 'internal' AND sender = 'system'
           AND recipient = 'alex' AND metadata->>'source' = 'delivery-refused'
           AND (metadata->>'about_message_id') = (SELECT id::text FROM messages WHERE external_id = '<e1@t>')")"
res "told: alex gets an internal note that its message was not delivered" \
    "$([[ "$note" == *"NOT delivered"* ]] && echo 1 || echo 0)" "note=${note:-<none>}"

o_blocked="$(Q "SELECT count(*) FROM events WHERE event_type = 'message.blocked'
                AND entity_id = (SELECT id::text FROM messages WHERE external_id = '<o1@t>')")"
res "owner-letter: the node's late-answer letter to the owner passes the gate" \
    "$([[ "$o_blocked" == 0 ]] && echo 1 || echo 0)" "blocked events=$o_blocked"

echo
if (( FAIL == 0 )); then echo "PASS: $PASS checks"; exit 0; fi
echo "FAIL: $FAIL of $((PASS+FAIL))"; tail -5 "$WORK/pass.log" | sed 's/^/    /'; exit 1
