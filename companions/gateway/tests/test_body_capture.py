"""Raw body capture: off by default, private files, no credential, a clock, never fatal."""
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

import body_capture as bc


class BodyCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (bc.ENABLED, bc.WINDOW_SECONDS, bc.DIR, bc._deadline, bc._swept_at)
        bc.ENABLED, bc.WINDOW_SECONDS, bc.DIR = True, 3600, self.tmp / "cap"
        bc._deadline, bc._swept_at = None, 0.0

    def tearDown(self):
        bc.ENABLED, bc.WINDOW_SECONDS, bc.DIR, bc._deadline, bc._swept_at = self.saved

    def test_off_means_no_directory_at_all(self):
        bc.ENABLED = False
        self.assertIsNone(bc.begin({"id": "a1"}, "POST", "https://x/v1/messages", {}, b"{}"))
        self.assertFalse(bc.DIR.exists())

    def test_a_call_is_one_private_directory_with_both_bodies(self):
        rec = {"id": "c0ffee", "agent": "agent-a", "model": "m", "provider": "p"}
        cap = bc.begin(rec, "POST", "https://x/v1/messages",
                       {"Authorization": "Bearer SECRET", "x-api-key": "SECRET2",
                        "anthropic-version": "2023-06-01"}, b'{"prompt":"hi"}')
        bc.note(cap, "answer", 200, {"content-type": "text/event-stream", "set-cookie": "SECRET3"})
        bc.note(cap, "chunk", b"event: message_start\n")
        bc.note(cap, "chunk", b"event: message_stop\n")
        bc.note(cap, "finish", "ok", 200, 12)
        d = bc.DIR / "c0ffee"
        self.assertEqual((d / "request.body").read_bytes(), b'{"prompt":"hi"}')
        self.assertEqual((d / "response.body").read_bytes(), b"event: message_start\nevent: message_stop\n")
        meta = (d / "meta.json").read_text()
        for secret in ("SECRET", "SECRET2", "SECRET3"):
            self.assertNotIn(secret, meta)
        self.assertIn('"agent": "agent-a"', meta)
        self.assertIn('"outcome": "ok"', meta)
        self.assertEqual(stat.S_IMODE(os.stat(bc.DIR).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for f in d.iterdir():
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600, f.name)

    def test_the_window_closes(self):
        self.assertIsNotNone(bc.begin({"id": "in"}, "POST", "u", {}, b"x"))
        bc._deadline = time.time() - 1
        self.assertIsNone(bc.begin({"id": "late"}, "POST", "u", {}, b"x"))
        self.assertFalse((bc.DIR / "late").exists())

    def test_a_restart_keeps_the_same_window(self):
        bc.begin({"id": "one"}, "POST", "u", {}, b"x")
        first = float((bc.DIR / bc.MARKER).read_text())
        bc._deadline = None            # the process restarted
        bc.enabled()
        self.assertEqual(bc._deadline, first)

    def test_old_calls_are_swept(self):
        bc.begin({"id": "old"}, "POST", "u", {}, b"x")
        old = time.time() - 2 * bc.WINDOW_SECONDS
        os.utime(bc.DIR / "old", (old, old))
        bc._swept_at = 0.0
        bc.begin({"id": "new"}, "POST", "u", {}, b"x")
        self.assertFalse((bc.DIR / "old").exists())
        self.assertTrue((bc.DIR / "new").exists())
        self.assertTrue((bc.DIR / bc.MARKER).exists())

    def test_capture_never_fails_the_call(self):
        blocker = self.tmp / "file"
        blocker.write_text("not a directory")
        bc.DIR = blocker / "cap"       # cannot be created
        self.assertIsNone(bc.begin({"id": "x"}, "POST", "u", {}, b"x"))
        bc.note(None, "chunk", b"x")   # no capture, no error
        cap = bc.begin.__globals__["Capture"].__new__(bc.Capture)
        bc.note(cap, "chunk", b"x")    # a broken capture swallows its own error


if __name__ == "__main__":
    unittest.main()
