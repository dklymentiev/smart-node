"""The watcher's actor is the agent it polls core-api as.

The dispatcher starts minutes before the node's first agent exists. "No agent
yet" was cached for the life of the process, so on a fresh node nothing that
was relayed into a chat was ever delivered: a reminder fired, the agent
answered, and the chat stayed silent until a restart (2026-10-30).
"""
import asyncio
import os
import unittest

os.environ.setdefault("SN_CORE_API_URL", "http://127.0.0.1:1")
os.environ.setdefault("SN_OPERATOR_ACTOR", "owner")

from services import message_watcher as mw  # noqa: E402


class _Resp:
    def __init__(self, agents):
        self._agents = agents

    def raise_for_status(self):
        pass

    def json(self):
        return {"agents": self._agents}


class _Client:
    def __init__(self, nodes_agents):
        self.answers = list(nodes_agents)

    async def get(self, url, headers=None):
        return _Resp(self.answers.pop(0))


class ActorTest(unittest.TestCase):
    def setUp(self):
        mw._RESOLVED_ACTOR = None
        mw._ACTOR_COMPLAINED = False

    def test_an_agent_created_after_start_is_found(self):
        client = _Client([[], [{"slug": "foreman", "enabled": True}]])
        self.assertIsNone(asyncio.run(mw._actor(client)))
        self.assertEqual(asyncio.run(mw._actor(client)), "foreman")


if __name__ == "__main__":
    unittest.main()
