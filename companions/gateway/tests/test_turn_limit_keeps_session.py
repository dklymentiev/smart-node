"""A turn that runs into the turn limit is stopped; its session lives on.

The session used to be set failed at the cutoff, so the next message found it
dead and the agent started over in a new session without what it had done.
In-process; the backend and the database are fakes."""
import asyncio
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402

import sessions_v2_routes as routes  # noqa: E402
from backends.cli_claude import TurnLimitExceeded  # noqa: E402

SESS = {"status": "active", "model": "claude-cli/sonnet", "agent_id": 7, "tenant_id": 1, "metadata": {}}


class Backend:
    async def send(self, session_id, history, content):
        raise TurnLimitExceeded(session_id, 3600)


@pytest.fixture
def store(monkeypatch):
    seen = {"status": [], "messages": [], "cutoffs": []}
    monkeypatch.setattr(routes.session_service, "set_status",
                        lambda sid, status, error=None: seen["status"].append(status))
    monkeypatch.setattr(routes.session_service, "append_message",
                        lambda sid, **kw: seen["messages"].append(kw) or 42)
    monkeypatch.setattr(routes, "record_turn_cutoff",
                        lambda sid, seconds: seen["cutoffs"].append(seconds) or None)
    return seen


def test_the_cutoff_stops_the_turn_and_keeps_the_session(store):
    out = asyncio.run(routes._complete_turn_async("s1", SESS, Backend(), [], "long job", 41))
    assert out["stopped"] is True and out["stopped_by"] == "turn_seconds"
    assert out["outcome"] == "stopped"
    assert out["response"]["final_answer"] == ""
    assert store["status"] == []                      # not failed: the next message resumes it
    assert store["cutoffs"] == [3600]                 # still journalled
    assert store["messages"][-1]["is_partial"] is True
