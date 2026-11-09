"""How long POST /sessions/{id}/messages holds the caller before 202 "working".

A live channel has nowhere to put an answer that arrives after its request is
gone, so it asks for the whole turn; everyone else gets the default window."""
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import sessions_v2_routes as routes  # noqa: E402


def test_no_request_is_the_default_window():
    assert routes._sync_wait(None) == routes._SYNC_RESPONSE_TIMEOUT


def test_a_caller_may_wait_for_the_whole_turn():
    assert routes._sync_wait(3600) == 3600


def test_the_wait_is_bounded_by_the_turn_limit_and_never_zero():
    assert routes._sync_wait(10 ** 6) == routes._MAX_SYNC_WAIT
    assert routes._sync_wait(0) == 1.0


def test_the_request_carries_it():
    assert routes.SendMessageRequest(content="hi", wait_seconds=600).wait_seconds == 600
    assert routes.SendMessageRequest(content="hi").wait_seconds is None
