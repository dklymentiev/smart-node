"""The quoted history ends before the person's message begins.

The block of previous exchanges was prepended with no end: the person's new
message ran on from its last line ("You: Reminder: check-..."), and the model
answered "your reminder fired" to "remind me in 1 minute to stretch my legs"
without setting one (fresh node, 2026-10-30).
"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("CORE_API_URL", "http://127.0.0.1:1")

import core.executor as ex  # noqa: E402


class _Resp:
    status_code = 200

    def json(self):
        return {"messages": [
            {"from_person": True, "channel": "web-chat", "ago": "6 min ago", "text": "remind me in 1 minute to stand up"},
            {"from_person": False, "channel": "web-chat", "ago": "5 min ago", "text": "Reminder: stand up"},
        ]}


class _Client:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return _Resp()


class HistoryBoundaryTest(unittest.TestCase):
    def test_the_message_does_not_run_on_from_the_last_quoted_line(self):
        e = ex.Executor.__new__(ex.Executor)
        with mock.patch.object(ex.httpx, "AsyncClient", _Client), \
                mock.patch.object(ex, "_service_headers", lambda: {}):
            block = asyncio.run(e._cross_channel_block("owner", agent_slug="foreman"))
        sent = block + "remind me in 1 minute to stretch my legs"
        last_quoted = "You: Reminder: stand up"
        between = sent[sent.index(last_quoted) + len(last_quoted):sent.index("remind me in 1 minute to stretch")]
        self.assertIn("[end of previous exchanges]", between)


if __name__ == "__main__":
    unittest.main()
