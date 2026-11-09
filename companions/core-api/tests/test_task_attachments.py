"""Files on a planner task, against a database built from the tree's bootstrap.

A person answering a hitl question may attach files; the answer lands on the
task, so the files must too, readable by whoever may read that task and by
nobody else. The planner is a separate service: here it is a fake that serves
task 425 to the owner and the admin account and to nobody else, so the test
decides who may read a task and asserts core-api asks.

1. a stored file is a message_attachments row whose parent is the task, its
   bytes under tasks/<id>/; the same file again returns that row; the task
   lists it; /api/attachments/{id}/content serves the bytes
2. a caller the planner does not serve the task to cannot store, list or read
3. a task that does not exist takes no file; a name alone (no proof) neither
4. main.py answers the two paths itself, before the planner proxy
5. B02 converges a node carrying the two-parent CHECK to the three-parent one,
   and a row with two parents is refused
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from permission_db import ADMIN, OWNER, PLAIN, call, permission_db  # noqa: F401

_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))

TASK = 425
P = f"/api/planner/api/v1/tasks/{TASK}/attachments"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg body " * 20


@pytest.fixture
def node(permission_db, monkeypatch, tmp_path):  # noqa: F811
    from routes import attachments, planner, task_attachments

    asked = []

    def fake_planner(method, subpath, qs, body, remote_user, content_type,
                     operator_proof=None):
        asked.append((method, subpath, remote_user))
        if method == "GET" and subpath == f"api/v1/tasks/{TASK}" \
                and remote_user in (OWNER, ADMIN):
            return 200, b'{"id": %d}' % TASK, "application/json"
        return 404, b'{"detail": "not found"}', "application/json"

    monkeypatch.setattr(planner, "_forward", fake_planner)
    monkeypatch.setattr(task_attachments, "_ATTACHMENT_DIR", str(tmp_path))
    monkeypatch.setattr(attachments, "_ATTACHMENT_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(attachments.router)
    app.include_router(task_attachments.router)
    app.include_router(planner.router)
    with permission_db(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.message_attachments WHERE task_id IS NOT NULL")
    return app, tmp_path, asked


def _post(app, who, *, data=JPEG, name="price%20list.jpg", path=P, proved=True):
    from permission_db import session

    headers = session(who) if proved else {"Remote-User": who}
    headers.update({"X-Filename": name, "Content-Type": "image/jpeg"})
    return _raw(app, "POST", path, headers, data)


def _raw(app, method, path, headers, data=None):
    import asyncio

    import httpx

    async def go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as c:
            return await c.request(method, path, headers=headers, content=data)

    return asyncio.run(go())


def test_a_file_on_a_task_is_stored_listed_and_served(node, permission_db):  # noqa: F811
    app, root, _ = node
    r = _post(app, ADMIN)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["task_id"] == TASK and row["filename"] == "price list.jpg"
    assert row["content_type"] == "image/jpeg" and row["status"] == "approved"
    stored = list((root / "tasks" / str(TASK)).iterdir())
    assert [p.read_bytes() for p in stored] == [JPEG]
    assert not any(p.name.endswith(".part") for p in stored)
    with permission_db() as cur:
        cur.execute("SELECT message_id, desk_id, task_id, channel_type "
                    "FROM sn_core.message_attachments WHERE id = %s", (row["id"],))
        db_row = cur.fetchone()
    assert (db_row["message_id"], db_row["desk_id"], db_row["task_id"]) == (None, None, TASK)

    again = _post(app, ADMIN)
    assert again.status_code == 200 and again.json()["id"] == row["id"]
    assert len(list((root / "tasks" / str(TASK)).iterdir())) == 1

    listed = call(app, "GET", P, ADMIN)
    assert listed.status_code == 200, listed.text
    assert [a["id"] for a in listed.json()["attachments"]] == [row["id"]]

    for who in (ADMIN, OWNER):
        got = call(app, "GET", f"/api/attachments/{row['id']}/content", who)
        assert got.status_code == 200, (who, got.text)
        assert got.content == JPEG


def test_a_caller_who_may_not_read_the_task_gets_nothing(node):
    app, root, asked = node
    row = _post(app, ADMIN).json()
    asked.clear()
    assert _post(app, PLAIN, data=JPEG + b"other").status_code == 403
    assert call(app, "GET", P, PLAIN).status_code == 403
    got = call(app, "GET", f"/api/attachments/{row['id']}/content", PLAIN)
    assert got.status_code == 403, got.text
    # Decided by asking the planner as that caller, not by a guess.
    assert ("GET", f"api/v1/tasks/{TASK}", PLAIN) in asked
    assert len(list((root / "tasks" / str(TASK)).iterdir())) == 1


def test_no_file_on_a_missing_task_or_from_a_name_alone(node):
    app, root, _ = node
    r = _post(app, OWNER, path="/api/planner/api/v1/tasks/999/attachments")
    assert r.status_code == 404, r.text
    assert not (root / "tasks" / "999").exists()
    r = _post(app, ADMIN, proved=False)
    assert r.status_code in (401, 403), r.text
    assert not (root / "tasks").exists()


def test_main_answers_the_task_paths_before_the_planner_proxy(permission_db):  # noqa: F811
    import main

    paths = [r.path for r in main.app.routes if isinstance(r, APIRoute)]
    mine = paths.index("/api/planner/api/v1/tasks/{task_id}/attachments")
    proxy = paths.index("/api/planner/{subpath:path}")
    assert mine < proxy


def _psql_file(path: str) -> None:
    from permission_db import _pg_bin, _state

    out = subprocess.run(
        [os.path.join(_pg_bin(), "psql"), "-h", _state["work"], "-U", "postgres", "-X",
         "-q", "-v", "ON_ERROR_STOP=1", "-d", "smartnode", "-f", path],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-600:]


def test_b02_converges_the_one_parent_check_to_three_parents(permission_db):  # noqa: F811
    from permission_db import _ROOT

    def definition():
        with permission_db() as cur:
            cur.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                        "WHERE conname = 'message_attachments_one_parent_chk'")
            return cur.fetchone()["d"]

    assert "task_id" in definition()
    # A node installed before files on tasks carries the two-parent form.
    with permission_db(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.message_attachments")
        cur.execute("ALTER TABLE sn_core.message_attachments "
                    "DROP CONSTRAINT message_attachments_one_parent_chk")
        cur.execute("ALTER TABLE sn_core.message_attachments ADD CONSTRAINT "
                    "message_attachments_one_parent_chk "
                    "CHECK ((message_id IS NULL) <> (desk_id IS NULL))")
    _psql_file(str(_ROOT / "bootstrap/B02-sn-core-schema.sql"))
    assert "num_nonnulls(message_id, desk_id, task_id) = 1" in definition()
    # And a second run leaves it as it is.
    _psql_file(str(_ROOT / "bootstrap/B02-sn-core-schema.sql"))
    assert "num_nonnulls(message_id, desk_id, task_id) = 1" in definition()

    # The CHECK fires before the desk FK would: two parents are refused as such.
    import psycopg2
    with pytest.raises(psycopg2.errors.CheckViolation):
        with permission_db(commit=True) as cur:
            cur.execute("INSERT INTO sn_core.message_attachments "
                        "(desk_id, task_id, channel_type) VALUES (%s, %s, 'planner')",
                        (str(uuid.uuid4()), TASK))
