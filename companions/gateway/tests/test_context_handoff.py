"""Unit coverage for the provider-neutral context handoff lifecycle."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import context_handoff
import sessions_v2_routes


class FakeSessionService:
    def __init__(self, status="active"):
        self.usage = []
        self.appended = []
        self.discarded = []
        self.status = status

    def discard_message(self, session_id, message_id):
        self.discarded.append((session_id, message_id))

    def get_session(self, _session_id):
        return {"status": self.status, "tenant_id": 1, "agent_id": 7,
                "is_opaque": True, "metadata": {"conversation": "desk:abc"}}

    def get_history(self, _session_id):
        return [{"role": "user", "content": "old fact", "is_injected": False}]

    def record_usage(self, session_id, **usage):
        self.usage.append((session_id, usage))

    def append_message(self, session_id, **message):
        self.appended.append((session_id, message))
        return 1


class FakeBackend:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send(self, session_id, history, message):
        self.sent.append((session_id, history, message))
        return SimpleNamespace(
            content="## Continue\nRemember the exact old fact and finish the pending task.",
            usage={
                "input_tokens": 2,
                "cache_read_tokens": 121000,
                "cache_write_tokens": 3,
                "output_tokens": 40,
                "cost_usd": 0.12,
                "estimated": False,
                "requests": [],
            },
        )

    async def close(self, session_id):
        self.closed.append(session_id)


IDENTITY = {
    "agent_id": 7,
    "agent_slug": "integration-agent",
    "workspace": "integration-agent",
    "conversation": "desk:abc",
    "threshold_tokens": 120000,
    "enabled": True,
    "scope_type": "agent",
}

SESSION = {
    "tenant_id": 1,
    "agent_id": 7,
    "is_opaque": True,
    "metadata": {"conversation": "desk:abc"},
}

OVER_THRESHOLD = {"input_tokens": 1, "cache_read_tokens": 120000, "cache_write_tokens": 1000}


@pytest.fixture(autouse=True)
def _clean_module_state():
    context_handoff._HANDOFF_BACKOFF.clear()
    context_handoff._HANDOFF_IN_PROGRESS.clear()
    sessions_v2_routes._TURN_LOCKS.clear()
    sessions_v2_routes._TURN_WAITERS.clear()
    sessions_v2_routes._PENDING_TURNS.clear()
    sessions_v2_routes._CANCEL_TOMBSTONES.clear()
    yield
    context_handoff._HANDOFF_BACKOFF.clear()
    context_handoff._HANDOFF_IN_PROGRESS.clear()
    sessions_v2_routes._TURN_LOCKS.clear()
    sessions_v2_routes._TURN_WAITERS.clear()
    sessions_v2_routes._PENDING_TURNS.clear()
    sessions_v2_routes._CANCEL_TOMBSTONES.clear()


def test_context_tokens_uses_latest_response_components_only():
    assert context_handoff.context_tokens({
        "input_tokens": 17,
        "cache_read_tokens": 119000,
        "cache_write_tokens": 2000,
        "output_tokens": 999999,
        "calls": 50,
    }) == 121017


# the claude-cli turn usage is the CLI `result` total, summed over every
# provider request of the turn. The context is the LAST request. Shape taken
# from Agent B's session 0a386c9d on example-node: requests of ~60k each, the sum
# recorded as 481,557 against a 200k threshold, real context 63,065.
def _req(context):
    return {"input_tokens": 5, "cache_read_tokens": context - 1005,
            "cache_write_tokens": 1000, "output_tokens": 300}


MULTI_REQUEST_TURN = {
    "input_tokens": 50, "cache_read_tokens": 471507, "cache_write_tokens": 10000,
    "output_tokens": 3000,
    "requests": [_req(40000), _req(48000), _req(55000), _req(60000), _req(63065)],
}


def test_threshold_uses_the_last_request_not_the_turn_sum(monkeypatch):
    identity = dict(IDENTITY, threshold_tokens=200000)
    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: identity)
    events = []
    monkeypatch.setattr(context_handoff, "emit_event",
                        lambda kind, sid, payload: events.append(kind) or 1)
    backend = FakeBackend()

    result = asyncio.run(context_handoff.maybe_handoff(
        FakeSessionService(), "s-sum", SESSION, backend, MULTI_REQUEST_TURN,
    ))

    assert result is None
    assert events == []
    assert backend.sent == []


def test_last_request_over_threshold_hands_off_and_records_its_size(monkeypatch):
    usage = dict(MULTI_REQUEST_TURN, requests=MULTI_REQUEST_TURN["requests"] + [_req(125000)])
    events = []
    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: dict(IDENTITY))
    monkeypatch.setattr(context_handoff, "emit_event",
                        lambda kind, sid, payload: events.append((kind, payload)) or 1)

    async def write(content, identity, session_id):
        return "doc_last"

    seen = {}

    def finalize(session_id, guid, before, identity, handoff_tokens):
        seen["before"] = before
        return 45

    monkeypatch.setattr(context_handoff, "_write_handoff", write)
    monkeypatch.setattr(context_handoff, "_finalize_reset", finalize)
    monkeypatch.setattr(context_handoff.budget, "record_spend", lambda **kw: None)

    result = asyncio.run(context_handoff.maybe_handoff(
        FakeSessionService(), "s-last", SESSION, FakeBackend(), usage,
    ))

    assert result["handoff_doc_id"] == "doc_last"
    assert seen["before"] == 125000
    crossed = [p for k, p in events if k == "session.context_threshold_crossed"]
    assert crossed and crossed[0]["context_tokens"] == 125000


def test_injected_handoff_is_prepended_only_to_first_real_turn():
    injected = {"role": "system", "content": "remember juniper", "is_injected": True}
    first = [injected, {"role": "user", "content": "hello", "is_injected": False}]
    second = first + [{"role": "assistant", "content": "hi", "is_injected": False},
                      {"role": "user", "content": "again", "is_injected": False}]
    assert "remember juniper" in context_handoff.content_for_first_turn(first, "hello")
    assert context_handoff.content_for_first_turn(second, "again") == "again"


def test_threshold_crossing_writes_mesh_then_finalizes_and_closes(monkeypatch):
    service = FakeSessionService()
    backend = FakeBackend()
    events = []
    order = []
    spends = []

    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: dict(IDENTITY))
    monkeypatch.setattr(context_handoff, "emit_event",
                        lambda kind, sid, payload: events.append((kind, sid, payload)) or 11)

    async def write(content, identity, session_id):
        order.append("mesh")
        assert "exact old fact" in content
        assert identity["workspace"] == "integration-agent"
        return "doc_deadbeef"

    def finalize(session_id, guid, before, identity, handoff_tokens):
        order.append("finalize")
        assert guid == "doc_deadbeef"
        assert before == 121001
        assert handoff_tokens == 121005
        return 44

    monkeypatch.setattr(context_handoff, "_write_handoff", write)
    monkeypatch.setattr(context_handoff, "_finalize_reset", finalize)
    monkeypatch.setattr(context_handoff.budget, "record_spend",
                        lambda **kw: spends.append(kw))

    result = asyncio.run(context_handoff.maybe_handoff(
        service, "session-1", SESSION, backend, OVER_THRESHOLD,
    ))

    assert order == ["mesh", "finalize"]
    assert backend.closed == ["session-1"]
    assert result["handoff_doc_id"] == "doc_deadbeef"
    assert events[0][0] == "session.context_threshold_crossed"
    assert service.usage and spends[0]["cost_usd"] == 0.12


def test_handoff_generation_is_not_cancelled_by_a_gateway_deadline(monkeypatch):
    """Only the backend may time out a CLI turn because it also kills the child.

    This deliberately installs the old setting with a deadline shorter than the
    fake provider call. Before c7740's fix, maybe_handoff read that setting via
    asyncio.wait_for, cancelled send(), and left the session pipe out of phase.
    """
    service = FakeSessionService()
    backend = FakeBackend()

    async def slow_send(session_id, history, message):
        await asyncio.sleep(0.01)
        return await FakeBackend.send(backend, session_id, history, message)

    backend.send = slow_send
    monkeypatch.setattr(
        context_handoff.config,
        "CONTEXT_HANDOFF_GENERATION_TIMEOUT",
        0.001,
        raising=False,
    )
    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: dict(IDENTITY))
    monkeypatch.setattr(context_handoff, "emit_event", lambda *_a, **_kw: 1)
    monkeypatch.setattr(context_handoff, "_write_handoff", _async_doc)
    monkeypatch.setattr(context_handoff, "_finalize_reset", lambda *_a: 9)
    monkeypatch.setattr(context_handoff.budget, "record_spend", lambda **_kw: None)

    result = asyncio.run(context_handoff.maybe_handoff(
        service, "session-no-outer-timeout", SESSION, backend, OVER_THRESHOLD,
    ))

    assert result["handoff_doc_id"] == "doc_no_outer_timeout"
    assert backend.closed == ["session-no-outer-timeout"]


async def _async_doc(*_args, **_kwargs):
    return "doc_no_outer_timeout"


def test_mesh_failure_keeps_session_open_and_is_loud(monkeypatch):
    service = FakeSessionService()
    backend = FakeBackend()
    events = []
    finalized = []

    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: dict(IDENTITY))
    monkeypatch.setattr(context_handoff, "emit_event",
                        lambda kind, sid, payload: events.append((kind, payload)) or 1)

    async def fail_write(*_args, **_kwargs):
        raise RuntimeError("mesh unavailable")

    monkeypatch.setattr(context_handoff, "_write_handoff", fail_write)
    monkeypatch.setattr(context_handoff, "_finalize_reset",
                        lambda *_args: finalized.append(True))
    monkeypatch.setattr(context_handoff.budget, "record_spend", lambda **_kw: None)

    result = asyncio.run(context_handoff.maybe_handoff(
        service, "session-2", SESSION, backend,
        {"input_tokens": 0, "cache_read_tokens": 120000, "cache_write_tokens": 0},
    ))

    assert result is None
    assert finalized == []
    assert backend.closed == []
    assert [kind for kind, _ in events] == [
        "session.context_threshold_crossed", "session.handoff_failed"
    ]
    assert "mesh unavailable" in events[-1][1]["error"]
    assert context_handoff.is_handoff_in_progress("session-2") is False


def test_failed_handoff_is_not_retried_on_every_following_turn(monkeypatch):
    """A memory outage must not double the spend of every turn that follows."""
    service = FakeSessionService()
    backend = FakeBackend()

    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: dict(IDENTITY))
    monkeypatch.setattr(context_handoff, "emit_event", lambda *_a, **_kw: 1)
    monkeypatch.setattr(context_handoff.budget, "record_spend", lambda **_kw: None)

    async def fail_write(*_args, **_kwargs):
        raise RuntimeError("mesh unavailable")

    monkeypatch.setattr(context_handoff, "_write_handoff", fail_write)

    asyncio.run(context_handoff.maybe_handoff(
        service, "session-backoff", SESSION, backend, OVER_THRESHOLD))
    assert len(backend.sent) == 1

    # Same session, same context size, next turn: no second generation.
    asyncio.run(context_handoff.maybe_handoff(
        service, "session-backoff", SESSION, backend, OVER_THRESHOLD))
    assert len(backend.sent) == 1

    # Grown far enough past the failure point to be worth another attempt.
    grown = dict(OVER_THRESHOLD, cache_read_tokens=200000)
    asyncio.run(context_handoff.maybe_handoff(
        service, "session-backoff", SESSION, backend, grown))
    assert len(backend.sent) == 2


def test_budget_accounting_failure_does_not_cancel_handoff(monkeypatch):
    service = FakeSessionService()
    backend = FakeBackend()

    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: dict(IDENTITY))
    monkeypatch.setattr(context_handoff, "emit_event", lambda *_a, **_kw: 1)

    async def write(*_args, **_kwargs):
        return "doc_budget_safe"

    monkeypatch.setattr(context_handoff, "_write_handoff", write)
    monkeypatch.setattr(context_handoff, "_finalize_reset", lambda *_a: 9)
    monkeypatch.setattr(
        context_handoff.budget, "record_spend",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("budget database busy")),
    )

    result = asyncio.run(context_handoff.maybe_handoff(
        service, "session-budget", SESSION, backend,
        {"input_tokens": 120000, "cache_read_tokens": 0, "cache_write_tokens": 0},
    ))

    assert result["handoff_doc_id"] == "doc_budget_safe"
    assert backend.closed == ["session-budget"]
    assert context_handoff.is_handoff_in_progress("session-budget") is False


def test_below_threshold_does_nothing(monkeypatch):
    service = FakeSessionService()
    backend = FakeBackend()
    monkeypatch.setattr(context_handoff, "_identity_and_limit", lambda _s: dict(IDENTITY))
    result = asyncio.run(context_handoff.maybe_handoff(
        service, "session-3", SESSION, backend,
        {"input_tokens": 999, "cache_read_tokens": 118000, "cache_write_tokens": 999},
    ))
    assert result is None
    assert backend.sent == []
    assert backend.closed == []


def test_handoff_tags_are_lowercased_like_mesh_stores_them(monkeypatch):
    """Mesh lowercases tags on write and matches them exactly on read."""
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"guid": "doc_written"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def put(self, url, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(context_handoff.httpx, "AsyncClient", lambda **_kw: Client())
    identity = dict(IDENTITY, conversation="integration-agent:Ivan.Petrov@Example.COM")

    guid = asyncio.run(context_handoff._write_handoff("the handoff body", identity, "S-42"))

    assert guid == "doc_written"
    assert captured["json"]["tags"] == [
        "type:handoff",
        "agent:integration-agent",
        "session:s-42",
        "conversation:integration-agent:ivan.petrov@example.com",
    ]


def test_injection_reads_the_document_the_previous_session_recorded(monkeypatch):
    service = FakeSessionService()
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"guid": "doc_latest", "content": "The durable fact is juniper."}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(context_handoff.httpx, "AsyncClient", lambda **_kw: Client())
    monkeypatch.setattr(context_handoff, "_predecessor_handoff_guid",
                        lambda agent_id, conversation: "doc_latest")
    monkeypatch.setattr(context_handoff.db, "query", lambda *_a, **_kw: None)
    monkeypatch.setattr(context_handoff, "emit_event", lambda *_a, **_kw: 1)

    guid = asyncio.run(context_handoff.inject_latest_handoff(
        service, "new-session", {
            "agent_id": 7,
            "metadata": {
                "mesh_workspace": "integration-agent",
                "conversation": "integration-agent:Ivan.Petrov@Example.COM",
                "agent_slug": "integration-agent",
            },
        },
    ))

    assert guid == "doc_latest"
    assert captured["url"].endswith("/doc_latest")
    assert service.appended[0][1]["role"] == "system"
    assert service.appended[0][1]["is_injected"] is True
    assert "juniper" in service.appended[0][1]["content"]


def test_first_session_of_a_conversation_injects_nothing(monkeypatch):
    service = FakeSessionService()

    def no_http(*_a, **_kw):
        raise AssertionError("memory must not be called when nothing was reset")

    monkeypatch.setattr(context_handoff.httpx, "AsyncClient", no_http)
    monkeypatch.setattr(context_handoff, "_predecessor_handoff_guid",
                        lambda agent_id, conversation: None)

    guid = asyncio.run(context_handoff.inject_latest_handoff(
        service, "first-session", {
            "agent_id": 7,
            "metadata": {"mesh_workspace": "integration-agent", "conversation": "desk:abc",
                         "agent_slug": "integration-agent"},
        },
    ))
    assert guid is None


def test_manual_clean_is_a_barrier_to_older_handoffs(monkeypatch):
    captured = {}

    def query(sql, *_args, **_kwargs):
        captured["sql"] = sql
        return [{"guid": None, "manually_cleared": True}]

    monkeypatch.setattr(context_handoff.db, "query", query)
    assert context_handoff._predecessor_handoff_guid(7, "alex:desk:d-1") is None
    assert "metadata->>'manual_context_clear' = 'true'" in captured["sql"]


def test_reap_does_not_hide_the_earlier_handoff(monkeypatch):
    """The SQL skips ordinary terminal rows and keeps manual clean as a barrier."""
    captured = {}

    def query(sql, *_args, **_kwargs):
        captured["sql"] = sql
        # This is the row Postgres returns after filtering out a newer reaped
        # session with neither a handoff doc nor manual_context_clear.
        return [{"guid": "doc_before_reap", "manually_cleared": False}]

    monkeypatch.setattr(context_handoff.db, "query", query)
    assert context_handoff._predecessor_handoff_guid(7, "alex:desk:d-1") == "doc_before_reap"
    sql = captured["sql"]
    assert "metadata->>'handoff_doc_id' IS NOT NULL" in sql
    assert "OR metadata->>'manual_context_clear' = 'true'" in sql
    assert "NULLS LAST" in sql


def test_manual_clean_is_atomic_audited_and_writes_no_handoff(monkeypatch):
    statements = []

    class Cursor:
        rowcount = 1

        def execute(self, sql, params):
            statements.append((sql, params))

        def fetchone(self):
            return (77, "now")

    class Tx:
        def cursor(self):
            return Cursor()

    class CM:
        def __enter__(self):
            return Tx()

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(context_handoff.db, "with_transaction", lambda: CM())
    monkeypatch.setattr(context_handoff, "_ensure_actor", lambda cur, actor: statements.append(("actor", actor)))

    result = context_handoff.finalize_manual_clean(
        "s-clean",
        {
            "agent_id": 7,
            "metadata": {"conversation": "alex:desk:d-1"},
            "usage": {"requests": [{
                "input_tokens": 1000, "cache_read_tokens": 62000,
                "cache_write_tokens": 2000, "output_tokens": 999999,
            }]},
        },
        "owner",
        "studio_manual_clean",
    )

    assert result["status"] == "completed"
    assert result["before_context_tokens"] == 65000
    assert any("session.context_cleared" in sql for sql, _ in statements if isinstance(sql, str))
    assert not any("handoff" in sql.lower() for sql, _ in statements if isinstance(sql, str))
    event_params = next(params for sql, params in statements if isinstance(sql, str) and "session.context_cleared" in sql)
    assert event_params[0] == "user:owner"
    assert "studio_chat_history" in event_params[2]
    assert "provider_transcript_context" in event_params[2]


# -- route-level turn queue ----------------------------------------------------

def _route_turn(session_id, content, future, backend=None):
    return sessions_v2_routes._run_queued_turn_async(
        session_id, backend, content, 1, future,
    )


def test_cancel_one_of_two_identical_queued_messages(monkeypatch):
    service = FakeSessionService()
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)
    sent = []

    async def complete(_sid, _sess, _backend, _history, content, _mid):
        sent.append(content)
        return {"response": {"usage": {}}}

    async def no_handoff(*_args):
        return None

    monkeypatch.setattr(sessions_v2_routes, "_complete_turn_async", complete)
    monkeypatch.setattr(sessions_v2_routes.context_handoff, "maybe_handoff", no_handoff)

    async def main():
        sid = "s-cancel"
        did = str(uuid.uuid4())
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        loop = asyncio.get_running_loop()
        lock = sessions_v2_routes._turn_lock(sid)
        await lock.acquire()
        pending = []
        tasks = []
        for i, cid in enumerate(ids):
            future = loop.create_future()
            entry = {"desk_id": did, "client_turn_id": cid, "session_id": sid,
                     "message_id": i + 10, "future": future, "state": "queued", "counted": False}
            sessions_v2_routes._PENDING_TURNS[(did, cid)] = entry
            pending.append(entry)
            task = asyncio.create_task(sessions_v2_routes._run_queued_turn_async(
                sid, None, "same text", i + 10, future, entry))
            entry["task"] = task
            tasks.append(task)
        await asyncio.sleep(0)
        assert sessions_v2_routes._queued_turns(sid) == 2
        out = await sessions_v2_routes.cancel_queued_desk_turn(
            sessions_v2_routes.CancelQueuedRequest(desk_id=did, client_turn_id=ids[0]))
        assert out == {"status": "cancelled"}
        assert service.discarded == [(sid, 10)]
        assert pending[0]["future"].result()["cancelled"] is True
        assert sessions_v2_routes._queued_turns(sid) == 1
        lock.release()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert sent == ["same text"]
        assert sessions_v2_routes._queued_turns(sid) == 0
        assert not sessions_v2_routes._PENDING_TURNS

    asyncio.run(main())


def test_queued_cancel_rejects_a_turn_that_already_started(monkeypatch):
    service = FakeSessionService()
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)

    async def main():
        did, cid = str(uuid.uuid4()), str(uuid.uuid4())
        sessions_v2_routes._PENDING_TURNS[(did, cid)] = {"state": "running"}
        out = await sessions_v2_routes.cancel_queued_desk_turn(
            sessions_v2_routes.CancelQueuedRequest(desk_id=did, client_turn_id=cid))
        assert out == {"status": "too_late"}
        assert service.discarded == []

    asyncio.run(main())


def test_cancel_arrives_before_brin_posts_queued_message(monkeypatch):
    service = FakeSessionService()
    did, cid = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)
    monkeypatch.setattr(service, "get_session", lambda _sid: {
        "status": "active", "tenant_id": 1, "agent_id": 7,
        "model": "flint-cli", "metadata": {"conversation": "agent-flint:desk:" + did},
    })
    monkeypatch.setattr(sessions_v2_routes.budget, "check_budget", lambda **_kw: (True, None, []))
    monkeypatch.setattr(sessions_v2_routes.backends, "resolve_backend", lambda _model: object())

    async def main():
        request = sessions_v2_routes.CancelQueuedRequest(desk_id=did, client_turn_id=cid)
        assert await sessions_v2_routes.cancel_queued_desk_turn(request) == {"status": "pending"}
        out = await sessions_v2_routes.send_message("s-before", sessions_v2_routes.SendMessageRequest(
            role="user", content="same text", client_turn_id=cid))
        assert out == {"cancelled": True}
        assert service.appended == []
        assert await sessions_v2_routes.cancel_queued_desk_turn(request) == {"status": "cancelled"}

    asyncio.run(main())


def test_queued_message_ids_are_recoverable_after_browser_reload(monkeypatch):
    from backends import cli_claude
    sid, did, cid = "s-reload", str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(cli_claude, "desks_in_progress", lambda: [
        {"session_id": sid, "desk_id": did, "in_progress": True}])
    sessions_v2_routes._TURN_WAITERS[sid] = 1
    sessions_v2_routes._PENDING_TURNS[(did, cid)] = {
        "session_id": sid, "desk_id": did, "client_turn_id": cid,
        "message_id": 42, "state": "queued"}

    async def main():
        rows = (await sessions_v2_routes.list_desks_in_progress())["desks"]
        assert rows[0]["queued"] == 1
        assert rows[0]["queued_turns"] == [{"client_turn_id": cid, "message_id": 42}]

    asyncio.run(main())


def test_http_model_queue_is_visible_after_reload(monkeypatch):
    from backends import cli_claude
    sid, did, cid = "s-http", str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(cli_claude, "desks_in_progress", lambda: [])
    sessions_v2_routes._TURN_WAITERS[sid] = 1
    sessions_v2_routes._PENDING_TURNS[(did, cid)] = {
        "session_id": sid, "desk_id": did, "client_turn_id": cid,
                "message_id": 43, "state": "queued", "agent": "agent-fixture"}

    async def main():
        lock = sessions_v2_routes._turn_lock(sid)
        await lock.acquire()
        try:
            rows = (await sessions_v2_routes.list_desks_in_progress())["desks"]
            assert rows[0]["desk_id"] == did
            assert rows[0]["agent"] == "agent-fixture"
            assert rows[0]["queued_turns"] == [{"client_turn_id": cid, "message_id": 43}]
        finally:
            lock.release()

    asyncio.run(main())


def test_a_second_turn_queues_instead_of_being_refused(monkeypatch):
    """The old per-session send lock queued overlapping turns; so does the claim."""
    service = FakeSessionService()
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)
    order = []

    async def no_handoff(*_a, **_kw):
        return None

    monkeypatch.setattr(sessions_v2_routes.context_handoff, "maybe_handoff", no_handoff)

    async def main():
        release_first = asyncio.Event()

        async def complete(session_id, sess, backend, history, content, user_msg_id):
            order.append("start:" + content)
            if content == "one":
                await release_first.wait()
            order.append("end:" + content)
            return {"response": {"usage": {}}}

        monkeypatch.setattr(sessions_v2_routes, "_complete_turn_async", complete)
        loop = asyncio.get_running_loop()
        f1, f2 = loop.create_future(), loop.create_future()
        t1 = asyncio.create_task(_route_turn("s-queue", "one", f1))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(_route_turn("s-queue", "two", f2))
        await asyncio.sleep(0)

        assert order == ["start:one"], "the second turn must wait, not run"
        assert not f2.done()
        release_first.set()
        await asyncio.gather(t1, t2)
        assert f1.done() and f2.done()

    asyncio.run(main())
    assert order == ["start:one", "end:one", "start:two", "end:two"]


def test_answer_is_published_before_the_handoff_runs(monkeypatch):
    """The person's reply must not wait for a second full-context request."""
    service = FakeSessionService()
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)
    seen = {}

    async def complete(*_args):
        return {"response": {"usage": OVER_THRESHOLD}}

    holder = {}

    async def handoff(_svc, session_id, _sess, _backend, usage):
        seen["answer_delivered"] = holder["future"].done()
        seen["usage"] = usage
        return {"handoff_doc_id": "doc_x"}

    monkeypatch.setattr(sessions_v2_routes, "_complete_turn_async", complete)
    monkeypatch.setattr(sessions_v2_routes.context_handoff, "maybe_handoff", handoff)

    async def main():
        holder["future"] = asyncio.get_running_loop().create_future()
        await _route_turn("s-order", "hello", holder["future"])
        return holder["future"].result()

    result = asyncio.run(main())
    assert result == {"response": {"usage": OVER_THRESHOLD}}
    assert seen["answer_delivered"] is True
    assert seen["usage"] == OVER_THRESHOLD


