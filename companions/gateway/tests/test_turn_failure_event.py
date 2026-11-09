"""A turn that dies is journaled as turn.failed.

Why this exists: on 2026-10-11 an agent's background turn died of a silent
provider and the only trace was a line in the gateway log. Nothing waits on a
background turn, so nothing told anyone, and the agent just stopped. The event
is what the owner's monitor mails; its type ends in ".failed" for that reason.

The session lookup, the turn and the event writer are replaced, so nothing is
written. Needs the gateway environment (the modules read it at import).

Runs two ways, like test_budget.py:
    pytest tests/test_turn_failure_event.py
    ./venv/bin/python tests/test_turn_failure_event.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sessions_v2_routes as routes  # noqa: E402

STALL = RuntimeError("flint turn failed (error_during_execution, stall): provider silent")


def _run(raises, emit):
    """Run one turn that raises; return (what propagated, the caller's future, events)."""
    events = []

    def record(event_type, session_id, payload):
        events.append((event_type, session_id, payload))
        return emit(event_type, session_id, payload)

    async def dying_turn(*args, **kwargs):
        raise raises

    saved = (routes.context_handoff.emit_event, routes.session_service.get_session,
             routes.session_service.get_history, routes.context_handoff.content_for_first_turn,
             routes._complete_turn_async)
    routes.context_handoff.emit_event = record
    routes.session_service.get_session = lambda sid: {
        "status": "active", "model": "flint-cli/stub",
        "metadata": {"conversation": "agent-a:runner-agent-a-1-1"}}
    routes.session_service.get_history = lambda sid: []
    routes.context_handoff.content_for_first_turn = lambda history, content: content
    routes._complete_turn_async = dying_turn

    async def go():
        future = asyncio.get_running_loop().create_future()
        try:
            await routes._run_queued_turn_async("sess-1", object(), "hi", 1, future)
        except Exception as exc:  # noqa: BLE001 -- the test inspects it
            return exc, future.exception()
        return None, None

    try:
        propagated, published = asyncio.run(go())
    finally:
        (routes.context_handoff.emit_event, routes.session_service.get_session,
         routes.session_service.get_history, routes.context_handoff.content_for_first_turn,
         routes._complete_turn_async) = saved
    return propagated, published, events


def test_a_dead_turn_is_journaled_with_what_killed_it():
    propagated, published, events = _run(STALL, lambda *a: 1)
    assert propagated is STALL and published is STALL
    assert [e[0] for e in events] == ["turn.failed"]
    _, session_id, payload = events[0]
    assert session_id == "sess-1"
    assert payload["conversation"] == "agent-a:runner-agent-a-1-1"
    assert payload["model"] == "flint-cli/stub"
    assert payload["error_type"] == "RuntimeError"
    assert "provider silent" in payload["error"]


def test_a_retired_session_is_a_reset_not_a_failure():
    retired = routes.SessionRetiredError("completed")
    propagated, _, events = _run(retired, lambda *a: 1)
    assert propagated is retired
    assert events == []


def test_a_journal_that_is_down_does_not_replace_the_turns_error():
    def down(*args):
        raise RuntimeError("journal down")

    propagated, published, _ = _run(STALL, down)
    assert propagated is STALL and published is STALL


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok  ", name)
    print("ALL OK")
