"""The wake message, checked against the real schema in one transaction that is rolled back.

Nothing is committed, so the Runner never sees these rows and no agent is woken.
Run from the core-api tree under test (cwd), as the core-api user:

    venv/bin/python t5351.py <agent slug>

Prints one line per check and exits non-zero when any fails.
"""
import inspect
import sys

sys.path.insert(0, ".")

from db import cursor  # noqa: E402
from routes import notifications as N  # noqa: E402

AGENT = sys.argv[1]
TASK = 987654321          # no such task: the notify path does not look the task up
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name + ((" -- " + str(detail)) if detail else ""))
    if not ok:
        fails += 1


class Rollback(Exception):
    pass


def messages(cur):
    cur.execute(
        "SELECT m.id, m.content, m.metadata, ms.status FROM sn_core.messages m "
        "JOIN sn_core.message_state ms ON ms.message_id = m.id "
        "WHERE m.recipient = %s AND m.sender = 'system' AND m.metadata->>'task_id' = %s "
        "ORDER BY m.id", (AGENT, str(TASK)))
    return cur.fetchall()


def flag(cur):
    cur.execute("SELECT wake_requested_at FROM sn_core.agent_runtime WHERE agent_slug = %s", (AGENT,))
    row = cur.fetchone()
    return row["wake_requested_at"] if row else None


# The route body, run inside OUR transaction: the handler opens its own cursor,
# so the cursor factory it uses is replaced by one that hands back ours.
class _Ours:
    def __init__(self, cur):
        self.cur = cur

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


try:
    with cursor(commit=False) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        N.cursor = _Ours(cur)
        before_flag = flag(cur)

        def notify(event, title="Margherita for four"):
            body = N.NotifyRequest(assignee=AGENT, task_id=TASK, event=event, title=title, summary="in_progress")
            return N.notify(body, principal=None)

        r1 = notify("assigned")
        got = messages(cur)
        check("an assignment puts one message in the agent's inbox", len(got) == 1, len(got))
        if got:
            m = got[0]
            check("the message names the task", ("task #%d" % TASK) in m["content"], m["content"][:90])
            check("it says why", "assigned to you" in m["content"])
            check("it carries the task and the reason as data",
                  m["metadata"].get("task_id") == TASK and m["metadata"].get("reason") == "assigned"
                  and m["metadata"].get("source") == "planner", m["metadata"])
            check("it waits as a new message, which is what the Runner wakes for", m["status"] == "new", m["status"])
        check("the route still says whom it woke", r1.get("woke") == AGENT, r1)
        check("the bare wake flag is not raised", flag(cur) == before_flag, (before_flag, flag(cur)))

        notify("status_changed")
        check("a second notify for the same task while the first waits writes no second message",
              len(messages(cur)) == 1, len(messages(cur)))

        cur.execute("UPDATE sn_core.message_state SET status = 'processed', processed_at = now() "
                    "WHERE message_id = %s", (got[0]["id"],) if got else (0,))
        notify("status_changed")
        got2 = messages(cur)
        check("once the agent has closed it, the next event for the task writes a new one",
              len(got2) == 2 and got2[-1]["status"] == "new", [(g["id"], g["status"]) for g in got2])

        r = N.notify(N.NotifyRequest(assignee="no-such-agent-or-user", task_id=TASK, event="assigned",
                                     title="t", summary="s"), principal=None)
        check("an assignee that is not an agent gets no message", r.get("woke") is None, r)
        raise Rollback()
except Rollback:
    pass

src = inspect.getsource(N.notify)
check("the route no longer writes wake_requested_at", "wake_requested_at" not in src)
print("ALL PASS" if not fails else "%d FAILED" % fails)
sys.exit(1 if fails else 0)
