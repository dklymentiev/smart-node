"""The calls behind tests/human-tcp-proof.test.sh. Run by that script, which
starts the database, core-api on TCP and on a unix socket, and a stand-in
admin-runner. Everything here is a real HTTP request to that core-api, made by
this file or by the node's own caller code imported from the tree under test."""
import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid

import httpx
import psycopg2

BASE = os.environ["T_BASE"]
SOCK = os.environ["T_SOCK"]
NOOWNER = os.environ["T_NOOWNER"]
NOOWNER_SOCK = os.environ["T_NOOWNER_SOCK"]
OWNER = os.environ["T_OWNER"]
SECRET = os.environ["T_SECRET"]
PROXY = os.environ["T_PROXY"]
SRC = os.environ["T_SRC"]
RUNNER_LOG = os.environ["T_RUNNER_LOG"]
PY = os.environ["T_PY"]
PLANNER_SRC = os.path.join(SRC, "companions/planner/src")
BRIN_SRC = os.path.join(SRC, "companions/brin")

failed = 0


def res(name, cond, note=""):
    global failed
    if not cond:
        failed = 1
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  %s" % (note,)))


def attempt(name, fn):
    """Run one group; a caller that cannot even be imported is a failure of
    that group, not of the run."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        res(name, False, "%s: %s" % (type(e).__name__, str(e)[:200]))


def q(sql, args=()):
    with psycopg2.connect(dbname="smartnode", user="smartnode") as con:
        with con.cursor() as cur:
            cur.execute("SET search_path TO sn_core, sn_config, public")
            cur.execute(sql, args)
            if cur.description:
                row = cur.fetchone()
                return row[0] if row else None


def events(event_type, **payload):
    sql = "SELECT count(*) FROM sn_core.events WHERE event_type = %s"
    args = [event_type]
    for key, value in payload.items():
        sql += " AND payload->>%s = %s"
        args += [key, value]
    return q(sql, args)


def proof(service, secret=SECRET, behalf=OWNER, remote_user=True):
    h = {"X-SN-Service": service, "X-SN-Service-Auth": secret,
         "X-SN-On-Behalf-Of": behalf}
    if remote_user:
        h["Remote-User"] = behalf
    return h


def message(sender, recipient, direction="inbound"):
    return q("INSERT INTO sn_core.messages (channel_type, external_id, direction, sender, "
             "recipient, content, content_type, tenant_id) "
             "VALUES ('internal', %s, %s, %s, %s, 'hello', 'text/plain', 1) RETURNING id",
             ("<%s@test>" % uuid.uuid4().hex, direction, sender, recipient))


def state(mid):
    return q("SELECT status FROM sn_core.message_state WHERE message_id = %s", (mid,))


tcp = httpx.Client(base_url=BASE, timeout=30)
uds = httpx.Client(transport=httpx.HTTPTransport(uds=SOCK), base_url="http://localhost",
                   timeout=60)
OWNER_ID = q("SELECT id FROM sn_config.users WHERE email = %s", (OWNER,))
NOTIFY = {"assignee": "testagent", "task_id": 1, "event": "assigned",
          "title": "A task", "summary": "for the agent"}
DENIED = "auth.human_tcp_assertion_denied"
desk_id = str(uuid.uuid4())
q("INSERT INTO sn_core.desks (id, tenant_id, owner_user_id, metadata) "
  "VALUES (%s, 1, %s, %s)", (desk_id, OWNER_ID, json.dumps({"agent": "testagent"})))


# ---- bare -------------------------------------------------------------------
def bare():
    before = events(DENIED, path="/api/desks")
    r = tcp.post("/api/desks", headers={"Remote-User": OWNER}, json={"title": "bare"})
    res("bare: an asserted owner name on a get_principal route is refused 403",
        r.status_code == 403 and "asserted Remote-User" in r.text, (r.status_code, r.text[:120]))
    res("bare: the refusal is written as an event",
        events(DENIED, path="/api/desks") == before + 1)
    before = events(DENIED, path="/api/agents")
    r = tcp.post("/api/agents", headers={"Remote-User": OWNER}, json={})
    res("bare: an asserted owner name on a get_actor route is refused 403, with its event",
        r.status_code == 403 and events(DENIED, path="/api/agents") == before + 1,
        (r.status_code, r.text[:120]))
    res("bare: nothing was created", q("SELECT count(*) FROM sn_core.desks WHERE title = 'bare'") == 0)
    r = tcp.get("/api/desks", headers={"Remote-User": OWNER})
    res("bare: a read by the same name still answers", r.status_code == 200, r.status_code)
    r = tcp.get("/api/messages/inbox", headers={"Remote-User": "testagent"})
    res("bare: an agent name over TCP is refused, as before",
        r.status_code == 403 and "names an agent" in r.text, (r.status_code, r.text[:120]))
    r = tcp.post("/api/desks", headers={"Remote-User": OWNER, "X-SN-Proxy-Auth": PROXY},
                 json={"title": "session"})
    res("bare: the same call with the proxy session proof is admitted",
        r.status_code == 201, (r.status_code, r.text[:120]))


# ---- scope (run before the planner's real notify, so 'no wake' means something)
def scope():
    r = tcp.post("/api/notify", headers=proof("planner", secret="not-the-secret"), json=NOTIFY)
    res("scope: a wrong secret is refused", r.status_code == 403, (r.status_code, r.text[:120]))
    r = tcp.post("/api/notify", headers=proof("planner", behalf="other@example.test"), json=NOTIFY)
    res("scope: a service cannot name a person who is not the owner",
        r.status_code == 403, (r.status_code, r.text[:120]))
    r = tcp.post("/api/notify", headers=proof("reports"), json=NOTIFY)
    res("scope: an unlisted service is refused", r.status_code == 403, (r.status_code, r.text[:120]))
    res("scope: none of the refused calls raised a wake",
        q("SELECT count(*) FROM sn_core.agent_runtime WHERE agent_slug = 'testagent' "
          "AND wake_requested_at IS NOT NULL") == 0)
    r = tcp.post("/api/desks", headers=proof("planner"), json={"title": "planner-desk"})
    res("scope: the planner proof on a route outside its list is refused",
        r.status_code == 403 and "delegated scope" in r.text, (r.status_code, r.text[:120]))
    mid = message("testagent", "desk:" + desk_id)
    r = tcp.post("/api/messages/%s/deliver-desk" % mid, headers=proof("planner"))
    res("scope: the planner proof on a route listed for another service is refused",
        r.status_code == 403 and state(mid) == "new", (r.status_code, state(mid)))
    r = tcp.post("/api/desks", headers=proof("brin"), json={"title": "brin-desk"})
    res("scope: the dispatcher proof on a route outside its list is refused",
        r.status_code == 403, (r.status_code, r.text[:120]))
    # No Remote-User here: the name travels in the delegation header alone,
    # which is how a service listed for "any agent" would try to be the owner.
    r = tcp.post("/api/desks", headers=proof("voice", remote_user=False),
                 json={"title": "wildcard-desk"})
    res("scope: a wildcard agent clause does not let a service name the owner",
        r.status_code == 403, (r.status_code, r.text[:120]))
    r = tcp.post("/api/desks", headers=proof("brin", remote_user=False),
                 json={"title": "brin-desk"})
    res("scope: nor does leaving the Remote-User header off",
        r.status_code == 403, (r.status_code, r.text[:120]))
    res("scope: no refused call created anything",
        q("SELECT count(*) FROM sn_core.desks WHERE title IN "
          "('planner-desk', 'brin-desk', 'wildcard-desk')") == 0)


# ---- planner: the real notify module ------------------------------------------
def planner():
    os.environ.update({"PLANNER_NOTIFY_URL": BASE + "/api/notify",
                       "SN_OPERATOR_ACTOR": OWNER, "SN_SERVICE_AUTH_SECRET": SECRET})
    sys.path.insert(0, PLANNER_SRC)
    import planner.notify as notify
    status = asyncio.run(notify._post_notify(dict(NOTIFY)))
    res("planner: the planner's own notify is admitted", status == 200, status)
    # An agent is woken for a task by a message that names the task, not by a
    # mark on its runtime row.
    res("planner: the assignee gets a message that names the task",
        q("SELECT count(*) FROM sn_core.messages WHERE recipient = 'testagent' "
          "AND channel_type = 'internal' AND sender = 'system' "
          "AND metadata->>'source' = 'planner' AND metadata->>'task_id' = '1'") == 1)
    res("planner: the call is recorded as the planner acting for the owner",
        events("identity.delegated_owner", service="planner", path="/api/notify") == 1)


# ---- brin: the real watcher and runner ----------------------------------------
def brin():
    os.environ.update({"CORE_API_URL": BASE, "SN_CORE_API_URL": BASE,
                       "AI_GATEWAY_URL": "http://127.0.0.1:9", "PLANNER_API_URL": "http://127.0.0.1:9",
                       "SN_OPERATOR_ACTOR": OWNER, "SN_SERVICE_AUTH_SECRET": SECRET})
    sys.path.insert(0, BRIN_SRC)
    from services import message_watcher
    import runner
    # runner configures root logging at INFO on import; keep warnings only.
    logging.disable(logging.INFO)

    desk_msg = message("testagent", "desk:" + desk_id)
    doorbell = message("system", "testagent")
    inbound = message("testagent", OWNER)

    async def calls():
        watcher = message_watcher.MessageWatcher()
        delivered = await watcher._deliver_desk(desk_msg)
        await watcher._mark_processed(doorbell)
        await runner._notify_owner(7, "A task was paused", "check it", "circuit_tripped")
        try:
            reply = await runner._release_reply(inbound, "approved answer")
        except httpx.HTTPStatusError as e:
            reply = e.response.status_code
        try:
            await runner._create_calendar_event({"summary": "Booking",
                                                 "start": "2030-01-01T10:00:00+00:00",
                                                 "end": "2030-01-01T11:00:00+00:00"})
            calendar = 200
        except httpx.HTTPStatusError as e:
            calendar = e.response.status_code
        return delivered, reply, calendar

    delivered, reply, calendar = asyncio.run(calls())
    res("brin: the watcher's desk delivery decision is admitted",
        delivered is True and state(desk_msg) == "processed", (delivered, state(desk_msg)))
    res("brin: the watcher closes a system message", state(doorbell) == "processed", state(doorbell))
    res("brin: the runner's owner alert is admitted",
        events("identity.delegated_owner", service="brin", path="/api/notify") == 1)
    res("brin: the runner releases an approved reply",
        isinstance(reply, dict) and reply.get("ok") is True, reply)
    # No calendar is connected here, so the route itself fails; what is under
    # test is that identity let the call reach it.
    res("brin: the runner's calendar call passes the identity check",
        calendar != 403 and events("identity.delegated_owner", service="brin",
                                   path="/api/integrations/google/calendar/events") == 1,
        calendar)
    r = tcp.get("/api/messages/inbox", headers=proof("brin", behalf="testagent"))
    res("brin: the same service still acts for an agent", r.status_code == 200,
        (r.status_code, r.text[:120]))


# ---- socket -------------------------------------------------------------------
def socket_peer():
    r = uds.post("/api/desks", json={"title": "socket-desk"})
    owner_of = q("SELECT owner_user_id FROM sn_core.desks WHERE title = 'socket-desk'")
    res("socket: a proved operator peer creates a desk owned by the owner",
        r.status_code == 201 and owner_of == OWNER_ID, (r.status_code, r.text[:120], owner_of))
    r = uds.post("/api/desks", headers={"Remote-User": "other@example.test"},
                 json={"title": "socket-header"})
    owner_of = q("SELECT owner_user_id FROM sn_core.desks WHERE title = 'socket-header'")
    res("socket: a Remote-User header on the socket changes nothing",
        r.status_code == 201 and owner_of == OWNER_ID, (r.status_code, owner_of))
    open(RUNNER_LOG, "w").close()
    r = uds.post("/api/agents", json={"tenant_id": 1, "slug": "socketagent", "title": "Socket Agent",
                                      "specialist_body": "You answer questions."})
    calls = [json.loads(line) for line in open(RUNNER_LOG) if line.strip()]
    named = calls[-1]["on_behalf_of"] if calls else None
    res("socket: agent creation names the owner to admin-runner's owner gate",
        named == OWNER, (r.status_code, r.text[:160], named))
    # What that gate checks: an active users row whose role is the owner role.
    res("socket: that name is an active super-admin",
        q("SELECT role FROM sn_config.users WHERE email = %s AND active", (named,)) == "super-admin")
    res("socket: the agent exists afterwards",
        r.status_code == 201 and q("SELECT count(*) FROM sn_config.agents WHERE slug = 'socketagent'") == 1,
        (r.status_code, r.text[:160]))


# ---- loud ---------------------------------------------------------------------
def loud():
    r = httpx.post(NOOWNER + "/api/notify", headers=proof("planner"), json=NOTIFY, timeout=30)
    res("loud: with no owner configured core-api answers 503 naming the setting",
        r.status_code == 503 and "SN_OPERATOR_ACTOR" in r.text, (r.status_code, r.text[:160]))
    blind = httpx.Client(transport=httpx.HTTPTransport(uds=NOOWNER_SOCK),
                         base_url="http://localhost", timeout=30)
    r = blind.post("/api/desks", json={"title": "no-owner-desk"})
    res("loud: with no owner configured the socket peer is nobody, not a guessed name",
        r.status_code == 403 and q("SELECT count(*) FROM sn_core.desks WHERE title = 'no-owner-desk'") == 0,
        (r.status_code, r.text[:120]))

    def run(code, path, drop):
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.update({"PYTHONPATH": path, "PLANNER_NOTIFY_URL": BASE + "/api/notify",
                    "SN_SERVICE_AUTH_SECRET": SECRET, "SN_OPERATOR_ACTOR": OWNER})
        for name in drop:
            env.pop(name, None)
        return subprocess.run([PY, "-c", code], env=env, capture_output=True, text=True, timeout=60)

    code = ("import asyncio, planner.notify as n; "
            "print('RESULT', asyncio.run(n._post_notify(%r)))" % (NOTIFY,))
    p = run(code, PLANNER_SRC, ["SN_OPERATOR_ACTOR"])
    res("loud: the planner without the owner setting sends nothing and names it",
        "RESULT None" in p.stdout and "SN_OPERATOR_ACTOR" in p.stderr,
        (p.stdout[-80:], p.stderr[-200:]))
    p = run(code, PLANNER_SRC, ["SN_SERVICE_AUTH_SECRET"])
    res("loud: the planner without the credential sends nothing and names it",
        "RESULT None" in p.stdout and "SN_SERVICE_AUTH_SECRET" in p.stderr,
        (p.stdout[-80:], p.stderr[-200:]))
    code = ("from core.owner_proof import owner_headers\n"
            "try:\n    owner_headers(); print('RESULT sent')\n"
            "except Exception as e:\n    print('RESULT', e)")
    p = run(code, BRIN_SRC, ["SN_OPERATOR_ACTOR", "SN_SERVICE_AUTH_SECRET"])
    res("loud: brin without either setting refuses to build the call and names both",
        "RESULT SN_OPERATOR_ACTOR and SN_SERVICE_AUTH_SECRET" in p.stdout, (p.stdout[-200:], p.stderr[-200:]))


for group, fn in (("bare", bare), ("scope", scope), ("planner", planner), ("brin", brin),
                  ("socket", socket_peer), ("loud", loud)):
    attempt("%s: the checks ran" % group, fn)
sys.exit(failed)
