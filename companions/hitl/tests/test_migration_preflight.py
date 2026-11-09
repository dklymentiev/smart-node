"""Legacy state audit must catch broken identity and missing request evidence."""

import base64
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "migration_preflight", Path(__file__).resolve().parents[1] / "migration_preflight.py"
)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migration_snapshot  # noqa: E402


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class MigrationPreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "hitl.sqlite"
        self.state = root / "caller"
        self.state.mkdir()
        self.rid = "a" * 36
        self.secret = b"s" * 32 + b"p" * 32
        (self.state / "caller.json").write_text(json.dumps({
            "caller_id": "node", "secret_key": b64(self.secret)
        }))
        (self.state / "requests").mkdir()
        (self.state / "requests" / f"{self.rid}.json").write_text(json.dumps({
            "request_id": self.rid, "recipient_id": "owner"
        }))
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                PRAGMA user_version = 3;
                CREATE TABLE callers (id TEXT, pubkey TEXT);
                CREATE TABLE recipients (id TEXT);
                CREATE TABLE requests (id TEXT, caller_id TEXT, recipient_id TEXT,
                                       response_record TEXT, expires_at TEXT);
                CREATE TABLE blobs (id TEXT);
            """)
            db.execute("INSERT INTO callers VALUES (?, ?)", ("node", b64(self.secret[32:])))
            db.execute("INSERT INTO recipients VALUES ('owner')")
            db.execute("INSERT INTO requests VALUES (?, 'node', 'owner', '{}', '2026-01-01T00:00:00Z')",
                       (self.rid,))

    def test_matching_state_is_audited_without_secret_output(self):
        result = preflight.audit(self.db_path, [self.state])
        self.assertEqual(result["local_requests"], 1)
        self.assertEqual(result["answered"], 1)
        self.assertEqual(result["answered_unconsumed_local"], 1)
        self.assertEqual(result["active_unanswered"], 0)

    def test_mismatched_caller_key_is_refused(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE callers SET pubkey=?", (b64(b"x" * 32),))
        with self.assertRaisesRegex(ValueError, "caller key does not match"):
            preflight.audit(self.db_path, [self.state])

    def test_missing_request_evidence_is_refused(self):
        (self.state / "requests" / f"{self.rid}.json").unlink()
        with self.assertRaisesRegex(ValueError, "lack local caller state"):
            preflight.audit(self.db_path, [self.state])

    def test_snapshot_preserves_two_callers_and_unconsumed_answer(self):
        root = Path(self.tmp.name)
        service = root / "service"
        service.mkdir()
        self.db_path.rename(service / "hitl.sqlite")
        flow = root / "flow-caller"
        (flow / "requests").mkdir(parents=True)
        second = b"t" * 32 + b"q" * 32
        (flow / "caller.json").write_text(json.dumps({
            "caller_id": "flow", "secret_key": b64(second)
        }))
        rid2 = "b" * 36
        (flow / "requests" / f"{rid2}.json").write_text(json.dumps({
            "request_id": rid2, "recipient_id": "owner"
        }))
        db = sqlite3.connect(service / "hitl.sqlite")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO callers VALUES (?, ?)", ("flow", b64(second[32:])))
        db.execute("INSERT INTO requests VALUES (?, 'flow', 'owner', '{}', '2026-01-01T00:00:00Z')", (rid2,))
        db.commit()
        app = root / "app"
        app.mkdir()
        (app / "version").write_text("legacy")
        backup = root / "backup"
        try:
            result = migration_snapshot.snapshot(service, self.state, flow, app, backup)
        finally:
            db.close()
        self.assertEqual(result["local_requests"], 2)
        self.assertEqual(result["answered"], 2)
        self.assertEqual(result["consumed_local"], 0)
        self.assertEqual(result["answered_unconsumed_local"], 2)
        self.assertEqual(preflight.audit(backup / "hitl.sqlite.snapshot",
                                         [backup / "caller", backup / "flow-caller"]), result)
        self.assertEqual((backup / "flow-caller/requests" / f"{rid2}.json").read_text(),
                         (flow / "requests" / f"{rid2}.json").read_text())
        owners = json.loads((backup / "ownership.json").read_text())
        self.assertEqual(owners["state"]["."],
                         [service.stat().st_uid, service.stat().st_gid])
        self.assertEqual(owners["caller"]["caller.json"],
                         [(self.state / "caller.json").stat().st_uid,
                          (self.state / "caller.json").stat().st_gid])
        fresh = self.state / "requests" / "new-answer.json"
        fresh.write_text("new answer after cutover")
        with patch.object(migration_snapshot.os, "chown") as chown:
            migration_snapshot.restore_ownership(backup, service, self.state)
        self.assertTrue(any(args.args[0] == fresh for args in chown.call_args_list))


if __name__ == "__main__":
    unittest.main()
