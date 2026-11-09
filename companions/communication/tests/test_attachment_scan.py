"""An attachment that arrives by mail can be opened by the agent it was sent to.

These tests exist because that stopped being true and nobody noticed for two
weeks. On 2026-09-21 GET /api/attachments/{id}/content began to serve only
files the scan stage approved, and gave the Studio chat upload that stage; the channel ingest
kept storing every file as `received` and never ran it. From then on every agent
got 403 for every attachment that came by email, including the distributor
reports a data agent exists to read.

They are about the DECISION the ingest writes on the row:

  * a file from a sender the contact gate trusts is stored approved
  * a file from anyone else is stored received (deny by default)
  * a gate that cannot answer leaves the file received, never approved
  * the ingest's copy of the rule answers exactly as core-api's does

Pure unit tests: no database. The connection and the contact gate are fakes,
so what is asserted is what the ingest decided and wrote.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))

CORE_API_SCAN = COMPONENT.parent / "core-api" / "attachment_scan.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# One rule, two copies
# --------------------------------------------------------------------------

def test_the_ingest_rule_is_core_api_rule():
    from common import attachment_scan as ours
    theirs = _load(CORE_API_SCAN, "core_api_attachment_scan")
    for source in (theirs.DESK_OWNER, "email", "telegram", "slack", "web-chat"):
        for trusted in (True, False):
            assert ours.verdict(source, trusted) == theirs.verdict(source, trusted), (source, trusted)
    assert (ours.APPROVED, ours.RECEIVED, ours.DESK_OWNER) == (theirs.APPROVED, theirs.RECEIVED, theirs.DESK_OWNER)


# --------------------------------------------------------------------------
# What the ingest writes
# --------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, log):
        self.log = log
        self._next = None

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        if "INSERT INTO messages" in sql:
            self._next = (42, 1)
        elif "INSERT INTO sn_core.message_attachments" in sql:
            self._next = (params[0],)
        else:
            self._next = None

    def fetchone(self):
        return self._next


class FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self.log)

    def close(self):
        pass


@pytest.fixture
def ingest(monkeypatch):
    from common import contacts, db
    log, events = [], []
    monkeypatch.setattr(db.psycopg2, "connect", lambda dsn: FakeConn(log))
    monkeypatch.setattr(db, "stamp_identity", lambda *a, **k: None)
    monkeypatch.setattr(db, "_emit", lambda actor, kind, msg, payload: events.append((kind, payload)))

    def run(gate):
        monkeypatch.setattr(contacts, "may_wake", gate)
        log.clear()
        events.clear()
        db.record_inbound_with_attachments(
            "email", "dana@node.test", "<m1@x>", None,
            "monique@distributor.test", "dana@node.test", "September report attached",
            attachments=[{"id": "a1", "filename": "Accounts Sold.xls",
                          "content_type": "application/vnd.ms-excel", "size_bytes": 40448,
                          "content_hash": "ab12", "storage_path": "2026/10/a1.bin",
                          "received_at": None}])
        rows = [p for sql, p in log if "INSERT INTO sn_core.message_attachments" in sql]
        assert len(rows) == 1
        return rows[0][9], rows[0][10], events
    return run


def test_a_trusted_sender_file_is_approved(ingest):
    asked = []

    def gate(channel, mailbox, sender):
        asked.append((channel, mailbox, sender))
        return True, "granted"
    status, reason, events = ingest(gate)
    assert (status, reason) == ("approved", "trusted-sender")
    # The gate is asked the inbound question: may this sender reach this mailbox.
    assert asked == [("email", "dana@node.test", "monique@distributor.test")]
    assert events == [("attachment.stored", {"attachment": "a1", "filename": "Accounts Sold.xls",
                                             "content_type": "application/vnd.ms-excel",
                                             "size_bytes": 40448, "status": "approved",
                                             "reason": "trusted-sender"})]


def test_a_stranger_file_is_received(ingest):
    status, reason, _ = ingest(lambda *a: (False, "no grant"))
    assert (status, reason) == ("received", "unscanned")


def test_a_gate_that_cannot_answer_approves_nothing(ingest):
    def broken(*a):
        raise RuntimeError("catalog unavailable")
    status, reason, _ = ingest(broken)
    assert (status, reason) == ("received", "unscanned")


def test_a_message_without_attachments_does_not_ask_the_gate(monkeypatch):
    from common import contacts, db
    log = []
    monkeypatch.setattr(db.psycopg2, "connect", lambda dsn: FakeConn(log))
    monkeypatch.setattr(db, "stamp_identity", lambda *a, **k: None)
    monkeypatch.setattr(contacts, "may_wake", lambda *a: pytest.fail("gate asked for no attachments"))
    msg_id, was_new = db.record_inbound_with_attachments(
        "email", "dana@node.test", "<m2@x>", None, "a@b.test", "dana@node.test", "hello")
    assert (msg_id, was_new) == (42, True)
