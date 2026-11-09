"""The credential wrapper with a gateway token: the node credential never
enters the agent's process, and without a frame the old path still works."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

WRAPPER = Path(__file__).resolve().parents[2] / "gateway-ops" / "claude-agent-wrapper"

FAKE_CLI = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "print(json.dumps({k: os.environ.get(k) for k in "
    "('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY')}"
    " | {'args': sys.argv[1:]}))\n"
)


def _frame(payload: bytes) -> bytes:
    return struct.pack("!Q", len(payload)) + payload


class RelayWrapperTest(unittest.TestCase):
    def _run(self, tmp, args, stdin, cred_file_text=None):
        binary = Path(tmp) / "fake-claude"
        binary.write_text(FAKE_CLI)
        binary.chmod(0o755)
        cred = Path(tmp) / "agent-runtime.env"
        if cred_file_text is not None:
            cred.write_text(cred_file_text)
        env = {"PATH": os.environ.get("PATH", ""), "CLI_PATH": str(binary),
               "SMART_NODE_AGENT_ENV": str(cred),
               "CLAUDE_CODE_OAUTH_TOKEN": "leaked-from-parent"}
        return subprocess.run([sys.executable, str(WRAPPER)] + args, input=stdin,
                              capture_output=True, env=env, timeout=20)

    def test_gateway_token_replaces_the_node_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = json.dumps({"ANTHROPIC_AUTH_TOKEN": "sn-token",
                                "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000/anthropic"}).encode()
            r = self._run(tmp, ["--sn-env-stdin", "--print"], _frame(frame))
            self.assertEqual(r.returncode, 0, r.stderr)
            seen = json.loads(r.stdout)
            self.assertEqual(seen["ANTHROPIC_AUTH_TOKEN"], "sn-token")
            self.assertEqual(seen["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8000/anthropic")
            self.assertIsNone(seen["CLAUDE_CODE_OAUTH_TOKEN"])
            self.assertIsNone(seen["ANTHROPIC_API_KEY"])
            self.assertEqual(seen["args"], ["--print"])   # the frame flag is consumed

    def test_without_a_frame_the_credential_file_is_read_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._run(tmp, ["--print"], b"", cred_file_text="CLAUDE_CODE_OAUTH_TOKEN=from-file\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            seen = json.loads(r.stdout)
            self.assertEqual(seen["CLAUDE_CODE_OAUTH_TOKEN"], "from-file")
            self.assertIsNone(seen["ANTHROPIC_AUTH_TOKEN"])

    def test_a_frame_without_a_token_falls_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = json.dumps({"SN_AGENT_SLUG": "agent-b"}).encode()
            r = self._run(tmp, ["--sn-env-stdin"], _frame(frame),
                          cred_file_text="ANTHROPIC_API_KEY=key-from-file\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            seen = json.loads(r.stdout)
            self.assertEqual(seen["ANTHROPIC_API_KEY"], "key-from-file")

    def test_a_frame_with_a_foreign_variable_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = json.dumps({"ANTHROPIC_AUTH_TOKEN": "sn-token", "LD_PRELOAD": "/x.so"}).encode()
            r = self._run(tmp, ["--sn-env-stdin"], _frame(frame))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(b"unsupported variable", r.stderr)


if __name__ == "__main__":
    unittest.main()
