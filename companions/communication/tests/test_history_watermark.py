"""A connected mailbox is not an inbox of everything it already holds.

These tests exist because the failure they cover was invisible until it had
already left the node: a real business inbox was bound to an agent, its whole
backlog was ingested as live inbound, and the agent answered it. One reply
reached an outside party.

They are deliberately about the DECISION, not about plumbing. Each one
constructs the state in which the wrong answer is possible and asserts the
right one:

  * a message older than the binding is recorded and never routed
  * a message newer than the binding is routed normally
  * a message whose arrival time cannot be established is NOT assumed to be new
  * re-binding the same mailbox does not move the watermark forward

Pure unit tests: no database, no IMAP. The transport and the registry writer
are replaced by fakes, so what is asserted is what the adapter DECIDED, which
is the thing that was wrong.
"""
import datetime
import sys
import types
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(COMPONENT))

UTC = datetime.timezone.utc
BOUND_AT = datetime.datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
LONG_BEFORE = datetime.datetime(2026, 5, 1, 9, 30, 0, tzinfo=UTC)
JUST_AFTER = datetime.datetime(2026, 8, 4, 12, 0, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# is_history -- the rule itself
# --------------------------------------------------------------------------

def test_older_than_the_binding_is_history():
    from common import binding
    assert binding.is_history(LONG_BEFORE, BOUND_AT) is True


def test_newer_than_the_binding_is_not_history():
    from common import binding
    assert binding.is_history(JUST_AFTER, BOUND_AT) is False


def test_exactly_at_the_binding_is_history():
    """The boundary is inclusive: a message already in the mailbox at the
    instant of binding was there before us, not because of us."""
    from common import binding
    assert binding.is_history(BOUND_AT, BOUND_AT) is True


def test_no_binding_means_not_history():
    """No watermark = no binding = nothing can route this message anyway.
    Calling it history here would be a second, quieter reason for silence."""
    from common import binding
    assert binding.is_history(LONG_BEFORE, None) is False


def test_unknown_arrival_time_is_history_not_new():
    """The failing direction matters. A message we cannot date is not thereby
    new; assuming it is new is exactly the assumption that mailed a stranger.
    Failing this way leaves a silent agent and a row that says why."""
    from common import binding
    assert binding.is_history(None, BOUND_AT) is True


# --------------------------------------------------------------------------
# _arrived_at -- where the arrival time comes from
# --------------------------------------------------------------------------

def _raw(date_header=None):
    lines = ["From: someone@example.com", "To: box@example.com",
             "Subject: hello", "Message-ID: <abc@example.com>"]
    if date_header:
        lines.append("Date: " + date_header)
    return ("\r\n".join(lines) + "\r\n\r\nbody\r\n").encode()


def test_internaldate_is_preferred_over_the_date_header():
    """INTERNALDATE is the server's own record and the sender cannot influence
    it. A backdated Date header must not be able to make an old message look
    new -- nor a new one look old."""
    import email
    from adapters.email.transport import _arrived_at
    envelope = b'1 (INTERNALDATE "04-Aug-2026 12:00:00 +0000" BODY[] {10}'
    msg = email.message_from_bytes(_raw("Tue, 1 Jan 2030 00:00:00 +0000"))
    got = _arrived_at(envelope, msg)
    assert got == BOUND_AT


def test_date_header_is_used_when_there_is_no_internaldate():
    import email
    from adapters.email.transport import _arrived_at
    msg = email.message_from_bytes(_raw("Fri, 1 May 2026 09:30:00 +0000"))
    assert _arrived_at(None, msg) == LONG_BEFORE


def test_no_usable_timestamp_returns_none_not_now():
    """None is the honest answer and the caller turns it into 'history'.
    Returning now() here would make every undatable message look brand new,
    which is the bug wearing a different hat."""
    import email
    from adapters.email.transport import _arrived_at
    msg = email.message_from_bytes(_raw())
    assert _arrived_at(b"1 (BODY[] {10}", msg) is None


def test_a_naive_date_header_is_read_as_utc_not_node_local():
    """A Date with no zone must not be shifted by wherever the node happens to
    run: hours in either direction is enough to cross a watermark."""
    import email
    from adapters.email.transport import _arrived_at
    msg = email.message_from_bytes(_raw("Fri, 1 May 2026 09:30:00 -0000"))
    got = _arrived_at(None, msg)
    assert got.utcoffset() == datetime.timedelta(0)
    assert got == LONG_BEFORE


# --------------------------------------------------------------------------
# run_once -- the decision as the adapter actually makes it
# --------------------------------------------------------------------------

class _FakeTransport:
    """Stands in for IMAP. Records what was flagged Seen, because a message
    that is recorded and not flagged comes back on the next pass."""

    def __init__(self, messages):
        self._messages = messages
        self.seen = []

    def poll_unseen(self):
        return self._messages

    def mark_seen(self, uid):
        self.seen.append(uid)


def _message(uid, arrived_at, subject="hello"):
    return {"uid": uid, "arrived_at": arrived_at,
            "message_id": "<%s@example.com>" % uid,
            "from_addr": "someone@example.com", "from_name": "Someone",
            "to_addr": "box@example.com", "subject": subject,
            "body": "text", "in_reply_to": "", "references": "",
            "auto_submitted": "", "precedence": "", "return_path": "",
            "x_failed_recipients": "", "list_id": ""}


@pytest.fixture
def adapter_with(monkeypatch):
    """Build the email adapter with its transport, registry and watermark
    lookup replaced, and hand back the calls it made."""
    def _build(messages, watermark):
        from adapters.email import adapter as email_adapter
        from common import binding

        recorded = []
        transport = _FakeTransport(messages)

        monkeypatch.setattr(email_adapter, "EmailTransport", lambda: transport)
        monkeypatch.setattr(email_adapter, "_channel_id", lambda: "box@example.com")
        monkeypatch.setattr(binding, "history_before",
                            lambda channel, ref: watermark)

        def _record_inbound(**kw):
            recorded.append(kw)
            return len(recorded)

        monkeypatch.setattr(email_adapter.db, "record_inbound", _record_inbound)
        return email_adapter, transport, recorded
    return _build


def test_backlog_is_recorded_but_marked_history(adapter_with):
    """The acceptance, in one line: a mailbox full of unanswered mail produces
    rows and produces nothing an agent will ever be handed."""
    email_adapter, transport, recorded = adapter_with(
        [_message("1", LONG_BEFORE), _message("2", LONG_BEFORE)], BOUND_AT)

    assert email_adapter.run_once() == 2
    assert [r["history"] for r in recorded] == [True, True]
    # Recorded, therefore readable: this is evidence, not a drop.
    assert all(r["content"] == "text" for r in recorded)
    # And the row carries the real arrival time, so the backlog does not sort
    # into the middle of today's conversation.
    assert [r["received_at"] for r in recorded] == [LONG_BEFORE, LONG_BEFORE]
    assert transport.seen == ["1", "2"]


def test_mail_arriving_after_the_binding_is_live(adapter_with):
    """The other half, and the half that makes the test able to fail: if the
    watermark silenced everything, this would go red."""
    email_adapter, _, recorded = adapter_with(
        [_message("3", JUST_AFTER)], BOUND_AT)

    assert email_adapter.run_once() == 1
    assert recorded[0]["history"] is False
    assert recorded[0]["received_at"] == JUST_AFTER


def test_one_pass_splits_backlog_from_live_mail(adapter_with):
    """Both in the same poll -- the case that actually happens on the first
    pass after connecting an inbox that is still receiving."""
    email_adapter, _, recorded = adapter_with(
        [_message("4", LONG_BEFORE), _message("5", JUST_AFTER)], BOUND_AT)

    assert email_adapter.run_once() == 2
    assert [r["history"] for r in recorded] == [True, False]


def test_undatable_message_is_not_routed(adapter_with):
    email_adapter, _, recorded = adapter_with(
        [_message("6", None)], BOUND_AT)

    assert email_adapter.run_once() == 1
    assert recorded[0]["history"] is True


def test_unbound_mailbox_routes_normally(adapter_with):
    """No binding, no watermark: behaviour is unchanged. The guard must not
    quietly become a second reason for a message to go nowhere."""
    email_adapter, _, recorded = adapter_with(
        [_message("7", LONG_BEFORE)], None)

    assert email_adapter.run_once() == 1
    assert recorded[0]["history"] is False


def test_the_verdict_is_stamped_on_the_immutable_row(adapter_with):
    """messages is append-only, so what is written at ingest is the permanent
    answer to 'why was this never answered'. The binding row can move on."""
    email_adapter, _, recorded = adapter_with(
        [_message("8", LONG_BEFORE)], BOUND_AT)

    email_adapter.run_once()
    meta = recorded[0]["metadata"]
    assert meta["history"] is True
    assert meta["history_before"] == BOUND_AT.isoformat()


# --------------------------------------------------------------------------
# record_inbound -- the row and its state are settled together
# --------------------------------------------------------------------------

def test_history_row_is_never_committed_as_new(monkeypatch):
    """Both consumers scan for status='new'. A row committed as 'new' and
    corrected a moment later is a row they may already have picked up, so the
    insert and the settle must share one transaction."""
    from common import db

    statements = []

    class _Cur:
        def execute(self, sql, args=None):
            statements.append((" ".join(sql.split()), args))

        def fetchone(self):
            return (42,)

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            statements.append(("CLOSE", None))

    monkeypatch.setattr(db.psycopg2, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(db, "_emit", lambda *a, **k: None)

    db.record_inbound("email", "box@example.com", "<x@y>", "t",
                      "them@example.com", "box@example.com", "hi",
                      history=True)

    sql = [s for s, _ in statements]
    assert any(s.startswith("INSERT INTO messages") for s in sql)
    settle = [(s, a) for s, a in statements if s.startswith("UPDATE message_state")]
    assert len(settle) == 1, "the state row must be settled exactly once"
    assert settle[0][1] == ("history", 42)
    # The settle happens before the connection is handed back, i.e. inside the
    # same `with conn:` block that commits the insert.
    assert sql.index("CLOSE") > sql.index(settle[0][0])


def test_a_normal_inbound_row_is_left_alone(monkeypatch):
    """The guard must be inert when it does not apply -- no UPDATE at all, so
    a live message keeps the seed state the trigger gave it."""
    from common import db

    statements = []

    class _Cur:
        def execute(self, sql, args=None):
            statements.append(" ".join(sql.split()))

        def fetchone(self):
            return (43,)

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def close(self):
            pass

    monkeypatch.setattr(db.psycopg2, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(db, "_emit", lambda *a, **k: None)

    db.record_inbound("email", "box@example.com", "<z@y>", "t",
                      "them@example.com", "box@example.com", "hi")

    assert not any(s.startswith("UPDATE message_state") for s in statements)
