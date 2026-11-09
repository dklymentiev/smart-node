"""Integration tests for the three-status model and the dictionaries (0010).

Requires ``PLANNER_TEST_DSN``. Covers:

- the legacy wire keeps working: a PATCH with ``status: done`` lands as
  closed/completed and reads back as ``done``;
- the new wire: ``status: closed`` with an outcome, ``status: queued`` with a
  reason, and what each shows as on the legacy side;
- the move that used to need a hop: in_progress straight to rejected;
- the dictionaries: a node adds a reason without a release, and a legacy
  writer meets it through its legacy_stage;
- the trigger refuses a stage that contradicts its state, and the DB refuses
  an unknown reason.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest

from planner import statuses as st
from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo
from planner.pipeline import Pipeline, PipelineError
from planner.repos.tasks import TasksRepo

pytestmark = pytest.mark.asyncio

# Rows these tests add; removed up front so a rerun starts clean (the
# dictionaries are not in the per-test TRUNCATE, they are configuration).
_TEST_CODES = ("supplier", "customs", "other_done")


async def _clean_dictionaries(pool: asyncpg.Pool) -> None:
    await pool.execute("DELETE FROM wait_reasons WHERE code = ANY($1::text[])", list(_TEST_CODES))
    await pool.execute("DELETE FROM close_outcomes WHERE code = ANY($1::text[])", list(_TEST_CODES))


async def _seed_actor(pool: asyncpg.Pool, actor_id: str, kind: str = "human") -> None:
    await pool.execute(
        "INSERT INTO actors (id, display_name, type) VALUES ($1, $1, $2) ON CONFLICT DO NOTHING",
        actor_id, kind,
    )


@asynccontextmanager
async def _client(monkeypatch: pytest.MonkeyPatch, actor: str = "system"):
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    app = create_app()
    async with app.router.lifespan_context(app):
        _, plaintext = await ApiKeysRepo(app.state.pool).create(label="t", actor_id=actor)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"X-API-Key": plaintext},
        ) as c:
            yield c


# ---------------------------------------------------------------------------
# Storage model
# ---------------------------------------------------------------------------


async def test_legacy_write_lands_as_state_and_reads_back(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    t = await repo.create(title="legacy", status="awaiting_approval")
    assert (t.status, t.state, t.wait_reason, t.close_outcome) == (
        "awaiting_approval", "queued", "approval", None)

    p = Pipeline(pool)
    t = await p.status_change(t.id, "in_progress", actor="system")
    assert (t.status, t.state, t.wait_reason) == ("in_progress", "in_progress", None)

    t = await p.status_change(t.id, "done", actor="system", comment="delivered")
    assert (t.status, t.state, t.close_outcome) == ("done", "closed", "completed")
    assert t.completed_at is not None


async def test_in_progress_to_rejected_in_one_move(pool: asyncpg.Pool) -> None:
    """What content flows used to fake with a hop through escalated."""
    repo = TasksRepo(pool)
    t = await repo.create(title="cancel me", status="in_progress")
    t = await Pipeline(pool).status_change(t.id, "rejected", actor="system", comment="owner cancelled")
    assert (t.status, t.state, t.close_outcome) == ("rejected", "closed", "rejected")
    log = await pool.fetch(
        "SELECT old_value, new_value FROM task_log WHERE task_id = $1 AND action = 'status_changed'", t.id)
    assert [(r["old_value"], r["new_value"]) for r in log][-1] == ("in_progress", "rejected")


async def test_new_style_close_with_outcome(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    t = await repo.create(title="cancel", status="in_progress")
    t = await Pipeline(pool).status_change(
        t.id, "closed", actor="system", comment="decided against", close_outcome="cancelled")
    assert (t.status, t.state, t.close_outcome) == ("rejected", "closed", "cancelled")

    with pytest.raises(PipelineError, match="close_outcome"):
        await Pipeline(pool).status_change(t.id, "closed", actor="system", comment="x")


async def test_new_style_queue_with_reason_and_reason_change(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    t = await repo.create(title="stop", status="in_progress")
    p = Pipeline(pool)
    t = await p.status_change(t.id, "queued", actor="system", comment="no photo", wait_reason="person")
    assert (t.status, t.state, t.wait_reason) == ("blocked", "queued", "person")

    # Same stage, another reason: a real change, logged as state_changed.
    t = await p.status_change(t.id, "queued", actor="system", comment="service down", wait_reason="error")
    assert (t.status, t.wait_reason) == ("blocked", "error")
    kinds = {r["action"] for r in await pool.fetch(
        "SELECT action FROM task_log WHERE task_id = $1", t.id)}
    assert "state_changed" in kinds

    # Leaving the queue clears the reason (trigger), whichever vocabulary moved it.
    t = await p.status_change(t.id, "in_progress", actor="system")
    assert (t.state, t.wait_reason) == ("in_progress", None)


async def test_reason_only_with_queued_and_outcome_only_with_closed(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    t = await repo.create(title="rules", status="open")
    p = Pipeline(pool)
    with pytest.raises(PipelineError, match="wait_reason goes with status queued"):
        await p.status_change(t.id, "in_progress", actor="system", wait_reason="person")
    with pytest.raises(PipelineError, match="close_outcome goes with status closed"):
        await p.status_change(t.id, "in_progress", actor="system", close_outcome="completed")
    with pytest.raises(PipelineError, match="legacy stage name"):
        await p.status_change(t.id, "blocked", actor="system", comment="x", wait_reason="person")
    with pytest.raises(PipelineError, match="unknown wait_reason"):
        await p.status_change(t.id, "queued", actor="system", comment="x", wait_reason="no_such")


async def test_retired_names_collapse(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    t = await repo.create(title="old names", status="assessing")
    assert (t.status, t.state) == ("in_progress", "in_progress")
    t2 = await repo.create(title="old names 2", status="open")
    t2 = await Pipeline(pool).status_change(t2.id, "incident", actor="system")
    assert t2.status == "in_progress"


async def test_trigger_refuses_a_stage_that_contradicts_its_state(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    t = await repo.create(title="two truths", status="open")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "UPDATE tasks SET status = 'closed', close_outcome = 'completed', stage = 'blocked' WHERE id = $1",
            t.id)
    # A raw legacy writer still works: the trigger derives the state.
    await pool.execute("UPDATE tasks SET stage = 'done' WHERE id = $1", t.id)
    row = await pool.fetchrow(
        "SELECT status, wait_reason, close_outcome, stage, completed_at FROM tasks WHERE id = $1", t.id)
    assert (row["status"], row["close_outcome"], row["stage"]) == ("closed", "completed", "done")
    assert row["completed_at"] is not None


async def test_unknown_reason_is_refused_by_the_database(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    t = await repo.create(title="fk", status="open")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute("UPDATE tasks SET wait_reason = 'no_such' WHERE id = $1", t.id)


async def test_list_filters_by_stage_state_and_reason(pool: asyncpg.Pool) -> None:
    repo = TasksRepo(pool)
    a = await repo.create(title="a", status="awaiting_approval")
    b = await repo.create(title="b", status="in_progress")
    c = await repo.create(title="c", status="queued", wait_reason="error")
    assert {t.id for t in await repo.list(status="queued")} == {a.id, c.id}
    assert {t.id for t in await repo.list(status="blocked")} == {c.id}
    assert {t.id for t in await repo.list(wait_reason="approval")} == {a.id}
    assert {t.id for t in await repo.list(status="in_progress")} == {b.id}
    counts = await repo.count_by_status()
    assert counts == {"awaiting_approval": 1, "in_progress": 1, "blocked": 1}


# ---------------------------------------------------------------------------
# Dictionaries: a node extends the model without a release
# ---------------------------------------------------------------------------


async def test_node_adds_a_reason_and_legacy_readers_still_understand(pool: asyncpg.Pool) -> None:
    await _clean_dictionaries(pool)
    repo = TasksRepo(pool)
    d = st.DictionariesRepo(pool)
    row = await d.create("wait_reasons", {
        "code": "supplier", "label": "Waiting on a supplier",
        "legacy_stage": "blocked", "effects": {"comment_required": True}}, actor="admin")
    assert row["code"] == "supplier" and row["effects"] == {"comment_required": True}

    t = await repo.create(title="bottles", status="in_progress")
    t = await Pipeline(pool).status_change(
        t.id, "queued", actor="system", comment="glass not delivered", wait_reason="supplier")
    assert (t.status, t.state, t.wait_reason) == ("blocked", "queued", "supplier")
    assert await st.comment_required(pool, "queued", "supplier", None) is True

    # In use: cannot be deleted, can be disabled.
    with pytest.raises(st.DictionaryInUse):
        await d.delete("wait_reasons", "supplier")
    row = await d.update("wait_reasons", "supplier", {"enabled": False}, actor="admin")
    assert row["enabled"] is False
    assert all(r["code"] != "supplier" for r in await d.list("wait_reasons"))
    assert any(r["code"] == "supplier" for r in await d.list("wait_reasons", include_disabled=True))


async def test_dictionary_validation(pool: asyncpg.Pool) -> None:
    await _clean_dictionaries(pool)
    d = st.DictionariesRepo(pool)
    with pytest.raises(st.DictionaryError, match="code must be"):
        await d.create("wait_reasons", {"code": "Bad Code", "label": "x"})
    with pytest.raises(st.DictionaryError, match="legacy_stage"):
        await d.create("close_outcomes", {"code": "weird", "label": "x", "legacy_stage": "blocked"})
    with pytest.raises(st.DictionaryError, match="already the default"):
        await d.create("close_outcomes", {"code": "other_done", "label": "x",
                                          "legacy_stage": "done", "legacy_default": True})
    with pytest.raises(st.DictionaryNotFound):
        await d.update("wait_reasons", "nope", {"label": "x"})
    with pytest.raises(st.DictionaryError, match="unknown dictionary"):
        await d.list("tasks")


async def test_comment_required_follows_the_dictionaries(pool: asyncpg.Pool) -> None:
    assert await st.comment_required(pool, "done") is True
    assert await st.comment_required(pool, "rejected") is True
    assert await st.comment_required(pool, "blocked") is True
    assert await st.comment_required(pool, "escalated") is True
    assert await st.comment_required(pool, "awaiting_approval") is False
    assert await st.comment_required(pool, "pending_review") is False
    assert await st.comment_required(pool, "in_progress") is False
    assert await st.comment_required(pool, "queued") is False
    assert await st.comment_required(pool, "closed", None, "duplicate") is True


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_rest_task_carries_both_vocabularies(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLANNER_STATUS_COMPAT", raising=False)
    # The caller is the owner here; a non-owner's filing lands awaiting_approval.
    monkeypatch.setenv("PLANNER_OWNER_ACTOR_ID", "system")
    async with _client(monkeypatch) as c:
        r = await c.post("/api/v1/tasks", json={"title": "wire", "status": "open"})
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        body = r.json()
        assert (body["status"], body["stage"], body["state"]) == ("open", "open", "queued")

        r = await c.patch(f"/api/v1/tasks/{tid}", json={"status": "in_progress"})
        assert r.status_code == 200, r.text
        # A close needs a report, in either vocabulary.
        r = await c.patch(f"/api/v1/tasks/{tid}", json={"status": "closed", "close_outcome": "completed"})
        assert r.status_code == 400 and "comment is required" in r.text
        r = await c.patch(f"/api/v1/tasks/{tid}",
                          json={"status": "closed", "close_outcome": "completed", "comment": "shipped"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["status"], body["state"], body["close_outcome"]) == ("done", "closed", "completed")

        # Filters in both vocabularies.
        r = await c.get("/api/v1/tasks", params={"status_filter": "closed"})
        assert [t["id"] for t in r.json()] == [tid]
        r = await c.get("/api/v1/tasks", params={"status_filter": "done"})
        assert [t["id"] for t in r.json()] == [tid]
        r = await c.get("/api/v1/tasks", params={"status_filter": "nonsense"})
        assert r.status_code == 400

        monkeypatch.setenv("PLANNER_STATUS_COMPAT", "new")
        r = await c.get(f"/api/v1/tasks/{tid}")
        assert r.json()["status"] == "closed" and r.json()["stage"] == "done"

        # A request chooses its own vocabulary, whatever the node default is.
        r = await c.get(f"/api/v1/tasks/{tid}", headers={"X-Status-Vocabulary": "legacy"})
        assert r.json()["status"] == "done"
        monkeypatch.delenv("PLANNER_STATUS_COMPAT", raising=False)
        r = await c.get(f"/api/v1/tasks/{tid}", headers={"X-Status-Vocabulary": "new"})
        assert r.json()["status"] == "closed"
        r = await c.get(f"/api/v1/tasks/{tid}", params={"vocabulary": "new"})
        assert r.json()["status"] == "closed"
        r = await c.get(f"/api/v1/tasks/{tid}", params={"vocabulary": "nonsense"})
        assert r.json()["status"] == "done"
        r = await c.get("/api/v1/tasks", params={"status_filter": "closed", "vocabulary": "new"})
        assert [t["status"] for t in r.json()] == ["closed"]


async def test_rest_agent_may_not_set_a_persons_outcome(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    await _seed_actor(pool, "bot", "agent")
    repo = TasksRepo(pool)
    t = await repo.create(title="agent close", status="in_progress")
    async with _client(monkeypatch, actor="bot") as c:
        r = await c.patch(f"/api/v1/tasks/{t.id}",
                          json={"status": "closed", "close_outcome": "not_possible", "comment": "cannot"})
        assert r.status_code == 403, r.text
        r = await c.patch(f"/api/v1/tasks/{t.id}",
                          json={"status": "queued", "wait_reason": "person", "comment": "address missing"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "blocked" and r.json()["wait_reason"] == "person"
        # The legacy word still works for an agent: it is the pipeline's own hand-off.
        r = await c.patch(f"/api/v1/tasks/{t.id}", json={"status": "in_progress"})
        assert r.status_code == 200
        r = await c.patch(f"/api/v1/tasks/{t.id}", json={"status": "done", "comment": "made it"})
        assert r.status_code == 200 and r.json()["close_outcome"] == "completed"


async def test_rest_dictionaries_owner_only(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    await _clean_dictionaries(pool)
    monkeypatch.setenv("PLANNER_OWNER_ACTOR_ID", "boss")
    await _seed_actor(pool, "boss", "human")
    await _seed_actor(pool, "bot", "agent")
    async with _client(monkeypatch, actor="bot") as c:
        r = await c.get("/api/v1/dictionaries/wait-reasons")
        assert r.status_code == 200 and {x["code"] for x in r.json()} >= {"approval", "review", "person", "error"}
        r = await c.post("/api/v1/dictionaries/wait-reasons", json={"code": "x", "label": "X"})
        assert r.status_code == 403
    async with _client(monkeypatch, actor="boss") as c:
        r = await c.post("/api/v1/dictionaries/wait-reasons",
                         json={"code": "customs", "label": "Customs", "effects": {"notify": "owner"}})
        assert r.status_code == 201, r.text
        assert r.json()["legacy_stage"] == "blocked"
        r = await c.patch("/api/v1/dictionaries/wait-reasons/customs", json={"sort_order": 5})
        assert r.status_code == 200 and r.json()["sort_order"] == 5
        r = await c.get("/api/v1/dictionaries/close-outcomes/completed")
        assert r.status_code == 200 and r.json()["legacy_stage"] == "done"
        r = await c.delete("/api/v1/dictionaries/wait-reasons/customs")
        assert r.status_code == 204
        r = await c.get("/api/v1/dictionaries/nope")
        assert r.status_code == 404
