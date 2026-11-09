"""Two notifies for one task at once write one message. Real schema, nothing committed.

One transaction writes the wake message and holds it open; a second one, on its
own connection, asks for the same (agent, task). It has to WAIT for the first.
Both are rolled back. Run from the core-api tree under test, as the core-api user:

    venv/bin/python t5351b.py <agent slug>
"""
import sys
import threading
import time

sys.path.insert(0, ".")

from db import cursor  # noqa: E402
from routes import notifications as N  # noqa: E402

AGENT, TASK = sys.argv[1], 987654322
body = N.NotifyRequest(assignee=AGENT, task_id=TASK, event="assigned", title="t", summary="s")
second = {"done_at": None, "error": None}


class Rollback(Exception):
    pass


def other():
    try:
        with cursor(commit=False) as cur:
            cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
            cur.execute("SET LOCAL lock_timeout = '8s'")
            N._task_wake_message(cur, AGENT, 1, body)
            second["done_at"] = time.time()
            raise Rollback()
    except Rollback:
        pass
    except Exception as exc:
        second["error"] = exc


ok = True
try:
    with cursor(commit=False) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        N._task_wake_message(cur, AGENT, 1, body)
        t = threading.Thread(target=other)
        t.start()
        time.sleep(2.0)
        waited = second["done_at"] is None and second["error"] is None
        print(("PASS " if waited else "FAIL ") + "a second notify for the same task waits while the first is still writing")
        ok = ok and waited
        released = time.time()
        raise Rollback()
except Rollback:
    pass
t.join(15)
went_on = second["done_at"] is not None and second["done_at"] >= released
print(("PASS " if went_on else "FAIL ") + "and goes on once the first is finished"
      + ("" if went_on else " -- %r" % (second,)))
ok = ok and went_on
print("ALL PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
