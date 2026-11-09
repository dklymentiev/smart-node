"""The live stream hands an event about a chat to that chat's owner only."""
import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import event_stream  # noqa: E402

OWNERS = {"d1": (1, 7), "d2": (1, 8)}


class FakeConn:
    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params): conn.desk = params[0]
            def fetchone(self): return OWNERS.get(conn.desk)
        return Cur()


class EventStreamTest(unittest.TestCase):
    def setUp(self):
        event_stream._subscribers.clear()
        event_stream._desk_owner.clear()

    def test_only_the_owner_of_the_chat_gets_it(self):
        async def run():
            mine, theirs = event_stream.subscribe(1, 7), event_stream.subscribe(1, 8)
            conn = FakeConn()
            # a desk entity, either spelling, and a reminder naming its desk
            event_stream._deliver(conn, json.dumps({"id": 1, "type": "agent.turn_completed", "entity_type": "desk", "entity_id": "d1"}))
            event_stream._deliver(conn, json.dumps({"id": 2, "type": "message.delivered", "entity_type": "desk", "entity_id": "desk:d1"}))
            event_stream._deliver(conn, json.dumps({"id": 3, "type": "reminder.sent", "entity_type": "reminder", "desk": "d1"}))
            # not about a chat: nobody
            event_stream._deliver(conn, json.dumps({"id": 4, "type": "auth.permission_granted", "entity_type": "permission"}))
            got = [mine.get_nowait() for _ in range(mine.qsize())]
            self.assertEqual([e["id"] for e in got], [1, 2, 3])
            self.assertTrue(all(e["desk"] == "d1" for e in got))
            self.assertTrue(theirs.empty())
        asyncio.run(run())

    def test_the_runner_hears_what_may_be_work(self):
        """A message, a wake request or a reminder: the Runner looks for work at
        once instead of on its next poll. Anything else is not its business."""
        async def run():
            runner = event_stream.subscribe_wakes()
            try:
                conn = FakeConn()
                for i, t in enumerate(["message.inbound", "agent.turn_completed",
                                       "agent.wake_requested", "auth.permission_granted",
                                       "reminder.sent"]):
                    event_stream._deliver(conn, json.dumps({"id": i, "type": t, "entity_type": "message"}))
                got = [runner.get_nowait()["type"] for _ in range(runner.qsize())]
                self.assertEqual(got, ["message.inbound", "agent.wake_requested", "reminder.sent"])
            finally:
                event_stream.unsubscribe_wakes(runner)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
