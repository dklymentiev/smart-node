"""A busy Flint pipe accepts steer while send() reads stdout and exposes text early."""
import asyncio
import json
import sys
import time
import unittest

from request_usage_regression import cli


FAKE_FLINT = r'''
import json, sys
def send(value):
    print(json.dumps(value), flush=True)
first = json.loads(sys.stdin.readline())
assert first["type"] == "user"
send({"type": "text_delta", "delta": "Starting ", "session_id": "live"})
steer = json.loads(sys.stdin.readline())
assert steer["type"] == "control_request" and steer["request"]["subtype"] == "steer"
assert steer["request"]["text"] == "use the new direction"
rid = steer["request_id"]
send({"type": "control_response", "response": {"subtype": "success", "request_id": rid,
                                               "response": {"status": "accepted"}}})
send({"type": "steer_status", "request_id": rid, "status": "delivered", "session_id": "live"})
send({"type": "text_delta", "delta": "steered", "session_id": "live"})
assert json.loads(sys.stdin.readline()) == {"type": "finish"}
send({"type": "assistant", "message": {"id": "m1", "usage": {},
      "content": [{"type": "text", "text": "Starting steered"}]}})
send({"type": "result", "subtype": "success", "result": "Starting steered",
      "usage": {"input_tokens": 2, "output_tokens": 2}})
'''


class FlintLiveTurn(unittest.IsolatedAsyncioTestCase):
    async def test_steer_and_delta_arrive_before_result(self):
        sid = "live"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", FAKE_FLINT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cli._PROCESSES[sid] = proc
        cli._SESSION_CONVERSATION[sid] = "agent-flint:desk:test"
        cli._SESSION_RUNTIME[sid] = "flint-cli"
        backend = cli.FlintCliBackend()
        try:
            task = asyncio.create_task(backend.send(sid, [], "long task"))
            for _ in range(200):
                if cli.get_desk_activity("test").get("text") == "Starting ":
                    break
                await asyncio.sleep(0.01)
            activity = cli.get_desk_activity("test")
            self.assertTrue(activity["in_progress"])
            self.assertEqual(activity["text"], "Starting ")
            self.assertFalse(task.done())
            ack = await backend.steer(sid, "use the new direction")
            self.assertEqual(ack["status"], "accepted")
            for _ in range(200):
                activity = cli.get_desk_activity("test")
                if activity.get("text") == "Starting steered" and activity.get("steers", [{}])[0].get("status") == "delivered":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(activity["text"], "Starting steered")
            self.assertEqual(activity["steers"][0]["status"], "delivered")
            self.assertFalse(task.done())
            proc.stdin.write(b'{"type":"finish"}\n')
            await proc.stdin.drain()
            answer = await asyncio.wait_for(task, 3)
            self.assertEqual(answer.content, "Starting steered")
        finally:
            if proc.returncode is None:
                proc.terminate()
            await proc.wait()
            for table in (cli._PROCESSES, cli._SESSION_CONVERSATION, cli._SESSION_RUNTIME,
                          cli._TURN_TEXT, cli._TURN_STEERS):
                table.pop(sid, None)


if __name__ == "__main__":
    unittest.main()
