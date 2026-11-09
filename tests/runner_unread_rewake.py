"""The real Runner tick against the real poll and claim, on a disposable Postgres.

What is due is core-api's answer since 2026-10-29 (message_state.handed_at,
handed_count, read by agents_with_pending_work and written by the claim), so the
guarantee can only be tested with both halves running. Time passes by moving the
stored timestamps back, because the intervals are read by SQL's now().
"""
import asyncio
import importlib.util
import os
import sys
import types

import psycopg2

os.environ.setdefault("SN_CORE_API_URL", "http://127.0.0.1:8030")
os.environ.setdefault("AI_GATEWAY_URL", "http://127.0.0.1:8000")
os.environ.setdefault("PLANNER_API_URL", "http://127.0.0.1:8030/api/planner")
os.environ["SN_WAKE_RETRY_SECONDS"] = "60"
os.environ["SN_WAKE_REMINDERS"] = "3"

from routes.agents import RuntimeAction, agent_runtime_action, agents_with_pending_work  # noqa: E402
from routes.messages import MessageUnhandled, message_unhandled  # noqa: E402

sys.path.append(os.path.dirname(os.path.abspath(sys.argv[1])))
spec = importlib.util.spec_from_file_location("runner_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def sql(statement, args=(), fetch=False):
    with psycopg2.connect(dbname="smartnode", user="smartnode") as con:
        with con.cursor() as cur:
            cur.execute("SET search_path TO sn_core, sn_config, public")
            cur.execute(statement, args)
            return cur.fetchall() if fetch else None


serial = {"n": 0}


def mail(to="hazel"):
    serial["n"] += 1
    return sql("INSERT INTO sn_core.messages (channel_type, external_id, direction, sender, recipient, content) "
               "VALUES ('internal', %s, 'inbound', 'pat', %s, 'hello') RETURNING id",
               ("x%d" % serial["n"], to), fetch=True)[0][0]


def answered(*ids):
    sql("UPDATE sn_core.message_state SET status = 'processed' WHERE message_id = ANY(%s)", (list(ids),))


def age(seconds=70, handed_only=False):
    """Everything happened this much earlier. handed_only: the wake is old, but
    its turn has only just ended."""
    sql("UPDATE sn_core.message_state SET handed_at = handed_at - make_interval(secs => %s) "
        "WHERE handed_at IS NOT NULL", (seconds,))
    if not handed_only:
        sql("UPDATE sn_core.agent_runtime SET updated_at = updated_at - make_interval(secs => %s)", (seconds,))


launched, events, final_texts, wakes = [], [], [], []
gateway = {"status": 200, "body": {"content": "done", "final_answer": "done"}}


class Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class Http:
    async def post(self, url, **kw):
        body = kw.get("json") or {}
        if url.endswith("/unhandled"):
            message_unhandled(int(url.split("/")[-2]), MessageUnhandled(reason=body.get("reason")))
        elif "/api/events" in url:
            events.append(body.get("event_type"))
        elif url.endswith("/api/message"):
            wakes.append(1)
            return Response(gateway["status"], gateway["body"])
        elif url.endswith("/final-text"):
            final_texts.append(body.get("body"))
        return Response(200, {})


async def pending(_c):
    return agents_with_pending_work()["agents"]


async def nobody_busy(_c):
    return 0


async def fake_launch(agent, ref, _flag=None, _wake=None, _withhold=None, handed=None, scope=None, *_rest):
    """The claim is real: it is what makes a message seen. The turn answers nothing."""
    done = agent_runtime_action(agent, RuntimeAction(action="claim", ref=ref, handed=handed, scope=scope))
    if done["claimed"]:
        launched.append(list(handed or []))
        agent_runtime_action(agent, RuntimeAction(action="release"))
        mod._turn_over(agent)


real_launch = mod._launch
mod._pending, mod._busy_count, mod._launch = pending, nobody_busy, fake_launch
mod._http = lambda: Http()
mod._service_headers = lambda: {}
mod.DRY_RUN = False


def tick():
    before = len(launched)

    async def go():
        await mod.tick()
        for _ in range(5):
            await asyncio.sleep(0)
    asyncio.run(go())
    return len(launched) - before


def ticks(n=10):
    return sum(tick() for _ in range(n))


# 1. a new message wakes at once
m1, m2 = mail(), mail()
check(tick() == 1 and launched[-1] == [m1, m2], "a new message wakes the agent at once, handed what is due")

# 2, 8. it answered one and left the other
answered(m1)
check(ticks() == 0, "the unread message does not wake it again on the following ticks")

# 3, 4. after the interval, a bounded number of times
age()
check(tick() == 1 and launched[-1] == [m2], "after the interval it is woken again for what it left unread")
check(ticks() == 0, "and not again on every tick after that")
age()
check(tick() == 1, "a second reminder comes after the next interval")
age()
check(tick() == 1, "and a third after the one after that")

# 5. then it stops, and says so once
woken = 0
for _ in range(5):
    age()
    woken += ticks(2)
check(woken == 0, "after the last allowed attempt it is not woken for that message again")
check(events.count("agent.wake_exhausted") == 1, "and the Runner says so once, as an event")
state = sql("SELECT status, tags FROM sn_core.message_state WHERE message_id = %s", (m2,), fetch=True)[0]
check(state[0] == "new" and "unhandled" in state[1], "the message stays open, tagged unhandled")

# 6. a newer message starts over, and does not bring the given-up one back
m3 = mail()
check(tick() == 1 and launched[-1] == [m3], "a newer message wakes it again at once, for itself alone")
age()
check(tick() == 1 and launched[-1] == [m3], "and what it then leaves unread gets its own reminders")

# 7. nothing unread
answered(m2, m3)
age()
check(ticks() == 0, "an agent with nothing unread is not woken")

# 9. the interval runs from the end of the turn
m4 = mail()
check(tick() == 1, "another message wakes it")
age(70, handed_only=True)
check(ticks() == 0, "a turn that has only just ended is left alone for the interval")
age(70)
check(tick() == 1 and launched[-1] == [m4], "and reminded once the interval has passed since the turn ended")
answered(m4)

# ---- The real _launch from here: what a wake's outcome does to the message.


async def runtime(_c, agent, action, **kw):
    return agent_runtime_action(agent, RuntimeAction(action=action, **kw))


async def inbox_state(_c, _agent):
    return set(), None


async def owed_answers(_c, _agent):
    return set(owed["ids"])


async def nothing(*_a, **_k):
    return None

owed = {"ids": []}
mod._runtime, mod._inbox_state, mod._owed_answers = runtime, inbox_state, owed_answers
mod._emit_woken = mod._waking_sender = mod.refresh_turn_ceiling = nothing
mod._close_what_woke_it = mod._check_budget_warnings = nothing


async def emit(_c, event_type, *_a, **_k):
    events.append(event_type)
mod._emit = emit
mod._agent_delegation_headers = lambda _agent: {}
mod.runner_call_timeout = lambda: 10.0
mod._launch = real_launch


def launch_due():
    """Every failure backoff has passed, and the poll says whether a wake is due."""
    mod._retry_after.clear()

    async def go():
        await mod.tick()
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            await task
    asyncio.run(go())


# A wake that never reached the model is not a message the agent left unread.
m5 = mail()
gateway.update(status=500, body={})
for _ in range(6):                       # about six minutes of the gateway being down
    launch_due()
    age()
tagged = sql("SELECT tags, handed_count FROM sn_core.message_state WHERE message_id = %s", (m5,), fetch=True)[0]
gateway.update(status=200, body={"content": "done", "final_answer": "done"})
woken_after = agents_with_pending_work()["agents"]
due_after = [a["due_ids"] for a in woken_after if a["slug"] == "hazel"]
check(due_after == [[m5]] and "unhandled" not in tagged[0],
      "failed wakes use up no reminders: after an outage the message is still due "
      "(handed_count=%s, tags=%s, due=%s)" % (tagged[1], tagged[0], due_after))
# A failed wake is never a verdict on the agent, and its pause grows up to a ceiling.
check(events.count("agent.broken") == 0, "failed wakes never mark the agent broken")
check(mod._retry_after["hazel"] - mod._time.monotonic() > mod.WAKE_RETRY_BACKOFF,
      "the pause before the next try has grown")
answered(m5)
sql("UPDATE sn_core.agent_runtime SET status = 'free'")
mod._wake_failures.clear()
m7 = mail()
gateway.update(status=500, body={})
del wakes[:]
launch_due()
for _ in range(10):                      # no clearing of the backoff here: 5 s ticks
    async def go():
        await mod.tick()
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            await task
    asyncio.run(go())
check(len(wakes) == 1, "a failed wake is not retried tick after tick, only after its backoff (%d wakes)" % len(wakes))

# A model that cannot run answers 200 with failed=true (an agent on model "none"
# fails the same way once brin reports it): given back too, nothing tagged.
gateway.update(status=200, body={"failed": "no model"})
age()
launch_due()
back = sql("SELECT handed_count, handed_at IS NULL FROM sn_core.message_state WHERE message_id = %s", (m7,), fetch=True)[0]
check(back == (0, True), "a wake reported failed gives its message back too (%s)" % (back,))
gateway.update(status=200, body={"content": "done", "final_answer": "done"})
answered(m7)
sql("UPDATE sn_core.agent_runtime SET status = 'free'")
mod._wake_failures.clear()

# A turn whose CLI result was empty has no answer: brin returns its working notes
# as content and an empty final_answer. They are not the answer to anyone.
m6 = mail()
owed["ids"] = [m6]
NOTES = "I need to check an internal constraint before replying."
gateway.update(status=200, body={"content": NOTES, "final_answer": ""})
del final_texts[:]
launch_due()
check(final_texts == [], "working text is not posted as a wake's answer (posted: %r)" % (final_texts,))

# And the real answer still is.
answered(m6)
m8 = mail()
owed["ids"] = [m8]
gateway.update(status=200, body={"content": "The answer.", "final_answer": "The answer."})
launch_due()
check(final_texts == ["The answer."], "a wake's final answer is posted as its answer (posted: %r)" % (final_texts,))

print()
print("PASS" if not fails else "FAIL (%d)" % len(fails))
sys.exit(1 if fails else 0)
