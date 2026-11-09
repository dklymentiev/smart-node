"""A turn refused because the owner stopped the agent is named as such, not
read as a gateway fault."""
import asyncio
import unittest
from unittest.mock import patch

from core import executor


class AgentStoppedGatewayTest(unittest.TestCase):
    def _run(self, detail):
        class FakeResponse:
            status_code = 409

            def json(self):
                return {"detail": detail}

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def post(self, url, json):
                return FakeResponse()

        async def run():
            instance = executor.Executor.__new__(executor.Executor)
            with patch.object(executor.httpx, "AsyncClient", FakeClient), \
                 patch.object(executor, "gateway_call_timeout", lambda: 20):
                await instance._chat_response("session", "enriched", raw_content="raw")

        return run()

    def test_a_stopped_agent_is_named(self):
        with self.assertRaises(executor.AgentStoppedError) as caught:
            asyncio.run(self._run({"error": "agent_stopped", "agent": "agent-b",
                                   "reason": "agent agent-b is stopped; enable it to continue"}))
        self.assertEqual(caught.exception.agent, "agent-b")
        self.assertIn("enable it", str(caught.exception))

    def test_any_other_409_is_still_a_gateway_error(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(self._run("session is busy"))


if __name__ == "__main__":
    unittest.main()
