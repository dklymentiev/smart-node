"""Real Flint through the Smart Node wrapper, with a local fake model API.

Run with SN_TEST_FLINT_BIN=/absolute/path/to/patched/flint and optionally
SN_TEST_NODE_BIN_DIR=/path/to/node-22/bin. No external key or DB is used.
"""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import select
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


WRAPPER = Path(__file__).resolve().parents[2] / "gateway-ops" / "claude-agent-wrapper"


class FlintStdioStandTest(unittest.TestCase):
    def test_steer_and_text_delta_inside_a_real_active_turn(self):
        binary = os.environ.get("SN_TEST_FLINT_BIN")
        if not binary:
            self.skipTest("set SN_TEST_FLINT_BIN to a reviewed Flint executable")
        first_piece_sent = threading.Event()
        release_first = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if len(requests) == 1:
                    first = {"id": "one", "choices": [{"delta": {"content": "Starting "}}]}
                    self.wfile.write(("data: " + json.dumps(first) + "\n\n").encode())
                    self.wfile.flush()
                    first_piece_sent.set()
                    if not release_first.wait(30):
                        return
                    tool = {"id": "one", "choices": [{"delta": {"tool_calls": [{
                        "index": 0, "id": "call_1", "function": {"name": "read_file",
                        "arguments": json.dumps({"path": str(fixture)})},
                    }]}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                    self.wfile.write(("data: " + json.dumps(tool) + "\n\ndata: [DONE]\n\n").encode())
                else:
                    answer = {"id": "two", "choices": [{"delta": {"content": "Followed direction"},
                        "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 4}}
                    self.wfile.write(("data: " + json.dumps(answer) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory(prefix="flint-live-stand-") as temp:
                home = Path(temp)
                (home / ".flint").mkdir()
                (home / ".flint" / "providers.json").write_text(json.dumps({"openrouter": {
                    "name": "fixture", "format": "openai", "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                    "authType": "bearer", "keyRequired": True, "defaultModel": "fixture/model", "headers": {},
                }}))
                agent = home / "agent"
                agent.mkdir()
                fixture = agent / "fixture.txt"
                fixture.write_text("fixture")
                (agent / "CLAUDE.md").write_text("You are a fixture agent.\n")
                node_bin = os.environ.get("SN_TEST_NODE_BIN_DIR")
                env = {"HOME": str(home), "FLINT_PATH": binary,
                       "PATH": (node_bin + os.pathsep if node_bin else "") + os.environ.get("PATH", "")}
                cmd = [sys.executable, str(WRAPPER), "--sn-runtime-flint", "--sn-env-stdin",
                       "--stdio", "--model", "fixture/model", "--session-id",
                       "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"]
                process = subprocess.Popen(cmd, cwd=agent, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, bufsize=0)
                private = json.dumps({"FLINT_PROVIDER": "openrouter",
                                      "OPENROUTER_API_KEY": "local-test-key",
                                      "FLINT_UPDATE_CHECK": "0"}).encode()
                process.stdin.write(struct.pack("!Q", len(private)) + private)
                process.stdin.write((json.dumps({"type": "user", "message": {
                    "role": "user", "content": "Read the file"}}) + "\n").encode())
                process.stdin.flush()

                def event():
                    ready, _, _ = select.select([process.stdout], [], [], 45)
                    self.assertTrue(ready, "timed out waiting for Flint stdout")
                    line = process.stdout.readline()
                    self.assertTrue(line, f"Flint stdout closed (rc={process.poll()})")
                    return json.loads(line)

                seen = []
                while not any(e.get("type") == "text_delta" for e in seen):
                    seen.append(event())
                self.assertTrue(first_piece_sent.is_set())
                self.assertFalse(any(e.get("type") == "result" for e in seen))
                process.stdin.write((json.dumps({"type": "control_request", "request_id": "steer-1",
                    "request": {"subtype": "steer", "text": "use the other file"}}) + "\n").encode())
                process.stdin.flush()
                while not any(e.get("type") == "control_response" for e in seen):
                    seen.append(event())
                self.assertEqual(next(e for e in seen if e.get("type") == "control_response")["response"]["response"]["status"], "accepted")
                release_first.set()
                while not any(e.get("type") == "result" for e in seen):
                    seen.append(event())
                self.assertEqual([e["status"] for e in seen if e.get("type") == "steer_status"], ["delivered"])
                self.assertEqual(len([e for e in seen if e.get("type") == "result"]), 1)
                self.assertIn("use the other file", json.dumps(requests[1]["messages"]))
                self.assertEqual(seen[-1]["result"], "Followed direction")
                process.stdin.close()
                self.assertEqual(process.wait(timeout=10), 0)
        finally:
            release_first.set()
            if process and process.poll() is None:
                process.kill()
                process.wait()
            if process:
                process.stdout.close()
                process.stderr.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_two_turns_and_resume_without_persisting_host_key(self):
        binary = os.environ.get("SN_TEST_FLINT_BIN")
        if not binary:
            self.skipTest("set SN_TEST_FLINT_BIN to a reviewed Flint executable")

        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.headers.get("Authorization"), body))
                n = len(requests)
                chunk = {
                    "id": f"fixture-{n}", "model": "fixture/model",
                    "choices": [{"delta": {"content": f"answer-{n}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                }
                payload = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="flint-stdio-stand-") as temp:
                home = Path(temp)
                data = home / ".flint"
                data.mkdir()
                (data / "providers.json").write_text(json.dumps({
                    "openrouter": {
                        "name": "fixture", "format": "openai",
                        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                        "authType": "bearer", "keyRequired": True,
                        "defaultModel": "fixture/model", "headers": {},
                    },
                }))
                agent = home / "agent"
                agent.mkdir()
                (agent / "CLAUDE.md").write_text("You are a fixture agent.\n")
                path = os.environ.get("PATH", "")
                node_bin = os.environ.get("SN_TEST_NODE_BIN_DIR")
                if node_bin:
                    path = node_bin + os.pathsep + path
                env = {"PATH": path, "HOME": str(home), "FLINT_PATH": binary}
                private = json.dumps({
                    "FLINT_PROVIDER": "openrouter",
                    "OPENROUTER_API_KEY": "local-test-key",
                    "FLINT_UPDATE_CHECK": "0",
                }).encode()
                frame = struct.pack("!Q", len(private)) + private
                session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

                def run(flag, questions):
                    lines = b"".join((json.dumps({
                        "type": "user", "message": {"role": "user", "content": q},
                    }) + "\n").encode() for q in questions)
                    cmd = [sys.executable, str(WRAPPER), "--sn-runtime-flint",
                           "--sn-env-stdin", "--stdio", "--model", "fixture/model",
                           flag, session_id]
                    process = subprocess.run(
                        cmd, input=frame + lines, cwd=agent, env=env,
                        capture_output=True, timeout=45,
                    )
                    self.assertEqual(process.returncode, 0, process.stderr[-1500:])
                    self.assertNotIn(b"local-test-key", process.stdout + process.stderr)
                    events = [json.loads(line) for line in process.stdout.splitlines()]
                    self.assertEqual(events[0]["agent"], "flint")
                    self.assertEqual(events[0]["cwd"], str(agent))
                    results = [event for event in events if event.get("type") == "result"]
                    self.assertEqual(len(results), len(questions), process.stderr[-1500:])
                    self.assertTrue(all(r["subtype"] == "success" for r in results))
                    self.assertTrue(all(r["usage"]["input_tokens"] > 0 for r in results))
                    return results

                first = run("--session-id", ["first", "second"])
                resumed = run("--resume", ["third"])
                self.assertEqual([r["result"] for r in first + resumed],
                                 ["answer-1", "answer-2", "answer-3"])
                self.assertEqual(len(requests), 3)
                self.assertTrue(all(auth == "Bearer local-test-key" for auth, _ in requests))
                self.assertTrue(all(body["model"] == "fixture/model" for _, body in requests))
                self.assertIn("You are a fixture agent", json.dumps(requests[0][1]))
                self.assertFalse((data / "keys.enc").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
