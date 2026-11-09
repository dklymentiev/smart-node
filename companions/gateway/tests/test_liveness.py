"""Is a live agent working? The relay keeps every model call in flight in
sn_core.model_calls_live, the process table says what each process waits on
and when it last showed life, and a turn the CLI ended with an error result is
failed, not an answer. In-process; the database and the upstream are fakes."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import anthropic_relay as relay  # noqa: E402
import model_door  # noqa: E402
import model_tokens  # noqa: E402
import sessions_v2_routes as routes  # noqa: E402
from backends import BackendResponse  # noqa: E402
from fake_db import install  # noqa: E402

SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\n\n'
       b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
       b'"usage":{"output_tokens":2}}\n\n')
SID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def door(monkeypatch):
    log = []
    monkeypatch.setattr(model_door, "live_start", lambda rec: log.append(("start", rec["id"], rec.get("agent"))))
    monkeypatch.setattr(model_door, "live_touch", lambda rid, n: log.append(("touch", rid, n)))
    monkeypatch.setattr(model_door, "live_end", lambda rid: log.append(("end", rid)))
    monkeypatch.setattr(model_door, "record_call", lambda rec: log.append(("record", rec["id"], rec["outcome"])))
    routes_ = {"claude-cli": relay.Route("claude-cli", relay.DEFAULT_UPSTREAM,
                                         {"CLAUDE_CODE_OAUTH_TOKEN": "node-oauth-secret"})}
    monkeypatch.setattr(relay, "_load_route", lambda provider: routes_[provider])
    relay.forget_routes()
    install(monkeypatch, model_tokens)
    monkeypatch.setattr(model_door, "_decide", lambda agent, session_id: None)
    model_door.forget_policy()
    app = FastAPI()
    app.include_router(relay.router)
    return TestClient(app), log, monkeypatch


def _call(client):
    token = model_tokens.issue(SID, "agent-b", "agent-b", "opus")
    return client.post("/anthropic/v1/messages",
                       headers={"authorization": "Bearer " + token, "x-claude-code-session-id": SID},
                       content=json.dumps({"model": "claude-opus-4-7", "stream": True, "messages": []}))


def test_a_streamed_call_is_live_from_its_start_to_its_record(door):
    client, log, mp = door
    mp.setattr(model_door, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                   stream=httpx.ByteStream(SSE)))))
    assert _call(client).status_code == 200
    kinds = [e[0] for e in log]
    assert kinds[0] == "start" and log[0][2] == "agent-b"
    assert "touch" in kinds                                     # the first byte is stamped
    # Gone from live, then recorded. A touch never makes the stream wait, so it
    # may land after the delete, where it updates nothing.
    assert kinds.index("end") < kinds.index("record")
    rid = log[0][1]
    assert all(e[1] == rid for e in log)
    assert next(e for e in log if e[0] == "touch")[2] > 0


def test_a_call_that_cannot_reach_upstream_leaves_no_live_row(door):
    client, log, mp = door

    def down(req):
        raise httpx.ConnectError("refused")

    mp.setattr(model_door, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    assert _call(client).status_code == 502
    assert [e[0] for e in log] == ["start", "end", "record"]


def test_a_live_write_that_fails_does_not_fail_the_call(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("database down")

    monkeypatch.setattr(model_door.db, "query", boom)
    model_door.live_start({"id": "r1"})
    model_door.live_touch("r1", 10)
    model_door.live_end("r1")
    model_door.live_clear()


# -- what a process waits on, and when it last showed life --------------------

NOW = datetime(2026, 10, 15, 13, 0, 0, tzinfo=timezone.utc)


def _iso(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def test_a_call_in_flight_with_bytes_flowing_is_waiting_on_the_model_and_alive():
    p = {"turn": {"in_progress": True, "elapsed": 90, "action": None},
         "model_call": {"since": _iso(60), "last_event_at": _iso(2), "bytes": 5000},
         "last_call": {"at": _iso(70)}}
    a = routes.activity(p, NOW)
    assert a["waiting_on"] == "model" and a["silent_seconds"] == 2


def test_a_call_with_no_byte_yet_is_silent_since_it_started():
    p = {"turn": {"in_progress": True, "elapsed": 300, "action": None},
         "model_call": {"since": _iso(240), "last_event_at": None, "bytes": 0},
         "last_call": {"at": _iso(250)}}
    a = routes.activity(p, NOW)
    assert a["waiting_on"] == "model" and a["silent_seconds"] == 240


def test_a_call_that_started_and_went_quiet_is_silent_since_its_last_event():
    p = {"turn": {"in_progress": True, "elapsed": 300, "action": None},
         "model_call": {"since": _iso(240), "last_event_at": _iso(235), "bytes": 120},
         "last_call": None}
    assert routes.activity(p, NOW)["silent_seconds"] == 235


def test_a_running_tool_is_waited_on_since_it_started():
    p = {"turn": {"in_progress": True, "elapsed": 130, "action": "Bash", "action_elapsed": 118},
         "model_call": None, "last_call": {"at": _iso(119)}}
    a = routes.activity(p, NOW)
    assert a["waiting_on"] == "tool" and a["silent_seconds"] == 118


def test_an_idle_process_waits_on_nothing():
    p = {"turn": {"in_progress": False}, "idle_seconds": 40, "model_call": None,
         "last_call": {"at": _iso(41)}}
    a = routes.activity(p, NOW)
    assert a["waiting_on"] is None and a["silent_seconds"] == 40


def test_a_process_with_no_sign_of_life_at_all_says_so():
    a = routes.activity({"turn": {"in_progress": True, "elapsed": 3}}, NOW)
    assert a == {"waiting_on": "cli", "last_activity_at": None, "silent_seconds": None}


# -- a turn the CLI ended with an error result --------------------------------

def test_an_error_result_is_a_failed_turn_journalled_not_an_answer(monkeypatch):
    seen = {"status": [], "failures": []}
    monkeypatch.setattr(routes.session_service, "set_status",
                        lambda sid, status, error=None: seen["status"].append(status))
    monkeypatch.setattr(routes.session_service, "append_message", lambda sid, **kw: 42)
    monkeypatch.setattr(routes.session_service, "record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(routes.budget, "record_spend", lambda **kw: [])
    monkeypatch.setattr(routes.budget, "check_budget", lambda **kw: (True, None, []))
    monkeypatch.setattr(routes, "_record_turn_failure", lambda sid, exc: seen["failures"].append(str(exc)))

    class Backend:
        async def send(self, *a):
            return BackendResponse(content="API Error: 529 overloaded", failed="API Error: 529 overloaded")

    sess = {"status": "active", "model": "claude-cli/sonnet", "agent_id": 7, "tenant_id": 1, "metadata": {}}
    out = asyncio.run(routes._complete_turn_async(SID, sess, Backend(), [], "hi", 41))
    assert out["outcome"] == "failed" and out["failed"].startswith("API Error: 529")
    assert out["response"]["final_answer"] == ""
    assert seen["failures"] == ["API Error: 529 overloaded"]
    assert seen["status"] == []                 # the turn failed; the session is fine


def test_pings_and_comments_are_not_progress(door):
    """A stream that only keeps the connection open is a model saying nothing."""
    client, log, mp = door
    quiet = b'event: ping\ndata: {"type":"ping"}\n\n: keepalive\n\n'
    mp.setattr(model_door, "_get_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                   stream=httpx.ByteStream(quiet)))))
    assert _call(client).status_code == 200
    assert "touch" not in [e[0] for e in log]


def test_a_tally_counts_answer_events_only():
    t = relay.Tally()
    t.feed_sse(b'event: ping\ndata: {"type":"ping"}\n\n')
    assert t.progress == 0
    t.feed_sse(SSE)
    assert t.progress == 2
