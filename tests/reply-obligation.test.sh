#!/usr/bin/env bash
#
# A request that asked for a reply is not lost when the agent ends its turn
# without answering.
#
# WHAT WENT WRONG. An agent asked a colleague for something. The colleague was
# woken, did not answer, and the Runner closed the message the way it closes
# everything a finished wake leaves open (_close_what_woke_it -> /dismiss). From
# then on nothing on the node knew an answer was owed: the colleague was never
# woken for it again and the asker was never told. The owner found out by asking
# "well?" in the chat, about thirteen times a day.
#
# WHAT IS CHECKED. routes/messages.py from the tree, run against a throwaway
# postgres built from the tree's B02 files, so the schema the feature needs is
# checked with the code that uses it.
#
#   plain          a message that did not ask for a reply is closed as before
#   held           one that did is held as awaiting_reply, first wake counted
#   not-due        nothing happens inside the window
#   busy           an agent in the middle of a run is not reminded and the
#                  attempt is not spent
#   reminded       a free agent gets a reminder as an internal message, once
#                  per window, numbered
#   reported       when the attempts are used up the request is closed, tagged
#                  obligation:unanswered, journalled, and the asker is told
#   visible        a held request still matches the inbox the model reads and
#                  is still found as the open question an answer belongs to
#   off            without the two settings nothing is held
#
# USAGE
#   tests/reply-obligation.test.sh                  # working tree, expect PASS
#   tests/reply-obligation.test.sh --baseline REF   # git:REF, expect FAIL
#
# Needs postgres server binaries (PG_BIN) and the core-api python (CORE_API_PY).
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

PG_BIN="${PG_BIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PY="${CORE_API_PY:-/opt/smart-node/core-api/venv/bin/python}"
[[ -x "$PG_BIN/initdb" ]] || { echo "cannot run: no initdb (set PG_BIN)" >&2; exit 2; }
"$PY" -c "import psycopg2, fastapi" 2>/dev/null \
    || { echo "cannot run: $PY lacks psycopg2 or fastapi (set CORE_API_PY)" >&2; exit 2; }

