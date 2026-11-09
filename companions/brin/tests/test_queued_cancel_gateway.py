"""Brin forwards a desk turn ID and preserves the gateway's cancellation result."""
import asyncio
import unittest
from unittest.mock import patch

from core import executor


class QueuedCancelGatewayTest(unittest.TestCase):
    def test_cancelled_response_is_not_an_empty_assistant_answer(self):
        sent = []

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"cancelled": True}

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def post(self, url, json):
                sent.append((url, json))
                return FakeResponse()

        async def run():
            instance = executor.Executor.__new__(executor.Executor)
            with patch.object(executor.httpx, "AsyncClient", FakeClient), \
                 patch.object(executor, "gateway_call_timeout", lambda: 20):
                with self.assertRaises(executor.QueuedTurnCancelledError):
                    await instance._chat_response(
                        "session", "enriched", raw_content="raw",
                        client_turn_id="123e4567-e89b-42d3-a456-426614174000")

        asyncio.run(run())
        self.assertEqual(sent[0][1]["raw_content"], "raw")
        self.assertEqual(sent[0][1]["client_turn_id"],
                         "123e4567-e89b-42d3-a456-426614174000")


if __name__ == "__main__":
    unittest.main()
