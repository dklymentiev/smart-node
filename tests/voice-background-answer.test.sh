#!/usr/bin/env bash
#
# An answer to a request made on a call reaches the caller, or the owner by mail
# when the call has ended, and never goes out on a channel nothing sends on (#9).
#
# WHAT WENT WRONG. The owner asked an agent something on a call; the voice line
# handed it to the agent's background turn with send_message(to=self). The
# background turn answered, and then:
#   - the hand-off carried origin.conversation = voice:<uid>, the socket id of the
#     browser, while every turn of the call was recorded on the caller's voice
#     address; nothing ever arrived on voice:<uid>;
#   - /inbox listed the answer (and the hand-off itself) in to_relay and told the
#     agent to send it to voice:<uid> on channel voice;
#   - /send queued that as an outbound voice row with an empty sender, answered
#     ok, and the delivery gate later blocked it naming a different agent;
#   - when the call had already ended there was no path at all.
#
# WHAT IS CHECKED. core-api's messages router, run for real (FastAPI TestClient,
# the core-api venv) against a throwaway postgres built from the tree's B02
# files. Nothing on the host is touched.
#
#   address        a hand-off from the line records the caller's voice address as
#                  origin.conversation, and the call's session id
#   second-handoff a second hand-off to self on the same call is a new request;
#                  it used to be taken as the answer to the first (an earlier
#                  coercion matched the agent's own open request)
#   request        the hand-off itself is not listed in to_relay
#   answer         the agent's answer to it is not in to_relay, and the guidance
#                  says the line speaks it / the node mails it
#   desk-kept      an answer with a desk origin is still in to_relay
#   voice-send     /send on channel voice is refused, naming inbox_reply; no row
#   empty-sender   /send on a channel where the agent's address is empty is
#                  refused; nothing queued
#   agent-named    an email /send records the composing agent on the row
#   live-no-mail   an answer while the call is live is not mailed
#   late-mail      an answer after voice.call.ended is mailed to the owner by the
#                  node (source voice-late-answer, from SN_NOTIFY_SENDER), once
#   hangup         /voice-call-ended mails an answer the line did not speak and
#                  skips one it did; it refuses before the call has ended
#   no-mailbox     without SN_NOTIFY_SENDER the answering agent is told, and an
#                  event says why: not dropped silently
#
# USAGE
#   tests/voice-background-answer.test.sh                  # working tree, expect PASS
#   tests/voice-background-answer.test.sh --baseline REF   # git:REF, expect FAIL
#
# Needs postgres server binaries (PG_BIN) and the core-api venv (CORE_API_PY).
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PG_BIN/initdb" ]] || { echo "cannot run: no initdb (set PG_BIN)" >&2; exit 2; }
"$PY" -c "import fastapi, httpx, psycopg2" 2>/dev/null \
    || { echo "cannot run: $PY lacks fastapi/httpx/psycopg2 (set CORE_API_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/src"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/core-api \
        | tar -x -C "$WORK/src" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$WORK/src/companions"
    cp -r "$ROOT/companions/core-api" "$WORK/src/companions/"
fi

echo "voice background answer (${BASELINE:+git:}${BASELINE:-working tree})"

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

# The owner (the super-admin) with an email and a voice address; two agents, each
# bound to voice with an empty address (voice has no mailbox) and alex to email.
cat > "$WORK/fixture.sql" <<'SQL'
SET search_path TO sn_core, sn_config, public;
DO $$
DECLARE t bigint := (SELECT min(id) FROM sn_config.tenants);
        owner_id bigint;
BEGIN
  SELECT id INTO owner_id FROM sn_config.users WHERE role = 'super-admin' ORDER BY id LIMIT 1;
  IF owner_id IS NULL THEN
    INSERT INTO sn_config.users (email, role, tenant_id, active)
      VALUES ('owner@example.test', 'super-admin', t, true) RETURNING id INTO owner_id;
  ELSE
    UPDATE sn_config.users SET email = 'owner@example.test', active = true WHERE id = owner_id;
  END IF;
  DELETE FROM sn_config.user_channels WHERE user_id = owner_id;
  INSERT INTO sn_config.user_channels (user_id, tenant_id, channel_slug, address, enabled) VALUES
    (owner_id, t, 'email', 'owner@example.test', true),
    (owner_id, t, 'voice', 'voice:owner@example.test', true);
  INSERT INTO sn_config.agents (slug, title, model) VALUES
    ('alex', 'alex', 'claude-cli/sonnet'), ('bob', 'bob', 'claude-cli/sonnet');
  INSERT INTO sn_config.agent_channels (agent_id, channel_id, instance_ref, enabled)
    SELECT a.id, c.id, CASE WHEN c.slug = 'email' THEN 'alex@node.test' ELSE '' END, true
      FROM sn_config.agents a, sn_config.channels c
     WHERE (a.slug IN ('alex', 'bob') AND c.slug = 'voice') OR (a.slug = 'alex' AND c.slug = 'email');
END $$;
SQL
"${PSQL[@]}" -d smartnode -f "$WORK/fixture.sql" >"$WORK/fixture.log" 2>&1 \
    || { echo "cannot run: fixture" >&2; tail -8 "$WORK/fixture.log" >&2; exit 2; }

cat > "$WORK/drive.py" <<'PY'
import os, sys, json
sys.path.insert(0, os.environ["CORE_API_DIR"])
from fastapi import FastAPI
from fastapi.testclient import TestClient
import auth
from auth import Principal
from db import cursor
from routes import messages as mr

app = FastAPI()
app.include_router(mr.router)
who = {"p": None}
app.dependency_overrides[auth.get_principal] = lambda: who["p"]
c = TestClient(app)

def P(name, role="agent"):
    return Principal(username=name, user_id=0, tenant_id=1, role=role, strength="proved")
ALEX = P("alex")

def res(name, cond, note=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {note}"))

def q(sql, args=()):
    with cursor(commit=True) as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None

def meta(mid):
    r = q("SELECT metadata FROM sn_core.messages WHERE id = %s", (mid,))
    return (r[0]["metadata"] if r else {}) or {}

def send(p, body):
    who["p"] = p
    return c.post("/api/messages/send", json=body)

def reply(p, to_id, text):
    who["p"] = p
    return c.post("/api/messages/reply", json={"to_message_id": to_id, "body": text})

def inbox(p):
    who["p"] = p
    r = c.get("/api/messages/inbox", params={"limit": 50})
    return r.json() if r.status_code == 200 else {}

def handoff(call):
    return send(ALEX, {"to": "alex", "channel": "internal",
                       "body": "Summarise the week. " + call,
                       "origin": {"channel": "voice", "conversation": "voice:uid-123",
                                  "caller": "owner@example.test", "call": call}})

def ended(call):
    q("INSERT INTO sn_core.actors (id, display_name, type, layer) VALUES "
      "('component:voice', 'component:voice', 'component', 'system') ON CONFLICT (id) DO NOTHING")
    q("INSERT INTO sn_core.events (actor, event_type, entity_type, entity_id, payload) "
      "VALUES ('component:voice', 'voice.call.ended', 'call', %s, '{}')", (call,))

def owner_mails():
    return q("SELECT id, sender, recipient, metadata FROM sn_core.messages "
             "WHERE channel_type = 'email' AND direction = 'outbound' "
             "AND metadata->>'source' = 'voice-late-answer' ORDER BY id") or []

# address
r = handoff("call-A")
hid = (r.json() or {}).get("id") if r.status_code == 200 else None
org = meta(hid).get("origin") or {}
res("address: origin.conversation is the caller's voice address, call kept",
    org.get("conversation") == "voice:owner@example.test" and org.get("call") == "call-A",
    f"{r.status_code} origin={org}")

# second-handoff: a second hand-off to self is a new request, not an answer
r = handoff("call-A")
h2 = (r.json() or {}).get("id") if r.status_code == 200 else None
m2 = meta(h2)
res("second-handoff: a second hand-off to self on the call is a new request, not an answer",
    h2 is not None and not m2.get("reply_to_message_id") and not m2.get("coerced_reply"),
    f"{r.status_code} meta={json.dumps(m2)[:200]}")

# request / answer / desk-kept
ib = inbox(ALEX)
relay_ids = [x["message_id"] for x in ib.get("to_relay", [])]
res("request: the hand-off itself is not in to_relay", hid not in relay_ids, f"to_relay={relay_ids}")
r = reply(ALEX, hid, "Three deals closed, two pending.")
aid = (r.json() or {}).get("outbound_id") if r.status_code == 200 else None
ib = inbox(ALEX)
relay_ids = [x["message_id"] for x in ib.get("to_relay", [])]
res("answer: the voice answer is not in to_relay, guidance says line/node deliver it",
    aid is not None and aid not in relay_ids and "voice line speaks" in (ib.get("guidance") or ""),
    f"reply={r.status_code} aid={aid} to_relay={relay_ids}")

r = send(P("bob"), {"to": "alex", "channel": "internal", "body": "desk question",
                    "origin": {"channel": "web-chat", "conversation": "desk:abc"}})
dq = (r.json() or {}).get("id")
r = reply(ALEX, dq, "desk answer")
dans = (r.json() or {}).get("outbound_id")
ib = inbox(P("bob"))
relay_ids = [x["message_id"] for x in ib.get("to_relay", [])]
res("desk-kept: an answer with a desk origin is still relayed", dans in relay_ids,
    f"dans={dans} to_relay={relay_ids}")

# voice-send / empty-sender / agent-named
n0 = q("SELECT count(*) AS n FROM sn_core.messages WHERE channel_type = 'voice'")[0]["n"]
r = send(ALEX, {"to": "voice:owner@example.test", "channel": "voice", "body": "the answer"})
n1 = q("SELECT count(*) AS n FROM sn_core.messages WHERE channel_type = 'voice'")[0]["n"]
res("voice-send: /send on voice is refused naming inbox_reply, no row",
    r.status_code == 400 and "inbox_reply" in r.text and n1 == n0, f"{r.status_code} {r.text[:160]} rows {n0}->{n1}")

q("INSERT INTO sn_config.agent_channels (agent_id, channel_id, instance_ref, enabled) "
  "SELECT a.id, c.id, '', true FROM sn_config.agents a, sn_config.channels c "
  "WHERE a.slug = 'alex' AND c.slug = 'telegram'")
n0 = q("SELECT count(*) AS n FROM sn_core.messages WHERE channel_type = 'telegram'")[0]["n"]
r = send(ALEX, {"to": "12345", "channel": "telegram", "body": "hi"})
n1 = q("SELECT count(*) AS n FROM sn_core.messages WHERE channel_type = 'telegram'")[0]["n"]
res("empty-sender: no sending address on the channel is refused, nothing queued",
    r.status_code == 400 and n1 == n0, f"{r.status_code} {r.text[:160]} rows {n0}->{n1}")

r = send(ALEX, {"to": "someone@example.test", "channel": "email", "body": "hello", "subject": "s"})
eid = (r.json() or {}).get("id") if r.status_code == 200 else None
res("agent-named: an email row records the composing agent", meta(eid).get("agent") == "alex",
    f"{r.status_code} meta={meta(eid)}")

# live-no-mail / late-mail
os.environ["SN_NOTIFY_SENDER"] = "node@example.test"
mr._NOTIFY_SENDER = "node@example.test"
r = handoff("call-B"); hb = r.json().get("id")
r = reply(ALEX, hb, "live answer"); ab = r.json().get("outbound_id")
res("live-no-mail: an answer while the call is live is not mailed",
    not any(m["metadata"].get("answer_message_id") == ab for m in owner_mails()), f"mails={owner_mails()}")

r = handoff("call-C"); hc = r.json().get("id")
ended("call-C")
r = reply(ALEX, hc, "late answer")
ac = (r.json() or {}).get("outbound_id")
mails = [m for m in owner_mails() if m["metadata"].get("answer_message_id") == ac]
ok = (len(mails) == 1 and mails[0]["sender"] == "node@example.test"
      and mails[0]["recipient"] == "owner@example.test")
again = mr._mail_late_voice_answer.__call__ if hasattr(mr, "_mail_late_voice_answer") else None
if again:
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        again_out = mr._mail_late_voice_answer(cur, ac)
    ok = ok and again_out == "already" and len([m for m in owner_mails() if m["metadata"].get("answer_message_id") == ac]) == 1
res("late-mail: after voice.call.ended the node mails the owner, once", ok, f"reply={r.status_code} mails={mails}")

# hangup. The report comes from the voice line itself, a node service: it
# proves itself and is listed for the callback.
who["p"] = ALEX
os.environ["SN_SERVICE_AUTH_SECRET"] = "stand-secret"
os.environ["SN_SERVICE_PERMISSIONS"] = "voice:internal.messages.voice.write"
LINE = {"X-SN-Service": "voice", "X-SN-Service-Auth": "stand-secret"}
r0 = c.post("/api/messages/voice-call-ended", headers=LINE, json={"call": "call-B", "spoken": []})
r = handoff("call-B"); hb2 = r.json().get("id")
r = reply(ALEX, hb2, "spoken answer"); ab2 = r.json().get("outbound_id")   # live: the line speaks it
ended("call-B")
r1 = c.post("/api/messages/voice-call-ended", headers=LINE, json={"call": "call-B", "spoken": [ab2]})
mailed = [m["metadata"].get("answer_message_id") for m in owner_mails()]
res("hangup: an unspoken answer is mailed, a spoken one is not, and not before the call ends",
    r0.status_code == 409 and r1.status_code == 200 and ab in mailed and ab2 not in mailed,
    f"before={r0.status_code} after={r1.status_code} {r1.text[:160]} mailed={mailed} ab={ab} ab2={ab2}")

# no-mailbox
mr._NOTIFY_SENDER = None
r = handoff("call-D"); hd = r.json().get("id")
ended("call-D")
r = reply(ALEX, hd, "answer with nowhere to go")
ad = (r.json() or {}).get("outbound_id")
note = q("SELECT content FROM sn_core.messages WHERE recipient = 'alex' AND sender = 'system' "
         "AND metadata->>'about_message_id' = %s", (str(ad),)) or []
ev = q("SELECT 1 FROM sn_core.events WHERE event_type = 'voice.answer.undelivered' "
       "AND entity_id = %s", (str(ad),)) or []
res("no-mailbox: the agent is told and an event says why",
    bool(note) and bool(ev) and "SN_NOTIFY_SENDER" in (note[0]["content"] if note else ""),
    f"reply={r.status_code} note={note} event={bool(ev)}")
PY

CORE_API_DIR="$WORK/src/companions/core-api" \
PGHOST="$WORK" PGDATABASE=smartnode PGUSER=smartnode \
    "$PY" "$WORK/drive.py" >"$WORK/drive.out" 2>"$WORK/drive.err"
[[ -s "$WORK/drive.out" ]] || { echo "cannot run: driver produced nothing" >&2; tail -20 "$WORK/drive.err" >&2; exit 2; }
sed 's/^/  /' "$WORK/drive.out"
FAIL=$(grep -c '^FAIL' "$WORK/drive.out"); TOTAL=$(grep -cE '^(ok|FAIL)' "$WORK/drive.out")
[[ "$TOTAL" -lt 12 ]] && { echo "  driver stopped early:"; tail -6 "$WORK/drive.err" | sed 's/^/    /'; FAIL=$((FAIL + 12 - TOTAL)); TOTAL=12; }
echo
if (( FAIL == 0 )); then echo "PASS: $TOTAL checks"; exit 0; fi
echo "FAIL: $FAIL of $TOTAL"; exit 1