WORK="$(mktemp -d)"
chmod 755 "$WORK"
cleanup() {
    "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/src"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" pearlve "$BASELINE" companions/core-api bootstrap \
        | tar -x -C "$WORK/src" || { echo "cannot read $BASELINE" >&2; exit 2; }
else
    mkdir -p "$WORK/src/companions"
    cp -r "$ROOT/companions/core-api" "$WORK/src/companions/"
    cp -r "$ROOT/bootstrap" "$WORK/src/"
fi

echo "reply obligation (${BASELINE:+git:}${BASELINE:-working tree})"

"$PG_BIN/initdb" -D "$WORK/data" -A trust -U postgres >/dev/null 2>&1 \
    || { echo "cannot run: initdb failed" >&2; exit 2; }
"$PG_BIN/pg_ctl" -D "$WORK/data" -o "-k $WORK -c listen_addresses=" \
    -l "$WORK/pg.log" -w start >/dev/null 2>&1 \
    || { echo "cannot run: postgres did not start" >&2; cat "$WORK/pg.log" >&2; exit 2; }
PSQL=("$PG_BIN/psql" -h "$WORK" -U postgres -X -q -v ON_ERROR_STOP=1)
"${PSQL[@]}" -c "CREATE ROLE smartnode LOGIN SUPERUSER" >/dev/null
"${PSQL[@]}" -c "CREATE DATABASE smartnode OWNER smartnode" >/dev/null
for f in bootstrap/B02-schema.sql bootstrap/B02-sn-core-schema.sql bootstrap/B02-sn-core-seed.sql; do
    "${PSQL[@]}" -d smartnode -f "$WORK/src/$f" >>"$WORK/apply.log" 2>&1 \
        || { echo "cannot run: $f did not apply" >&2; tail -5 "$WORK/apply.log" >&2; exit 2; }
done
"${PSQL[@]}" -d smartnode >"$WORK/fixture.log" 2>&1 <<'SQL' \
    || { echo "cannot run: fixture" >&2; tail -8 "$WORK/fixture.log" >&2; exit 2; }
INSERT INTO sn_config.agents (slug, title, model) VALUES
  ('asker', 'asker', 'claude-cli/sonnet'), ('doer', 'doer', 'claude-cli/sonnet');
INSERT INTO sn_core.agent_runtime (agent_slug, status) VALUES ('asker', 'free'), ('doer', 'free')
  ON CONFLICT (agent_slug) DO UPDATE SET status = 'free';
SQL

cat > "$WORK/drive.py" <<'PY'
import os
import sys

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json

sys.path.insert(0, ".")
from routes import messages as M

conn = psycopg2.connect(os.environ["T_DSN"])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("SET search_path TO sn_core, sn_config, public")
cur.execute("SELECT min(id) AS t FROM sn_config.tenants")
TENANT = cur.fetchone()["t"]
n = [0]


def res(name, ok, detail=""):
    print("%s|%s|%s" % ("1" if ok else "0", name, detail))


def ask(needs_reply=True, origin=None):
    n[0] += 1
    meta = {"source": "comm-send"}
    if needs_reply:
        meta["needs_reply"] = True
    if origin:
        meta["origin"] = origin
    cur.execute(
        "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, direction, "
        " sender, recipient, content, content_type, metadata, tenant_id) "
        "VALUES ('internal', 'doer', %s, 'inbound', 'asker', 'doer', 'please do X', "
        "        'text/plain', %s, %s) RETURNING id",
        ("<t%d@test>" % n[0], Json(meta), TENANT))
    return cur.fetchone()["id"]


def state(mid):
    cur.execute("SELECT status, wake_attempts, wake_next_at, tags FROM sn_core.message_state "
                "WHERE message_id = %s", (mid,))
    return cur.fetchone()


def notes(mid, to):
    cur.execute("SELECT content FROM sn_core.messages WHERE sender = 'system' "
                "AND recipient = %s AND metadata->>'source' = 'reply-obligation' "
                "AND (metadata->>'about_message_id')::bigint = %s ORDER BY id", (to, mid))
    return [r["content"] for r in cur.fetchall()]


def overdue(mid):
    cur.execute("UPDATE sn_core.message_state SET wake_next_at = now() - interval '1 second' "
                "WHERE message_id = %s", (mid,))


os.environ.pop("SN_REPLY_WAKE_WINDOW_SECONDS", None)
os.environ.pop("SN_REPLY_MAX_ATTEMPTS", None)
unset = ask()
res("off: without the two settings nothing is held",
    M._keep_reply_obligation(cur, [unset]) == [] and state(unset)["status"] == "new")

os.environ["SN_REPLY_WAKE_WINDOW_SECONDS"] = "600"
os.environ["SN_REPLY_MAX_ATTEMPTS"] = "3"

plain = ask(needs_reply=False)
res("plain: a message that did not ask for a reply is not held",
    M._keep_reply_obligation(cur, [plain]) == [] and state(plain)["status"] == "new")

mid = ask()
held = M._keep_reply_obligation(cur, [mid])
s = state(mid)
res("held: awaiting_reply, first wake counted, a reminder scheduled",
    held == [mid] and s["status"] == "awaiting_reply" and s["wake_attempts"] == 1
    and s["wake_next_at"] is not None, dict(s))

M.sweep_reply_obligations(cur)
res("not-due: nothing happens inside the window",
    notes(mid, "doer") == [] and state(mid)["wake_attempts"] == 1)

overdue(mid)
cur.execute("UPDATE sn_core.agent_runtime SET status = 'busy', "
            "lease_until = now() + interval '5 minutes' WHERE agent_slug = 'doer'")
M.sweep_reply_obligations(cur)
res("busy: an agent in a run is not reminded and the attempt is kept",
    notes(mid, "doer") == [] and state(mid)["wake_attempts"] == 1)
cur.execute("UPDATE sn_core.agent_runtime SET status = 'free', lease_until = NULL "
            "WHERE agent_slug = 'doer'")

M.sweep_reply_obligations(cur)
got = notes(mid, "doer")
res("reminded: a free agent gets reminder 2 of 3 as an internal message",
    len(got) == 1 and "attempt 2 of 3" in got[0] and state(mid)["wake_attempts"] == 2, got)
M.sweep_reply_obligations(cur)
res("reminded: not again inside the window", len(notes(mid, "doer")) == 1)
overdue(mid)
M.sweep_reply_obligations(cur)
got = notes(mid, "doer")
res("reminded: then 3 of 3", len(got) == 2 and "attempt 3 of 3" in got[1], got[-1:])

overdue(mid)
M.sweep_reply_obligations(cur)
s = state(mid)
res("reported: closed and tagged obligation:unanswered",
    s["status"] == "processed" and "obligation:unanswered" in s["tags"], dict(s))
told = notes(mid, "asker")
res("reported: the asker is told once, naming who stayed silent",
    len(told) == 1 and "doer" in told[0], told)
res("reported: the silent agent is not reminded again", len(notes(mid, "doer")) == 2)
cur.execute("SELECT count(*) AS c FROM sn_core.events "
            "WHERE event_type = 'message.reply_overdue' AND entity_id = %s", (str(mid),))
res("reported: one journal entry", cur.fetchone()["c"] == 1)

q = ask(origin={"channel": "web-chat", "conversation": "desk:t"})
M._keep_reply_obligation(cur, [q])
found = M._open_question_from(cur, "doer", "asker", TENANT)
res("visible: a held request is still the open question an answer belongs to",
    found is not None and found["id"] == q)
conn.rollback()
PY

( cd "$WORK/src/companions/core-api" \
  && T_DSN="host=$WORK dbname=smartnode user=smartnode" PYTHONPATH=. \
     "$PY" "$WORK/drive.py" ) >"$WORK/out.log" 2>"$WORK/err.log"
DRC=$?

PASS=0; FAIL=0
while IFS='|' read -r ok name detail; do
    [[ "$ok" == 0 || "$ok" == 1 ]] || continue
    if [[ "$ok" == 1 ]]; then PASS=$((PASS+1)); echo "  ok   $name"
    else FAIL=$((FAIL+1)); echo "  FAIL $name  $detail"; fi
done < "$WORK/out.log"
# A driver that died is a failure whatever it printed before dying, and one that
# printed nothing must not read as "0 failed".
if (( DRC != 0 )) || (( PASS + FAIL == 0 )); then
    FAIL=$((FAIL+1)); echo "  FAIL the driver did not finish (rc=$DRC)"
    tail -4 "$WORK/err.log" | sed 's/^/    /'
fi

# The view the model reads must include a held request; this is the filter the
# route builds, checked as source because the route needs a signed-in caller.
if grep -q "ms.status IN ('new', 'routed', 'awaiting_reply')" \
        "$WORK/src/companions/core-api/routes/messages.py"; then
    PASS=$((PASS+1)); echo "  ok   visible: the inbox the model reads includes a held request"
else
    FAIL=$((FAIL+1)); echo "  FAIL visible: the inbox filter does not include awaiting_reply"
fi

echo
if (( FAIL == 0 )); then echo "PASS: $PASS checks"; exit 0; fi
echo "FAIL: $FAIL of $((PASS+FAIL))"; exit 1
