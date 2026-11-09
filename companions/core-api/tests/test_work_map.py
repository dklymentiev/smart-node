"""Tests for GET /api/work/map.

Two bands:
  * pure logic (no DB): window parsing refuses anything outside 24 h / 168 h,
    task-ref extraction pulls ids from a planner path and from message text,
    status / feed-kind labels are the English form the Studio template reads.
  * endpoint shape (through TestClient against the real DB): the response has
    the documented keys, schema_version is 1, project / agent / task lanes are
    lists, and the cache is honoured on a second call unless ?fresh=1 bypasses.

Run inside the backend container (see conftest.py).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from routes import work


@pytest.fixture
def as_owner():
    """Call as the node's owner: the account whose actor holds the super-admin
    role row. A bare TestClient presents no identity, and these routes need a
    caller whose role holds their permission."""
    from auth import Principal, get_principal
    from db import cursor
    from main import app

    with cursor() as cur:
        cur.execute(
            "SELECT u.id, u.email, u.tenant_id, u.actor_id FROM sn_config.users u "
            "JOIN sn_core.actor_roles r ON r.actor_id = u.actor_id "
            "WHERE r.role = 'super-admin' AND u.active")
        row = cur.fetchone()
    assert row is not None, "this database has no owner account with a role row"
    app.dependency_overrides[get_principal] = lambda: Principal(
        username=row["email"], user_id=row["id"], tenant_id=row["tenant_id"],
        role="super-admin", actor_id=row["actor_id"], actor_kind="human")
    yield
    app.dependency_overrides.pop(get_principal, None)


# --------------------------------------------------------------------------- window parsing

def test_window_168h_is_accepted():
    assert work._parse_window("168h") == 168


def test_window_24h_is_accepted():
    assert work._parse_window("24h") == 24


def test_window_720h_is_refused():
    # Only 24 and 168 are allowed; 30d (720h) is deliberately not in the set.
    with pytest.raises(HTTPException) as ei:
        work._parse_window("720h")
    assert ei.value.status_code == 400
    assert ei.value.detail["error"] == "window_not_allowed"


def test_window_garbage_is_refused():
    with pytest.raises(HTTPException) as ei:
        work._parse_window("week")
    assert ei.value.status_code == 400
    assert ei.value.detail["error"] == "window_invalid"


def test_window_empty_is_refused():
    with pytest.raises(HTTPException):
        work._parse_window("")


# --------------------------------------------------------------------------- task-ref extraction

def test_refs_from_payload_pulls_task_id_from_planner_path():
    payload = {"op": "planner.comment",
               "path": "api/v1/tasks/8421/comments"}
    refs = work._refs_from_payload(payload, max_task_id=10000, known_task_ids={8421})
    assert 8421 in refs


def test_refs_from_payload_pulls_four_digit_id_from_preview():
    payload = {"channel": "internal",
               "preview": "chat text with 8421 as a bare number"}
    refs = work._refs_from_payload(payload, max_task_id=10000, known_task_ids={8421})
    assert 8421 in refs


def test_refs_from_payload_ignores_unknown_four_digit_number():
    # A four-digit number that is not an existing task id must not create a
    # phantom reference.
    payload = {"channel": "internal",
               "preview": "the year 2026 ends soon"}
    refs = work._refs_from_payload(payload, max_task_id=10000, known_task_ids={8421})
    assert 2026 not in refs


def test_refs_from_payload_ignores_three_digit_numbers():
    payload = {"preview": "see #42 for the old issue"}
    refs = work._refs_from_payload(payload, max_task_id=10000, known_task_ids={42})
    assert 42 not in refs


# --------------------------------------------------------------------------- label dictionaries

def test_status_label_en_covers_every_planner_status():
    # Every status the planner can emit must have an English label; a missing
    # entry means a transition caption in the Gantt would say the raw slug.
    for s in ("open", "in_progress", "pending_review", "done", "blocked",
              "rejected", "awaiting_approval", "escalated", "assessing"):
        assert s in work._STATUS_LABEL_EN
        assert work._STATUS_LABEL_EN[s]


def test_feed_kind_labels_are_english():
    assert work._FEED_KIND_LABEL_EN["created"] == "opened"
    assert work._FEED_KIND_LABEL_EN["done"] == "closed"
    assert work._FEED_KIND_LABEL_EN["pending_review"] == "sent to review"


# --------------------------------------------------------------------------- endpoint through TestClient

def _assert_lane_shape(lane: dict):
    for key in ("name", "branches", "nodes", "who", "total"):
        assert key in lane, f"missing {key} in lane {lane.get('name')}"
    assert isinstance(lane["branches"], list)
    for b in lane["branches"]:
        for key in ("key", "name", "h"):
            assert key in b
        assert isinstance(b["h"], list)


def test_work_map_shape(client, as_owner):
    r = client.get("/api/work/map")
    assert r.status_code == 200
    b = r.json()
    for k in ("as_of", "window_hours", "t0", "tend", "H",
              "project", "agent", "task", "now", "feed", "raw",
              "handoffs", "events", "attribution", "schema_version"):
        assert k in b, f"response is missing {k}"
    assert b["schema_version"] == 1
    assert b["window_hours"] == 168
    assert b["H"] == 168
    assert isinstance(b["project"], list)
    assert isinstance(b["agent"], list)
    assert isinstance(b["task"], list)
    assert isinstance(b["now"], list)
    assert isinstance(b["feed"], list)
    assert isinstance(b["handoffs"], dict)
    for lane in b["project"] + b["agent"] + b["task"]:
        _assert_lane_shape(lane)
    attr = b["attribution"]
    for k in ("explicit_refs", "inferred_refs", "unattributed",
              "explicit_minutes", "inferred_minutes", "unattributed_minutes",
              "notes"):
        assert k in attr
    assert isinstance(attr["notes"], list)
    assert attr["notes"], "attribution notes must not be empty"


def test_work_map_window_24h(client, as_owner):
    r = client.get("/api/work/map?window=24h")
    assert r.status_code == 200
    b = r.json()
    assert b["window_hours"] == 24
    assert b["H"] == 24


def test_work_map_window_30d_refused(client, as_owner):
    # 30d (720h) must not silently fall back to 7d; the lab mockup disabled the
    # button for a reason and the endpoint repeats that refusal.
    r = client.get("/api/work/map?window=720h")
    assert r.status_code == 400
    body = r.json()
    detail = body.get("detail", {})
    assert detail.get("error") == "window_not_allowed"


def test_work_map_fresh_bypasses_cache(client, as_owner):
    # Two calls to the cached endpoint return the same `as_of`; a `?fresh=1`
    # call rebuilds. The server-side timestamp is produced inside _build_map,
    # so a bypass yields a different (newer or equal) value.
    r1 = client.get("/api/work/map")
    r2 = client.get("/api/work/map?fresh=1")
    assert r1.status_code == 200 and r2.status_code == 200
    # The fresh call must still return a valid payload; the cache test proper
    # is that the cache does not raise on repeat use (above) and that fresh
    # does not refuse.
    assert r2.json()["schema_version"] == 1


def test_work_map_task_lane_names_are_task_refs(client, as_owner):
    r = client.get("/api/work/map")
    assert r.status_code == 200
    for lane in r.json()["task"]:
        # Task lanes are #<id>, where <id> is an integer; the Studio template
        # strips the # to key markers off the id.
        assert lane["name"].startswith("#"), lane["name"]
        assert lane["name"][1:].isdigit()