def test_handoff_is_deferred_while_another_turn_is_queued(monkeypatch):
    """A burst finishes on the session it was addressed to."""
    service = FakeSessionService()
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)
    handoffs = []

    async def handoff(_svc, session_id, _sess, _backend, _usage):
        handoffs.append(session_id)
        return None

    monkeypatch.setattr(sessions_v2_routes.context_handoff, "maybe_handoff", handoff)

    async def main():
        queued = asyncio.Event()

        async def complete(session_id, sess, backend, history, content, user_msg_id):
            if content == "one":
                await queued.wait()
            return {"response": {"usage": OVER_THRESHOLD}}

        monkeypatch.setattr(sessions_v2_routes, "_complete_turn_async", complete)
        loop = asyncio.get_running_loop()
        f1, f2 = loop.create_future(), loop.create_future()
        t1 = asyncio.create_task(_route_turn("s-defer", "one", f1))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(_route_turn("s-defer", "two", f2))
        await asyncio.sleep(0)
        queued.set()
        await asyncio.gather(t1, t2)

    asyncio.run(main())
    # Once, for the turn that had nobody waiting behind it.
    assert handoffs == ["s-defer"]


def test_queued_turn_on_a_retired_session_is_told_to_start_a_clean_one(monkeypatch):
    service = FakeSessionService(status="completed")
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)

    async def complete(*_args):
        raise AssertionError("a retired session must never receive the turn")

    monkeypatch.setattr(sessions_v2_routes, "_complete_turn_async", complete)

    async def main():
        future = asyncio.get_running_loop().create_future()
        with pytest.raises(sessions_v2_routes.SessionRetiredError):
            await _route_turn("s-retired", "hello", future)
        return future

    future = asyncio.run(main())
    assert isinstance(future.exception(), sessions_v2_routes.SessionRetiredError)
    assert sessions_v2_routes._TURN_WAITERS.get("s-retired", 0) == 0
    # The undelivered copy is dropped: the same text is about to be stored in
    # the clean session, and the person must not see it twice.
    assert service.discarded == [("s-retired", 1)]


def test_turn_claim_is_released_on_failure(monkeypatch):
    service = FakeSessionService()
    monkeypatch.setattr(sessions_v2_routes, "session_service", service)

    async def fail(*_args):
        raise RuntimeError("provider failed")

    monkeypatch.setattr(sessions_v2_routes, "_complete_turn_async", fail)

    async def main():
        future = asyncio.get_running_loop().create_future()
        with pytest.raises(RuntimeError):
            await _route_turn("s-fail", "hello", future)
        assert isinstance(future.exception(), RuntimeError)

    asyncio.run(main())
    assert sessions_v2_routes._TURN_WAITERS.get("s-fail", 0) == 0
    assert "s-fail" not in sessions_v2_routes._TURN_LOCKS
