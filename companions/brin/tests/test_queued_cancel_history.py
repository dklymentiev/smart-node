"""A cancelled queued desk message must not remain in Brin's local transcript."""
import json
import tempfile
import unittest
from pathlib import Path

from core.session_history import SessionHistory


class QueuedCancelHistoryTest(unittest.TestCase):
    def test_discard_uses_turn_id_not_message_text(self):
        with tempfile.TemporaryDirectory() as temp:
            history = SessionHistory(base_dir=temp)
            history.start_session("flint", temp, user_id="desk:one")
            history.append("flint", "desk:one", "user", "same text", client_turn_id="first")
            history.append("flint", "desk:one", "user", "same text", client_turn_id="second")
            self.assertTrue(history.discard_client_turn("flint", "desk:one", "first"))
            path = next(Path(temp).glob("*.json"))
            messages = json.loads(path.read_text())["messages"]
            self.assertEqual([(m["content"], m["client_turn_id"]) for m in messages],
                             [("same text", "second")])


if __name__ == "__main__":
    unittest.main()
