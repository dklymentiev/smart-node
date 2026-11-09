"""Tests for GET /api/work/now (first slice).

Two bands:
  * pure logic (no DB): regex that pulls task_id out of a planner event path,
    the attribution tagging rule (explicit vs inferred vs unattributed), the
    attribution-block sum invariant, the minutes-to-last-event rule, and the
    SQL-shape check that the live filter JOINs are in place.
  * endpoint shape (through TestClient against the real DB): response has the
    documented keys, schema_version is 1, ?price=1 returns 501.

The live filter itself (only slugs in sn_config.agents AND agent_runtime.status
= 'busy') is enforced in SQL. A pure Python mock of cursor() cannot verify it,
so the SQL-shape test (test_build_now_sql_filters_known_and_busy_agents) is the
guard; a full integration test lives outside this file and runs on a stand.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


# --------------------------------------------------------------------------- regex

def test_task_path_regex_matches_tasks_id():
    assert work._extract_task_id("api/v1/tasks/5025") == 5025
    assert work._extract_task_id("api/v1/tasks/5025/comments") == 5025
    assert work._extract_task_id("/api/v1/tasks/42/comments") == 42


def test_task_path_regex_does_not_match_search_or_list():
    # A listing or a search call is not a tie to one task in particular.
    assert work._extract_task_id("api/v1/tasks") is None
    assert work._extract_task_id("api/v1/tasks/search") is None
    assert work._extract_task_id(None) is None
    assert work._extract_task_id("") is None


# --------------------------------------------------------------------------- attribution tagging

def test_inferred_tag_when_no_explicit_event_but_dashboard_assigns_task(monkeypatch):
    """A planner tool call in the event preview that does NOT name a task_id
    (for example a listing or a search) must leave the agent at `inferred`,
    tied to the planner dashboard's in-progress task; not `explicit`.
    """
    now = datetime.now(timezone.utc)
    dashboard = [{
        "id": 4895, "title": "wake retry", "status": "in_progress",
        "assignee": "ann", "updated_at": now.isoformat(),
    }]

    # Stub the planner HTTP call so the test is DB-dependent only on sn_core.events.
    monkeypatch.setattr(work, "_planner_get",
                        lambda sub: dashboard if sub.endswith("dashboard/agents-working") else None)

    # Stub the DB reads so this test is pure logic. Three event rows for ann
    # in the last 60 min, none of them naming a task_id in payload.path:
    #   last_event: planner.list (api/v1/tasks)
    #   first_event: planner.list
    #   planner_events: [list, list]
    class _StubCur:
        def __init__(self):
            self._q = 0
            self.rows = []

        def execute(self, sql, params):
            # The three queries in order: last-per-agent, first-per-agent, planner-events
            if self._q == 0:
                self.rows = [{
                    "slug": "ann",
                    "event_type": "tool.invoked",
                    "entity_id": "planner",
                    "payload": {"op": "planner.list", "path": "api/v1/tasks"},
                    "ts": now - timedelta(minutes=1),
                }]
            elif self._q == 1:
                self.rows = [{"slug": "ann", "ts": now - timedelta(minutes=30)}]
            else:
                self.rows = [
                    {"slug": "ann", "event_type": "tool.invoked",
                     "payload": {"op": "planner.list", "path": "api/v1/tasks"},
                     "ts": now - timedelta(minutes=1)},
                    {"slug": "ann", "event_type": "tool.invoked",
                     "payload": {"op": "planner.list", "path": "api/v1/tasks"},
                     "ts": now - timedelta(minutes=30)},
                ]
            self._q += 1

        def fetchall(self):
            return self.rows

    class _Ctx:
        def __enter__(self): return _StubCur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(work, "cursor", lambda: _Ctx())

    class _P:
        tenant_id = 1
        cross_tenant = True
    data = work._build_now(_P())

    assert data["schema_version"] == 1
    assert len(data["agents"]) == 1
    row = data["agents"][0]
    assert row["agent"] == "ann"
    # No event in the window named /tasks/<id>, so the tie is INFERRED from the
    # planner dashboard, not explicit.
    assert row["attribution"] == "inferred"
    assert row["task_id"] == 4895
    assert row["task_title"] == "wake retry"


def test_explicit_tag_when_planner_event_names_task_id(monkeypatch):
    now = datetime.now(timezone.utc)
    dashboard = [{
        "id": 4895, "title": "wake retry", "status": "in_progress",
        "assignee": "ann", "updated_at": now.isoformat(),
    }]
    monkeypatch.setattr(work, "_planner_get",
                        lambda sub: dashboard if sub.endswith("dashboard/agents-working") else None)

    class _StubCur:
        def __init__(self):
            self._q = 0
            self.rows = []

        def execute(self, sql, params):
            if self._q == 0:
                self.rows = [{
                    "slug": "ann",
                    "event_type": "tool.invoked",
                    "entity_id": "planner",
                    "payload": {"op": "planner.comment",
                                "path": "api/v1/tasks/4895/comments"},
                    "ts": now - timedelta(minutes=1),
                }]
            elif self._q == 1:
                self.rows = [{"slug": "ann", "ts": now - timedelta(minutes=30)}]
            else:
                self.rows = [
                    {"slug": "ann", "event_type": "tool.invoked",
                     "payload": {"op": "planner.comment",
                                 "path": "api/v1/tasks/4895/comments"},
                     "ts": now - timedelta(minutes=1)},
                    {"slug": "ann", "event_type": "tool.invoked",
                     "payload": {"op": "planner.get",
                                 "path": "api/v1/tasks/4895"},
                     "ts": now - timedelta(minutes=20)},
                ]
            self._q += 1

        def fetchall(self):
            return self.rows

    class _Ctx:
        def __enter__(self): return _StubCur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(work, "cursor", lambda: _Ctx())

    class _P:
        tenant_id = 1
        cross_tenant = True
    data = work._build_now(_P())
    row = data["agents"][0]
    assert row["attribution"] == "explicit"
    assert row["task_id"] == 4895


# --------------------------------------------------------------------------- attribution sum

def test_attribution_sum_matches_agent_rows(monkeypatch):
    """The attribution block's counts must sum to the number of agent rows.

    One of each class, so explicit_refs + inferred_refs + unattributed == 3,
    and each _minutes field is >= 0.
    """
    now = datetime.now(timezone.utc)
    dashboard = [{
        "id": 11, "title": "A", "status": "in_progress",
        "assignee": "bob", "updated_at": now.isoformat(),
    }]
    monkeypatch.setattr(work, "_planner_get",
                        lambda sub: dashboard if sub.endswith("dashboard/agents-working") else None)

    class _StubCur:
        def __init__(self):
            self._q = 0
            self.rows = []

        def execute(self, sql, params):
            if self._q == 0:
                self.rows = [
                    {"slug": "ann", "event_type": "tool.invoked", "entity_id": "planner",
                     "payload": {"op": "planner.comment",
                                 "path": "api/v1/tasks/42/comments"},
                     "ts": now - timedelta(minutes=1)},
                    {"slug": "bob", "event_type": "tool.invoked", "entity_id": "mesh",
                     "payload": {}, "ts": now - timedelta(minutes=2)},
                    {"slug": "cat", "event_type": "tool.invoked", "entity_id": "mesh",
                     "payload": {}, "ts": now - timedelta(minutes=3)},
                ]
            elif self._q == 1:
                self.rows = [
                    {"slug": "ann", "ts": now - timedelta(minutes=30)},
                    {"slug": "bob", "ts": now - timedelta(minutes=20)},
                    {"slug": "cat", "ts": now - timedelta(minutes=10)},
                ]
            else:
                self.rows = [
                    {"slug": "ann", "event_type": "tool.invoked",
                     "payload": {"op": "planner.comment",
                                 "path": "api/v1/tasks/42/comments"},
                     "ts": now - timedelta(minutes=1)},
                ]
            self._q += 1

        def fetchall(self):
            return self.rows

    class _Ctx:
        def __enter__(self): return _StubCur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(work, "cursor", lambda: _Ctx())

    class _P:
        tenant_id = 1
        cross_tenant = True
    data = work._build_now(_P())

    agents = data["agents"]
    attr = data["attribution"]
    # ann has an explicit event (/tasks/42/comments), bob has a dashboard hit
    # (inferred), cat has neither (unattributed).
    assert attr["explicit_refs"] == 1
    assert attr["inferred_refs"] == 1
    assert attr["unattributed"] == 1
    assert len(agents) == attr["explicit_refs"] + attr["inferred_refs"] + attr["unattributed"]
    # Minutes are non-negative in every bucket.
    for k in ("explicit_minutes", "inferred_minutes", "unattributed_minutes"):
        assert attr[k] >= 0


# --------------------------------------------------------------------------- minutes bound

def test_minutes_bound_at_last_event_not_as_of(monkeypatch):
    """An agent whose latest event is 5 minutes ago accrues minutes only up
    to that event, not to `as_of`. Prevents a stale-row still-busy
    regression (agent paused half-way through the window keeps the row,
    but minutes stop at the pause).
    """
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(work, "_planner_get", lambda sub: [])

    class _StubCur:
        def __init__(self):
            self._q = 0
            self.rows = []

        def execute(self, sql, params):
            if self._q == 0:
                # Last event 5 minutes ago (agent paused since then).
                self.rows = [{
                    "slug": "ann",
                    "event_type": "tool.invoked",
                    "entity_id": "planner",
                    "payload": {"op": "planner.comment",
                                "path": "api/v1/tasks/4895/comments"},
                    "ts": now - timedelta(minutes=5),
                }]
            elif self._q == 1:
                self.rows = [{"slug": "ann", "ts": now - timedelta(minutes=35)}]
            else:
                self.rows = [
                    {"slug": "ann", "event_type": "tool.invoked",
                     "payload": {"op": "planner.comment",
                                 "path": "api/v1/tasks/4895/comments"},
                     "ts": now - timedelta(minutes=5)},
                    {"slug": "ann", "event_type": "tool.invoked",
                     "payload": {"op": "planner.get",
                                 "path": "api/v1/tasks/4895"},
                     "ts": now - timedelta(minutes=20)},
                ]
            self._q += 1

        def fetchall(self):
            return self.rows

    class _Ctx:
        def __enter__(self): return _StubCur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(work, "cursor", lambda: _Ctx())

    class _P:
        tenant_id = 1
        cross_tenant = True
    data = work._build_now(_P())

    assert len(data["agents"]) == 1
    minutes = data["attribution"]["explicit_minutes"]
    # since = earliest planner-event with the same task_id, which is -20 min.
    # last_event = -5 min. Expected minutes ~= 15.
    # Under the previous rule (now - since) this would have been ~20.
    assert 14.0 <= minutes <= 16.0, f"explicit_minutes should be ~15, got {minutes}"


# --------------------------------------------------------------------------- SQL shape

def test_build_now_sql_filters_known_and_busy_agents(monkeypatch):
    """The event-log queries must join sn_config.agents (known slug) and
    sn_core.agent_runtime with status='busy' so a non-agent actor (admin,
    person principals) does not appear and a stopped agent does not appear.

    The join is enforced in SQL; this test records the SQL text and asserts
    the clauses are present. A pure Python mock cannot verify filter effect
    directly, so the next guard is an integration test on a stand.
    """
    captured = []

    class _RecCur:
        def execute(self, sql, params):
            captured.append(sql)
        def fetchall(self):
            return []

    class _Ctx:
        def __enter__(self): return _RecCur()
        def __exit__(self, *a): return False

    monkeypatch.setattr(work, "cursor", lambda: _Ctx())
    monkeypatch.setattr(work, "_planner_get", lambda sub: [])

    class _P:
        tenant_id = 1
        cross_tenant = True
    data = work._build_now(_P())
    # Three queries recorded: latest-per-agent, earliest-per-agent, planner.
    assert len(captured) == 3
    joined_sql = " \n ".join(captured)
    # Known slug join on sn_config.agents (archived_at IS NULL) is on every
    # read of sn_core.events in this module.
    assert "sn_config.agents" in joined_sql
    assert "archived_at IS NULL" in joined_sql
    # Busy filter on the two queries that drive the live-agent set. Not on
    # the planner-events query (defensive: that query is only consulted for
    # slugs already in the busy set).
    assert captured[0].count("sn_core.agent_runtime") == 1
    assert captured[1].count("sn_core.agent_runtime") == 1
    assert "status = 'busy'" in captured[0]
    assert "status = 'busy'" in captured[1]
    # With no rows returned the response is a well-formed empty one.
    assert data["agents"] == []
    assert data["schema_version"] == 1


# --------------------------------------------------------------------------- endpoint through TestClient

def test_work_now_shape(client, as_owner):
    r = client.get("/api/work/now")
    assert r.status_code == 200
    b = r.json()
    for k in ("as_of", "window_minutes", "agents", "attribution", "schema_version"):
        assert k in b
    assert b["schema_version"] == 1
    assert b["window_minutes"] == 60
    assert isinstance(b["agents"], list)
    attr = b["attribution"]
    for k in ("explicit_refs", "inferred_refs", "unattributed",
              "explicit_minutes", "inferred_minutes", "unattributed_minutes",
              "notes"):
        assert k in attr
    assert isinstance(attr["notes"], list)


def test_work_now_price_returns_501(client, as_owner):
    r = client.get("/api/work/now?price=1")
    assert r.status_code == 501
    body = r.json()
    detail = body.get("detail", {})
    assert detail.get("error") == "price_not_implemented"
    assert "cost" in detail.get("message", "").lower()


def test_work_now_fresh_param_accepted(client, as_owner):
    r = client.get("/api/work/now?fresh=1")
    assert r.status_code == 200
    assert r.json()["schema_version"] == 1
