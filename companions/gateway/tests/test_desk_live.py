"""The chat's live line is pushed: a watcher of a desk gets a frame on
every change of its turn (start, text, end) without asking again."""
import asyncio
import json
import sys
import unittest

from request_usage_regression import cli


FAKE_FLINT = r'''
import json, sys
def send(value):
    print(json.dumps(value), flush=True)
json.loads(sys.stdin.readline())
send({"type": "text_delta", "delta": "Writing ", "session_id": "watched"})
assert json.loads(sys.stdin.readline()) == {"type": "finish"}
send({"type": "text_delta", "delta": "done", "session_id": "watched"})
send({"type": "assistant", "message": {"id": "m1", "usage": {},
      "content": [{"type": "text", "text": "Writing done"}]}})
send({"type": "result", "subtype": "success", "result": "Writing done",
      "usage": {"input_tokens": 2, "output_tokens": 2}})
'''


class DeskLive(unittest.IsolatedAsyncioTestCase):
    async def test_a_watcher_gets_each_change_of_the_turn(self):
        sid = "watched"
        frames = []

        async def watch():
            async for chunk in cli.desk_live("live-desk", ping_seconds=5):
                if chunk.startswith("data: "):
                    frames.append(json.loads(chunk[6:]))

        async def until(cond):
            for _ in range(300):
                if cond():
                    return
                await asyncio.sleep(0.01)
            self.fail(f"no such frame; got {frames}")

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", FAKE_FLINT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cli._PROCESSES[sid] = proc
        cli._SESSION_CONVERSATION[sid] = "agent-flint:desk:live-desk"
        cli._SESSION_RUNTIME[sid] = "flint-cli"
        watcher = asyncio.create_task(watch())
        try:
            await until(lambda: frames)
            self.assertFalse(frames[0]["in_progress"])           # nothing running yet
            task = asyncio.create_task(cli.FlintCliBackend().send(sid, [], "write"))
            await until(lambda: frames[-1].get("text") == "Writing ")
            self.assertTrue(frames[-1]["in_progress"])
            proc.stdin.write(b'{"type":"finish"}\n')
            await proc.stdin.drain()
            await asyncio.wait_for(task, 3)
            await until(lambda: frames[-1]["in_progress"] is False)  # the end is pushed too
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if proc.returncode is None:
                proc.terminate()
            await proc.wait()
            for table in (cli._PROCESSES, cli._SESSION_CONVERSATION, cli._SESSION_RUNTIME,
                          cli._TURN_TEXT, cli._TURN_STEERS):
                table.pop(sid, None)
        self.assertEqual(cli._LIVE_WATCHERS, set())               # a closed watcher leaves


STOPPED_FLINT = r'''
import json, sys
def send(value):
    print(json.dumps(value), flush=True)
json.loads(sys.stdin.readline())
send({"type": "text_delta", "delta": "Half a sentence", "session_id": "stopped"})
assert json.loads(sys.stdin.readline()) == {"type": "finish"}
send({"type": "result", "subtype": "error_during_execution", "result": "",
      "usage": {"input_tokens": 2, "output_tokens": 2}})
'''


class StoppedAnswer(unittest.IsolatedAsyncioTestCase):
    async def test_a_stopped_answer_keeps_the_text_already_written(self):
        sid = "stopped"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", STOPPED_FLINT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        cli._PROCESSES[sid] = proc
        cli._SESSION_CONVERSATION[sid] = "agent-flint:desk:stopped-desk"
        cli._SESSION_RUNTIME[sid] = "flint-cli"
        try:
            task = asyncio.create_task(cli.FlintCliBackend().send(sid, [], "write"))
            for _ in range(300):
                if cli._TURN_TEXT.get(sid):
                    break
                await asyncio.sleep(0.01)
            cli._INTERRUPTS[sid] = {"by": "owner"}      # what a Stop records
            proc.stdin.write(b'{"type":"finish"}\n')
            await proc.stdin.drain()
            answer = await asyncio.wait_for(task, 3)
            self.assertTrue(answer.stopped)
            self.assertEqual(answer.content, "Half a sentence")
        finally:
            if proc.returncode is None:
                proc.terminate()
            await proc.wait()
            for table in (cli._PROCESSES, cli._SESSION_CONVERSATION, cli._SESSION_RUNTIME,
                          cli._TURN_TEXT, cli._TURN_STEERS, cli._INTERRUPTS):
                table.pop(sid, None)


if __name__ == "__main__":
    unittest.main()


class ColleagueWake(unittest.TestCase):
    """A colleague @mentioned in a chat answers in a session of its own (a chat
    wake). The chat sees that turn live, and a Stop naming the colleague reaches
    it, while a Stop of the chat still stops the chat's own turn."""

    def setUp(self):
        import time
        now = time.monotonic()
        cli._SESSION_CONVERSATION.update({"own": "twine:desk:d1", "wake": "nora:runner-nora-1-1"})
        cli._WAKE_DESK["wake"] = "d1"
        cli._TURN_STARTED["wake"] = now

    def tearDown(self):
        for t in (cli._SESSION_CONVERSATION, cli._WAKE_DESK, cli._TURN_STARTED):
            for k in ("own", "wake"):
                t.pop(k, None)

    def test_the_chat_shows_the_colleague_working_for_it(self):
        a = cli.get_desk_activity("d1")
        self.assertTrue(a["in_progress"])
        self.assertTrue(a["colleague"])
        self.assertEqual(a["agent"], "nora")
        self.assertFalse(a["can_steer"])        # the chat's messages are not steered into her turn

    def test_a_stop_naming_the_colleague_reaches_her_turn(self):
        self.assertEqual(cli.desk_session_in_flight("d1", agent="nora", colleagues=True), "wake")
        self.assertIsNone(cli.desk_session_in_flight("d1"))            # steer: never a colleague
        import time
        cli._TURN_STARTED["own"] = time.monotonic()
        self.assertEqual(cli.desk_session_in_flight("d1", agent="twine", colleagues=True), "own")
        self.assertEqual(cli.get_desk_activity("d1")["agent"], "twine")  # the chat's own turn first
