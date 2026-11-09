"""Tests for /api/project-state/collect (first slice).

Three bands:

  * pure logic: timestamp parsing, window-spec parsing (ISO, bare date,
    relative shorthand), slug-not-found 404 shape.
  * collector logic with stubbed planner reads: a sample Flint-like
    project returns the expected `goals`, `linked_counts`, `moved`
    shape; the by-tag blind spot is reported in `notes`; the step 4-8
    pending note is present; sources_completed lists exactly the three
    sources collected in this slice.
  * endpoint shape (through TestClient against the real DB, with the
    planner reader stubbed): response has `project`, `goals`,
    `linked_counts`, `moved`, `sources_completed`, `notes`,
    `schema_version`, and the window echoes `since` / `until`.

The planner HTTP surface is replaced with a dict in every test, so the
test runs even when the planner service is down.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lib import project_state as collector
from routes import project_state as route


# --------------------------------------------------------------------------- time parsing

def test_parse_ts_handles_zulu_and_offset():
    dt = collector._parse_ts("2026-10-10T04:03:40.790314Z")
    assert dt == datetime(2026, 10, 10, 4, 3, 40, 790314, tzinfo=timezone.utc)
    dt2 = collector._parse_ts("2026-10-10T04:03:40+02:00")
    assert dt2 == datetime(2026, 10, 10, 2, 3, 40, tzinfo=timezone.utc)


def test_parse_ts_returns_none_on_garbage():
    assert collector._parse_ts(None) is None
    assert collector._parse_ts("") is None
    assert collector._parse_ts("not a date") is None
    assert collector._parse_ts(42) is None


# --------------------------------------------------------------------------- window parsing

def test_parse_window_relative_shorthand():
    now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert route._parse_window("-7d", now) == datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert route._parse_window("-72h", now) == datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert route._parse_window("-30m", now) == datetime(2026, 10, 17, 11, 30, 0, tzinfo=timezone.utc)


def test_parse_window_bare_date_is_midnight_utc():
    now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert route._parse_window("2026-10-10", now) == datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_window_iso_zulu_and_offset():
    now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert route._parse_window("2026-10-10T00:00:00Z", now) == datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)
    assert route._parse_window("2026-10-10T02:00:00+02:00", now) == datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)


def test_parse_window_rejects_garbage():
    from fastapi import HTTPException
    now = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(HTTPException) as excinfo:
        route._parse_window("yesterday", now)
    assert excinfo.value.status_code == 400
    with pytest.raises(HTTPException):
        route._parse_window("", now)


# --------------------------------------------------------------------------- collector logic

def _stub_planner(monkeypatch, map_: dict):
    """Replace project_state._planner_get with a dict lookup.

    `map_` keys are the subpath passed to _planner_get, values are the
    JSON the stub returns. An unknown key returns None, matching the
    collector's "surface blind spot" behaviour.
    """
    def fake(subpath):
        key = subpath.lstrip("/")
        for pattern, value in map_.items():
            if key.startswith(pattern):
                return value
        return None
    monkeypatch.setattr(collector, "_planner_get", fake)


def test_collect_slug_not_found_returns_null_project(monkeypatch):
    _stub_planner(monkeypatch, {
        "api/v1/projects": [{"id": 1, "slug": "other", "title": "Other"}],
    })
    out = collector.collect(
        "flint",
        datetime(2026, 10, 10, tzinfo=timezone.utc),
        datetime(2026, 10, 13, tzinfo=timezone.utc),
    )
    assert out["project"] is None
    assert any("not found" in n for n in out["notes"])
    assert out["schema_version"] == collector.COLLECTOR_SCHEMA_VERSION


def test_collect_three_source_shape(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    objectives = [
        {"id": 382, "status": "active", "type": None,
         "title": "flintagent.dev: releases on the site",
         "updated_at": "2026-10-12T00:00:00Z"},
    ]
    # Two tasks: one directly on the project, one only via objective.
    direct = {
        "id": 5526, "title": "F2 changelog", "status": "done",
        "project_id": 24, "objective_id": 382, "assignee": "agent-a",
        "created_at": "2026-10-11T00:00:00Z",
        "updated_at": "2026-10-13T04:00:00Z",
    }
    only_by_obj = {
        "id": 9000, "title": "orphan under same objective", "status": "open",
        "project_id": None, "objective_id": 382, "assignee": None,
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-10-12T12:00:00Z",
    }
    outside_window = {
        "id": 100, "title": "ancient", "status": "done",
        "project_id": 24, "objective_id": 382, "assignee": None,
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
    }

    # Comments on moved tasks: 5526 has an owner comment in-window and an
    # out-of-window agent comment; 9000 has nothing. The out-of-window one
    # must be dropped and 5526's last_by_role.owner must point at the one
    # inside the window.
    comments_5526 = [
        {"id": 20000, "task_id": 5526, "author": "admin",
         "content": "approve the F2 changelog", "comment_type": None,
         "created_at": "2026-10-12T09:30:00Z"},
        {"id": 20001, "task_id": 5526, "author": "agent-a",
         "content": "work started", "comment_type": None,
         "created_at": "2026-04-01T00:00:00Z"},
    ]
    comments_9000 = []  # task returns empty list

    def fake(subpath):
        key = subpath.lstrip("/")
        if key.startswith("api/v1/projects"):
            return [project]
        if key.startswith("api/v1/objectives"):
            return objectives
        if key == "api/v1/tasks/5526/comments":
            return comments_5526
        if key == "api/v1/tasks/9000/comments":
            return comments_9000
        if key.startswith("api/v1/tasks/search"):
            # No cross-project mentions in this scenario.
            return []
        if "project_id=24" in key:
            return [direct, outside_window]
        if "objective_id=382" in key:
            return [direct, only_by_obj]
        return None

    monkeypatch.setattr(collector, "_planner_get", fake)
    # Mesh wired to a non-empty URL so step 6 runs, but every mesh query
    # returns an empty list. The memory step then contributes nothing to
    # the response, and no "mesh unreachable" or "MESH_API_URL empty" note.
    monkeypatch.setattr(collector, "MESH_URL", "http://mesh.example")
    monkeypatch.setattr(collector, "_mesh_get", lambda path, workspace="*": [])
    # Doci wired to a non-empty URL so step 7 runs, but every doci folder
    # read returns a well-formed "no documents" payload. The documents
    # step contributes nothing and no failure note surfaces.
    monkeypatch.setattr(collector, "DOCI_URL", "http://doci.example")
    monkeypatch.setattr(
        collector, "_doci_get",
        lambda path, remote_user="admin": {"success": True, "documents": []})
    # Channels (step 8) stubbed to an empty read so the step runs but
    # contributes nothing. The underlying helpers reach `db.cursor`, which
    # is not available in this unit test, so both are replaced.
    monkeypatch.setattr(
        collector, "_owner_identifiers",
        lambda: {"web-chat": "web:admin"})
    monkeypatch.setattr(
        collector, "_messages_rows_in_window",
        lambda since_dt, until_dt, limit, owner: [])

    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    until = datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc)
    out = collector.collect("flint", since, until)

    assert out["project"] == {"id": 24, "slug": "flint", "title": "Flint"}
    assert out["schema_version"] == collector.COLLECTOR_SCHEMA_VERSION
    assert out["sources_completed"] == [
        "goals", "linked_tasks", "moved", "comments", "mentions", "memory",
        "documents", "channels"]
    # mentions: wired through, nothing to find in this scenario.
    assert out["mentions"] == []
    # memory: wired through, mesh stub returned nothing.
    assert out["memory"] == []
    # documents: wired through, doci stub returned nothing.
    assert out["documents"] == []
    # channels: wired through, stub returned no messages.
    assert out["channels"] == []

    # goals: one row for objective 382, with totals from the linked set.
    assert len(out["goals"]) == 1
    g = out["goals"][0]
    assert g["id"] == 382
    assert g["task_count"] == 3  # direct + only_by_obj + outside_window all under 382
    assert g["closed_count"] == 2  # direct + outside_window are done
    assert g["last_task_move"] == "2026-10-13T04:00:00Z"

    # linked_counts: no double-counting, by_tag None, only-by surfaces the orphan.
    lc = out["linked_counts"]
    assert lc["total"] == 3
    assert lc["by_project"] == 2
    assert lc["by_objective"] == 3
    assert lc["by_tag"] is None
    assert lc["only_by_objective_not_project"] == 1

    # moved: only the two tasks whose updated_at is in the window.
    assert [t["id"] for t in out["moved"]] == [5526, 9000]
    assert out["moved"][0]["is_new_in_window"] is True
    assert out["moved"][1]["is_new_in_window"] is False

    # comments: 5526 appears with only its in-window owner comment; 9000 is
    # omitted because it had none in-window.
    assert len(out["comments"]) == 1
    c_entry = out["comments"][0]
    assert c_entry["task_id"] == 5526
    assert [c["id"] for c in c_entry["comments"]] == [20000]
    assert c_entry["comments"][0]["role"] == "owner"
    assert c_entry["last_by_role"] == {"owner": 20000}

    # notes: by_tag gap present. No more pending-source note now that
    # step 8 completes (with empty stubs).
    assert any("by_tag" in n for n in out["notes"])
    assert not any("sources pending" in n for n in out["notes"])

    # Derived fields: no awaiting_approval / escalated among the stubbed
    # tasks, so waiting_for_owner is empty. There IS at least one task
    # that moved in-window (5526), so where_it_stopped is None.
    assert out["waiting_for_owner"] == []
    assert out["where_it_stopped"] is None


# --------------------------------------------------------------------------- comments (step 5)

def test_comment_role_classifies_known_actors():
    assert collector._comment_role("admin") == "owner"
    assert collector._comment_role("runner") == "operator"
    assert collector._comment_role("agent-a") == "agent"
    assert collector._comment_role("agent-b") == "agent"
    assert collector._comment_role("component:core-api") == "system"
    assert collector._comment_role("system") == "system"
    assert collector._comment_role("pipeline") == "system"
    assert collector._comment_role(None) == "unknown"
    assert collector._comment_role("") == "unknown"


def test_collect_comments_filters_by_window_and_classifies(monkeypatch):
    """A moved task with mixed-role comments: window filter + roles + last_by_role."""
    moved = [
        {"id": 5526, "title": "F2 changelog"},
        {"id": 9000, "title": "orphan"},
    ]
    # Mix of in/out window; mix of roles. Order on the wire is not sorted.
    comments_5526 = [
        {"id": 1, "author": "agent-a", "content": "started",
         "comment_type": None, "created_at": "2026-10-11T10:00:00Z"},
        {"id": 2, "author": "admin", "content": "approve",
         "comment_type": None, "created_at": "2026-10-12T09:30:00Z"},
        {"id": 3, "author": "runner", "content": "status cycled",
         "comment_type": "status_change", "created_at": "2026-10-12T10:00:00Z"},
        {"id": 4, "author": "agent-a", "content": "later agent note",
         "comment_type": None, "created_at": "2026-10-13T00:00:00Z"},
        {"id": 5, "author": "admin", "content": "ancient owner comment",
         "comment_type": None, "created_at": "2025-01-01T00:00:00Z"},
        {"id": 6, "author": "admin", "content": "after the window",
         "comment_type": None, "created_at": "2026-10-20T00:00:00Z"},
    ]
    comments_9000 = []

    def fake(subpath):
        key = subpath.lstrip("/")
        if key == "api/v1/tasks/5526/comments":
            return comments_5526
        if key == "api/v1/tasks/9000/comments":
            return comments_9000
        return None

    monkeypatch.setattr(collector, "_planner_get", fake)
    out, notes = collector._collect_comments(
        moved,
        datetime(2026, 10, 10, tzinfo=timezone.utc),
        datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    # Only 5526 appears; 9000 is dropped because it had nothing in-window.
    assert len(out) == 1
    e = out[0]
    assert e["task_id"] == 5526
    assert e["task_title"] == "F2 changelog"
    # In-window comments are 1, 2, 3, 4 (5 before window, 6 after). Sorted oldest-first.
    assert [c["id"] for c in e["comments"]] == [1, 2, 3, 4]
    assert [c["role"] for c in e["comments"]] == ["agent", "owner", "operator", "agent"]
    # last_by_role picks the newest id per role.
    assert e["last_by_role"] == {"agent": 4, "owner": 2, "operator": 3}
    assert notes == []


def test_collect_comments_notes_a_read_failure(monkeypatch):
    moved = [{"id": 10, "title": "ten"}, {"id": 11, "title": "eleven"}]

    def fake(subpath):
        key = subpath.lstrip("/")
        if key == "api/v1/tasks/10/comments":
            return [{"id": 1, "author": "admin", "content": "ok",
                     "comment_type": None, "created_at": "2026-10-11T00:00:00Z"}]
        # task 11 fails: _planner_get returns None on error, we must note it.
        return None

    monkeypatch.setattr(collector, "_planner_get", fake)
    out, notes = collector._collect_comments(
        moved,
        datetime(2026, 10, 10, tzinfo=timezone.utc),
        datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert [e["task_id"] for e in out] == [10]
    assert len(notes) == 1
    assert "11" in notes[0]
    assert "comments" in notes[0]


# --------------------------------------------------------------------------- mentions (step 4)

def test_collect_mentions_drops_linked_and_own_project(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    # Mentions search returns tasks across projects; the collector must drop
    # (a) tasks already in the project's linked set, (b) tasks whose own
    # project_id is this project, and (c) tasks outside the window.
    returned = [
        {"id": 7001, "title": "Smart Node: install Flint as a runtime",
         "status": "done", "project_id": 79, "objective_id": None,
         "assignee": "max", "created_at": "2026-10-10T00:00:00Z",
         "updated_at": "2026-10-11T15:00:00Z"},
        {"id": 7002, "title": "Agents cannot start under Flint", "status": "open",
         "project_id": 79, "objective_id": None, "assignee": None,
         "created_at": "2026-10-12T00:00:00Z",
         "updated_at": "2026-10-13T10:00:00Z"},
        # already linked -> drop
        {"id": 5526, "title": "F2 changelog", "status": "done",
         "project_id": 24, "objective_id": 382,
         "updated_at": "2026-10-13T04:00:00Z"},
        # own project -> drop (should not reach here, but defensive filter)
        {"id": 5000, "title": "flint internal", "status": "open",
         "project_id": 24, "updated_at": "2026-10-13T04:00:00Z"},
        # outside window -> drop
        {"id": 8000, "title": "Flint audit, April", "status": "done",
         "project_id": 79, "updated_at": "2026-04-01T00:00:00Z"},
    ]

    def fake(subpath):
        key = subpath.lstrip("/")
        if key.startswith("api/v1/tasks/search"):
            if "q=flint" in key or "q=Flint" in key:
                return returned
            return []
        return None

    monkeypatch.setattr(collector, "_planner_get", fake)

    out, notes = collector._collect_mentions(
        project,
        linked_ids={5526, 9000},
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    # Only 7002 and 7001 remain, sorted newest-first.
    assert [m["id"] for m in out] == [7002, 7001]
    assert out[0]["source_project_id"] == 79
    assert out[0]["matched_terms"] == ["flint"]  # title "Flint" is case-insensitive dup of slug, skipped
    assert notes == []


def test_collect_mentions_notes_search_cap(monkeypatch):
    project = {"id": 79, "slug": "smart-node", "title": "Smart Node"}
    # Return exactly the cap: the collector must note the truncation on both
    # terms (slug and title, which here are distinct).
    at_cap = [
        {"id": i, "title": f"t{i}", "status": "open", "project_id": 24,
         "updated_at": "2026-10-12T00:00:00Z"}
        for i in range(1, collector._MENTIONS_LIMIT + 1)
    ]

    def fake(subpath):
        key = subpath.lstrip("/")
        if key.startswith("api/v1/tasks/search"):
            return at_cap
        return None

    monkeypatch.setattr(collector, "_planner_get", fake)
    out, notes = collector._collect_mentions(
        project,
        linked_ids=set(),
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert len(out) == collector._MENTIONS_LIMIT
    assert len(notes) == 1
    assert "smart-node" in notes[0] and "Smart Node" in notes[0]
    assert str(collector._MENTIONS_LIMIT) in notes[0]


def test_collect_mentions_notes_search_failure(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Flint"}

    def fake(subpath):
        key = subpath.lstrip("/")
        if key.startswith("api/v1/tasks/search"):
            # planner read returns None on error
            return None
        return None

    monkeypatch.setattr(collector, "_planner_get", fake)
    out, notes = collector._collect_mentions(
        project,
        linked_ids=set(),
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert out == []
    # One term tried (title is a case-insensitive duplicate of the slug and
    # is skipped). One failure note is enough; the test does not pin the
    # count beyond "at least one".
    assert any("mentions:" in n and "failed" in n for n in notes)


# --------------------------------------------------------------------------- memory (step 6)

def test_title_to_project_slug_matches_mesh_convention():
    # Mesh project tags are kebab-case: 'Smart Node' -> 'smart-node'.
    assert collector._title_to_project_slug("Smart Node") == "smart-node"
    assert collector._title_to_project_slug("Flint") == "flint"
    assert collector._title_to_project_slug("  Multi   Word  ") == "multi-word"
    assert collector._title_to_project_slug("No-change-needed") == "no-change-needed"
    # Non-alnum collapses to a single dash; trailing dashes trimmed.
    assert collector._title_to_project_slug("A/B/C!!") == "a-b-c"
    assert collector._title_to_project_slug("") == ""


def test_collect_memory_dedups_and_filters_window(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    # Two docs returned by two different queries; one is in window and
    # must appear once (dedup by guid, merged matched_terms). The second
    # one is out of window and must drop out. The third one has no
    # updated_at and must fall back to created_at.
    doc_in = {
        "guid": "doc_in_window", "workspace": "agent-a",
        "filename": None, "source": "mcp",
        "tags": ["type:note", "project:flint", "date:2026-10-12"],
        "content": "# Flint release pass\nSome body text here.",
        "created_at": "2026-10-10T00:00:00Z",
        "updated_at": "2026-10-12T15:00:00Z",
    }
    doc_out = {
        "guid": "doc_out_of_window", "workspace": "agent-a",
        "filename": "old-note.md", "source": "mcp",
        "tags": ["type:note", "project:flint"],
        "content": "ancient",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
    }
    doc_no_upd = {
        "guid": "doc_no_updated_at", "workspace": "agent-a",
        "filename": "fallback.md", "source": "mcp",
        "tags": ["type:note", "project:flint"],
        "content": "no updated_at here",
        "created_at": "2026-10-13T08:00:00Z",
        "updated_at": None,
    }
    calls: list[str] = []

    def fake(path, workspace="*"):
        calls.append(path)
        # Return the three docs on BOTH project= and tag= queries for the
        # slug term; return nothing for the title term (title_slug equals
        # slug "flint" so step 6 does not call it twice).
        if "project=flint" in path:
            return [doc_in, doc_out, doc_no_upd]
        if "tag=project%3Aflint" in path or "tag=project:flint" in path:
            return [doc_in]  # dedup with the one above
        return []

    monkeypatch.setattr(collector, "MESH_URL", "http://mesh.example")
    monkeypatch.setattr(collector, "_mesh_get", fake)

    out, notes = collector._collect_memory(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    # Two docs in window (in and no_upd); dedup worked (doc_in appears once).
    guids = {r["guid"] for r in out}
    assert guids == {"doc_in_window", "doc_no_updated_at"}
    # doc_in matched BOTH queries -> matched_terms has both labels.
    in_entry = next(r for r in out if r["guid"] == "doc_in_window")
    assert sorted(in_entry["matched_terms"]) == sorted([
        "project:flint", "tag:flint"])
    # Title is extracted from filename or first non-empty content line.
    assert in_entry["title"] == "Flint release pass"
    # Type comes from the type:X tag.
    assert in_entry["type"] == "note"
    # No step notes when nothing failed and nothing hit the cap.
    assert notes == []


def test_collect_memory_skipped_when_mesh_url_empty(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    monkeypatch.setattr(collector, "MESH_URL", "")

    def should_not_be_called(path, workspace="*"):
        raise AssertionError(f"mesh must not be reached when MESH_URL is empty: {path}")

    monkeypatch.setattr(collector, "_mesh_get", should_not_be_called)
    out, notes = collector._collect_memory(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert out == []
    assert any("MESH_API_URL" in n and "skipped" in n for n in notes)


def test_collect_memory_notes_read_failure_and_cap(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Smart Node"}
    # title_slug "smart-node" is distinct from slug "flint", so step 6
    # issues four queries: project=flint, tag=project:flint,
    # project=smart-node, tag=project:smart-node.
    # Make one fail and one hit the cap.
    cap_rows = [
        {
            "guid": f"doc_{i:03d}",
            "workspace": "default",
            "filename": None,
            "source": "mcp",
            "tags": ["type:note", "project:smart-node"],
            "content": f"body {i}",
            "created_at": "2026-10-12T00:00:00Z",
            "updated_at": "2026-10-12T00:00:00Z",
        }
        for i in range(collector._MESH_PAGE_LIMIT)
    ]

    def fake(path, workspace="*"):
        if "project=flint" in path and "tag=" not in path:
            return None  # failure
        if "project=smart-node" in path and "tag=" not in path:
            return cap_rows  # hit the 100 cap
        return []

    monkeypatch.setattr(collector, "MESH_URL", "http://mesh.example")
    monkeypatch.setattr(collector, "_mesh_get", fake)
    out, notes = collector._collect_memory(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    # The cap batch landed in window: all 100 appear.
    assert len(out) == collector._MESH_PAGE_LIMIT
    # One failure note mentions the failed query path.
    assert any("memory:" in n and "failed" in n and "project=flint" in n
               for n in notes)
    # One cap note mentions the limit value and the query that hit it.
    assert any("memory:" in n and str(collector._MESH_PAGE_LIMIT) in n
               and "project=smart-node" in n for n in notes)


# --------------------------------------------------------------------------- documents (step 7)

def test_collect_doci_filters_window_and_folders(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    # Three rows under the "flint" folder:
    #   - doc_type=document in window -> kept
    #   - doc_type=folder (always skipped)
    #   - doc_type=document out of window -> dropped
    rows_flint = [
        {
            "guid": "doc-in", "path": "flint/notes.md",
            "doc_type": "document", "title": "In window note",
            "summary": "s1", "created_by": "admin",
            "created_at": "2026-10-11 00:00:00.0-05",
            "updated_at": "2026-10-12 15:00:00.0-05",
        },
        {
            "guid": "folder-row", "path": "flint/audits",
            "doc_type": "folder", "title": "Audits",
            "summary": None, "created_by": "system",
            "created_at": "2026-10-12 00:00:00.0-05",
            "updated_at": "2026-10-12 00:00:00.0-05",
        },
        {
            "guid": "doc-out", "path": "flint/old.md",
            "doc_type": "document", "title": "Old",
            "summary": None, "created_by": "unknown",
            "created_at": "2026-04-01 00:00:00.0-05",
            "updated_at": "2026-04-01 00:00:00.0-05",
        },
    ]

    def fake(path, remote_user="admin"):
        if "folder=flint" in path:
            return {"success": True, "documents": rows_flint}
        # title_slug == slug for Flint, so no second query must happen.
        raise AssertionError(f"unexpected doci call: {path}")

    monkeypatch.setattr(collector, "DOCI_URL", "http://doci.example")
    monkeypatch.setattr(collector, "_doci_get", fake)

    out, notes = collector._collect_doci(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert [r["guid"] for r in out] == ["doc-in"]
    r = out[0]
    assert r["path"] == "flint/notes.md"
    assert r["title"] == "In window note"
    assert r["doc_type"] == "document"
    assert r["matched_folders"] == ["flint"]
    # Timestamps are normalised to Zulu.
    assert r["updated_at"].endswith("Z")
    assert r["created_at"].endswith("Z")
    assert notes == []


def test_collect_doci_dedups_across_two_folders(monkeypatch):
    # title "Smart Node" -> title_slug "smart-node", distinct from slug
    # "sn". Both folders exist; one document appears in both.
    project = {"id": 79, "slug": "sn", "title": "Smart Node"}
    shared = {
        "guid": "doc-shared", "path": "sn/overview.md",
        "doc_type": "document", "title": "Overview",
        "summary": None, "created_by": "admin",
        "created_at": "2026-10-10 09:00:00.0-05",
        "updated_at": "2026-10-12 11:00:00.0-05",
    }
    only_title = {
        "guid": "doc-title-only", "path": "smart-node/plan.md",
        "doc_type": "document", "title": "Plan",
        "summary": None, "created_by": "admin",
        "created_at": "2026-10-11 09:00:00.0-05",
        "updated_at": "2026-10-13 08:00:00.0-05",
    }

    def fake(path, remote_user="admin"):
        if "folder=sn" in path:
            return {"success": True, "documents": [shared]}
        if "folder=smart-node" in path:
            return {"success": True, "documents": [shared, only_title]}
        raise AssertionError(f"unexpected doci call: {path}")

    monkeypatch.setattr(collector, "DOCI_URL", "http://doci.example")
    monkeypatch.setattr(collector, "_doci_get", fake)

    out, notes = collector._collect_doci(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    guids = {r["guid"] for r in out}
    assert guids == {"doc-shared", "doc-title-only"}
    shared_out = next(r for r in out if r["guid"] == "doc-shared")
    assert sorted(shared_out["matched_folders"]) == ["smart-node", "sn"]
    only_out = next(r for r in out if r["guid"] == "doc-title-only")
    assert only_out["matched_folders"] == ["smart-node"]
    assert notes == []


def test_collect_doci_skipped_when_doci_url_empty(monkeypatch):
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    monkeypatch.setattr(collector, "DOCI_URL", "")

    def should_not_be_called(path, remote_user="admin"):
        raise AssertionError(
            f"doci must not be reached when DOCI_URL is empty: {path}")

    monkeypatch.setattr(collector, "_doci_get", should_not_be_called)
    out, notes = collector._collect_doci(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert out == []
    assert any("DOCI_URL" in n and "skipped" in n for n in notes)


def test_collect_doci_notes_read_failure_and_cap(monkeypatch):
    # slug "flint" and title_slug "smart-node" are distinct so two folder
    # queries happen. First returns None (connection failure); second
    # returns a payload that hits the walk cap.
    project = {"id": 99, "slug": "flint", "title": "Smart Node"}
    cap_rows = [
        {
            "guid": f"doc-{i:04d}",
            "path": f"smart-node/{i}.md",
            "doc_type": "document",
            "title": f"T{i}",
            "summary": None,
            "created_by": "admin",
            "created_at": "2026-10-12 00:00:00.0-05",
            "updated_at": "2026-10-12 00:00:00.0-05",
        }
        for i in range(collector._DOCI_FOLDER_WALK_CAP)
    ]

    def fake(path, remote_user="admin"):
        if "folder=flint" in path:
            return None
        if "folder=smart-node" in path:
            return {"success": True, "documents": cap_rows}
        raise AssertionError(f"unexpected doci call: {path}")

    monkeypatch.setattr(collector, "DOCI_URL", "http://doci.example")
    monkeypatch.setattr(collector, "_doci_get", fake)
    out, notes = collector._collect_doci(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    # Cap rows fit in window and all appear.
    assert len(out) == collector._DOCI_FOLDER_WALK_CAP
    assert any("documents:" in n and "failed" in n and "flint" in n
               for n in notes)
    assert any("documents:" in n and str(collector._DOCI_FOLDER_WALK_CAP) in n
               and "smart-node" in n for n in notes)


def test_collect_doci_treats_success_false_as_empty_not_failure(monkeypatch):
    # Doci returns success:false when a folder does not exist. That is
    # not an error for the collector: it just means "no documents here".
    # No failure note should surface in that case.
    project = {"id": 24, "slug": "flint", "title": "Flint"}

    def fake(path, remote_user="admin"):
        return {"success": False, "error": "Folder not found"}

    monkeypatch.setattr(collector, "DOCI_URL", "http://doci.example")
    monkeypatch.setattr(collector, "_doci_get", fake)
    out, notes = collector._collect_doci(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    assert out == []
    # No note about failures: an empty folder is a valid answer.
    assert not any("documents:" in n and "failed" in n for n in notes)


# --------------------------------------------------------------------------- owner channels (step 8)

def _ch_row(**kw):
    """Default channel-message row shape as yielded by _messages_rows_in_window."""
    base = {
        "id": 1,
        "thread_id": "t-1",
        "channel_type": "web-chat",
        "direction": "inbound",
        "sender": "web:admin",
        "recipient": "web:agent-a",
        "content": "",
        "received_at": datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
        "status": None,
        "subject": None,
    }
    base.update(kw)
    return base


def test_collect_owner_channels_matches_slug_and_task_ref(monkeypatch):
    # Three messages. Two match (slug term, task-ref), one is dropped
    # (matches neither). Slug term wins on body; task-ref hits a linked
    # id that step 3 already found.
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    linked_ids = {55555, 12345}

    monkeypatch.setattr(
        collector, "_owner_identifiers",
        lambda: {"web-chat": "web:admin"})

    def fake_rows(since_dt, until_dt, limit, owner):
        return [
            _ch_row(id=1, content="How is Flint doing today?"),
            _ch_row(id=2, content="Please look at #12345"),
            _ch_row(id=3, content="Nice weather, the coffee went cold."),
        ]
    monkeypatch.setattr(collector, "_messages_rows_in_window", fake_rows)

    out, notes = collector._collect_owner_channels(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
        linked_ids=linked_ids,
    )
    ids = [r["id"] for r in out]
    assert ids == [1, 2], ids
    r1, r2 = out
    assert r1["matched_terms"] == ["flint"]
    assert r1["matched_task_ids"] == []
    assert r2["matched_terms"] == []
    assert r2["matched_task_ids"] == [12345]
    assert notes == []


def test_collect_owner_channels_ignores_task_ref_not_in_linked_set(monkeypatch):
    # A stray number that looks like a task id but is NOT one of this
    # project's linked tasks must not create a false match.
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    linked_ids = {5555}

    monkeypatch.setattr(
        collector, "_owner_identifiers",
        lambda: {"web-chat": "web:admin"})
    monkeypatch.setattr(
        collector, "_messages_rows_in_window",
        lambda since_dt, until_dt, limit, owner: [
            _ch_row(id=1, content="Link to #99999"),
        ])

    out, notes = collector._collect_owner_channels(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
        linked_ids=linked_ids,
    )
    assert out == []
    assert notes == []


def test_collect_owner_channels_skipped_when_no_owner_addresses(monkeypatch):
    # sn_config.user_channels has no enabled super-admin addresses: the
    # step reports a skip, does not try to read sn_core.messages, and
    # contributes nothing.
    project = {"id": 24, "slug": "flint", "title": "Flint"}

    monkeypatch.setattr(collector, "_owner_identifiers", lambda: {})

    def should_not_be_called(*a, **kw):
        raise AssertionError("messages read must not happen without owner")
    monkeypatch.setattr(
        collector, "_messages_rows_in_window", should_not_be_called)

    out, notes = collector._collect_owner_channels(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
        linked_ids=set(),
    )
    assert out == []
    assert any("sn_config.user_channels" in n or "owner channels" in n
               for n in notes)


def test_collect_owner_channels_notes_read_failure(monkeypatch):
    # A DB read failure (helper returns None) surfaces loudly in notes
    # instead of being swallowed.
    project = {"id": 24, "slug": "flint", "title": "Flint"}

    monkeypatch.setattr(
        collector, "_owner_identifiers",
        lambda: {"web-chat": "web:admin"})
    monkeypatch.setattr(
        collector, "_messages_rows_in_window",
        lambda since_dt, until_dt, limit, owner: None)

    out, notes = collector._collect_owner_channels(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
        linked_ids=set(),
    )
    assert out == []
    assert any("owner channels" in n and ("failed" in n or "incomplete" in n)
               for n in notes)


def test_collect_owner_channels_notes_cap(monkeypatch):
    # When the helper returns more than the cap, the collector trims to
    # the cap and reports it in notes. The matched slug keeps every row
    # in-scope for matching, so the cap is what actually trims.
    project = {"id": 24, "slug": "flint", "title": "Flint"}

    cap = collector._CHANNELS_WINDOW_CAP
    monkeypatch.setattr(
        collector, "_owner_identifiers",
        lambda: {"web-chat": "web:admin"})
    monkeypatch.setattr(
        collector, "_messages_rows_in_window",
        lambda since_dt, until_dt, limit, owner: [
            _ch_row(id=i, content="flint flint flint") for i in range(cap + 5)
        ])

    out, notes = collector._collect_owner_channels(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
        linked_ids=set(),
    )
    assert len(out) == cap
    assert any("owner channels" in n and str(cap) in n for n in notes)


def test_collect_owner_channels_truncates_long_content(monkeypatch):
    # Content longer than 400 chars is cut to a snippet with trailing
    # ellipsis so a card never carries a wall of text.
    project = {"id": 24, "slug": "flint", "title": "Flint"}

    monkeypatch.setattr(
        collector, "_owner_identifiers",
        lambda: {"web-chat": "web:admin"})
    long_text = "flint " + "x" * 600
    monkeypatch.setattr(
        collector, "_messages_rows_in_window",
        lambda since_dt, until_dt, limit, owner: [
            _ch_row(id=1, content=long_text),
        ])

    out, _ = collector._collect_owner_channels(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
        linked_ids=set(),
    )
    assert len(out) == 1
    snippet = out[0]["content_snippet"]
    assert len(snippet) <= 403  # 400 + "..."
    assert snippet.endswith("...")


# --------------------------------------------------------------------------- noise filters

def test_is_test_task_matches_flow_test_and_test_prefix():
    assert collector._is_test_task("flow test content-simple 2026-10-17")
    assert collector._is_test_task("Flow Test task-development")
    assert collector._is_test_task("test fixture 2026-10")
    assert collector._is_test_task("Test smoke 42")
    assert collector._is_test_task("   test  spaced prefix")
    # Real tasks must pass through.
    assert not collector._is_test_task("Latest release pass")
    assert not collector._is_test_task("testing framework upgrade")  # 'testing' is not 'test'
    assert not collector._is_test_task("")
    assert not collector._is_test_task(None)
    assert not collector._is_test_task(123)


def test_collect_moved_filters_test_tasks_and_notes_count():
    """Flow-test tasks are not project movement; the collector drops them
    and loudly says how many it dropped so the signal is not hidden."""
    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    tasks = [
        {"id": 1, "title": "flow test content-simple", "status": "done",
         "project_id": 24, "objective_id": None, "assignee": "agent-a",
         "created_at": "2026-10-11T00:00:00Z",
         "updated_at": "2026-10-12T10:00:00Z"},
        {"id": 2, "title": "Flow Test task-development", "status": "done",
         "project_id": 24, "objective_id": None, "assignee": "agent-a",
         "created_at": "2026-10-11T00:00:00Z",
         "updated_at": "2026-10-12T11:00:00Z"},
        {"id": 3, "title": "test fixture 2026-10", "status": "done",
         "project_id": 24, "objective_id": None, "assignee": "agent-a",
         "created_at": "2026-10-11T00:00:00Z",
         "updated_at": "2026-10-12T12:00:00Z"},
        {"id": 4, "title": "Latest release pass", "status": "in_progress",
         "project_id": 24, "objective_id": None, "assignee": "agent-a",
         "created_at": "2026-10-11T00:00:00Z",
         "updated_at": "2026-10-13T04:00:00Z"},
    ]
    out, notes = collector._collect_moved(tasks, since)
    # Only the real task survives.
    assert [t["id"] for t in out] == [4]
    # The step note says "3 test task(s)".
    assert any("filtered 3 test task" in n for n in notes)


def test_collect_moved_no_filter_note_when_none_filtered():
    """A clean project emits no noise-filter note at all."""
    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    tasks = [
        {"id": 10, "title": "Release 1.12.52 scope", "status": "in_progress",
         "project_id": 24, "objective_id": None, "assignee": "agent-a",
         "created_at": "2026-10-11T00:00:00Z",
         "updated_at": "2026-10-13T04:00:00Z"},
    ]
    out, notes = collector._collect_moved(tasks, since)
    assert [t["id"] for t in out] == [10]
    assert notes == []


def test_is_session_snapshot_requires_explicit_type_tag():
    # Positive: the exact type-tag.
    assert collector._is_session_snapshot(
        {"guid": "g1", "tags": ["type:session-snapshot", "project:smart-node"]})
    # Negative: a different type-tag.
    assert not collector._is_session_snapshot(
        {"guid": "g2", "tags": ["type:note", "project:smart-node"]})
    # Negative: no type-tag at all is kept, the collector cannot infer intent.
    assert not collector._is_session_snapshot(
        {"guid": "g3", "tags": ["project:smart-node"]})
    # Negative: a snapshot-looking content without the tag is NOT dropped.
    assert not collector._is_session_snapshot(
        {"guid": "g4", "tags": [], "content": "session snapshot agent-a 2026-10-17"})
    # Degenerate inputs.
    assert not collector._is_session_snapshot(None)
    assert not collector._is_session_snapshot("not a dict")
    assert not collector._is_session_snapshot({})


def test_collect_memory_drops_session_snapshots_and_notes_count(monkeypatch):
    """Operator-session snapshots in mesh are kept off the card."""
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    kept = {
        "guid": "doc_kept", "workspace": "agent-a",
        "source": "mcp",
        "tags": ["type:note", "project:flint"],
        "content": "# Flint release note",
        "created_at": "2026-10-11T00:00:00Z",
        "updated_at": "2026-10-12T15:00:00Z",
    }
    snapshot = {
        "guid": "doc_snapshot", "workspace": "agent-a",
        "source": "mcp",
        "tags": ["type:session-snapshot", "project:flint"],
        "content": "session carry-over notes",
        "created_at": "2026-10-11T00:00:00Z",
        "updated_at": "2026-10-12T16:00:00Z",
    }

    def fake(path, workspace="*"):
        return [kept, snapshot] if "project=flint" in path else []

    monkeypatch.setattr(collector, "MESH_URL", "http://mesh.example")
    monkeypatch.setattr(collector, "_mesh_get", fake)
    out, notes = collector._collect_memory(
        project,
        since=datetime(2026, 10, 10, tzinfo=timezone.utc),
        until=datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc),
    )
    guids = {r["guid"] for r in out}
    assert guids == {"doc_kept"}
    assert any("session-snapshot" in n and "filtered 1" in n for n in notes)


# --------------------------------------------------------------------------- waiting_for_owner / where_it_stopped

def test_collect_waiting_for_owner_includes_any_age_and_sorts_oldest_first():
    """A task sitting for weeks in awaiting_approval must appear, and
    the oldest one must lead."""
    tasks = [
        {"id": 1, "title": "young", "status": "awaiting_approval",
         "assignee": "agent-a", "objective_id": 10,
         "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 2, "title": "stale", "status": "awaiting_approval",
         "assignee": "agent-a", "objective_id": 10,
         "updated_at": "2026-07-01T00:00:00Z"},
        {"id": 3, "title": "running", "status": "in_progress",
         "assignee": "agent-a", "objective_id": 10,
         "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 4, "title": "escalated one", "status": "escalated",
         "assignee": "agent-a", "objective_id": 10,
         "updated_at": "2026-08-01T00:00:00Z"},
    ]
    out = collector._collect_waiting_for_owner(tasks)
    # Only tasks with waiting statuses, in oldest-first order.
    assert [r["id"] for r in out] == [2, 4, 1]
    # Each row carries age_days (an integer).
    for r in out:
        assert isinstance(r["age_days"], int) and r["age_days"] >= 0


def test_collect_waiting_for_owner_reads_the_three_statuses():
    """The planner's three statuses (0010): queued for approval or for the
    owner waits on the owner; queued for review or for a person does not."""
    tasks = [
        {"id": 1, "title": "approve me", "status": "queued", "state": "queued",
         "wait_reason": "approval", "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 2, "title": "owner call", "status": "queued", "state": "queued",
         "wait_reason": "owner", "updated_at": "2026-10-01T00:00:00Z"},
        {"id": 3, "title": "in review", "status": "queued", "state": "queued",
         "wait_reason": "review", "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 4, "title": "needs Robert", "status": "queued", "state": "queued",
         "wait_reason": "person", "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 5, "title": "untaken", "status": "queued", "state": "queued",
         "wait_reason": None, "updated_at": "2026-10-14T00:00:00Z"},
    ]
    out = collector._collect_waiting_for_owner(tasks)
    assert [r["id"] for r in out] == [2, 1]
    assert [r["wait_reason"] for r in out] == ["owner", "approval"]


def test_objective_closed_count_reads_the_three_statuses():
    tasks = [
        {"id": 1, "objective_id": 10, "status": "closed", "state": "closed",
         "close_outcome": "cancelled", "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 2, "objective_id": 10, "status": "closed", "state": "closed",
         "close_outcome": "completed", "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 3, "objective_id": 10, "status": "queued", "state": "queued",
         "wait_reason": "person", "updated_at": "2026-10-14T00:00:00Z"},
    ]
    goals = collector._goals([{"id": 10, "title": "g", "status": "active"}], tasks)
    assert goals[0]["task_count"] == 3 and goals[0]["closed_count"] == 2


def test_collect_waiting_for_owner_drops_test_tasks():
    tasks = [
        {"id": 1, "title": "flow test content-simple",
         "status": "awaiting_approval", "assignee": "agent-a",
         "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 2, "title": "Real approval needed",
         "status": "awaiting_approval", "assignee": "agent-a",
         "updated_at": "2026-10-14T00:00:00Z"},
    ]
    out = collector._collect_waiting_for_owner(tasks)
    assert [r["id"] for r in out] == [2]


def test_collect_waiting_for_owner_empty_when_no_waiting_tasks():
    tasks = [
        {"id": 1, "title": "work", "status": "in_progress",
         "updated_at": "2026-10-14T00:00:00Z"},
        {"id": 2, "title": "finished", "status": "done",
         "updated_at": "2026-10-14T00:00:00Z"},
    ]
    assert collector._collect_waiting_for_owner(tasks) == []


def test_collect_where_it_stopped_picks_most_recent_task_move():
    tasks = [
        {"id": 1, "title": "old", "status": "done", "assignee": "agent-a",
         "objective_id": 10, "updated_at": "2026-07-01T00:00:00Z"},
        {"id": 2, "title": "latest", "status": "in_progress",
         "assignee": "agent-a", "objective_id": 10,
         "updated_at": "2026-10-13T04:00:00Z"},
        {"id": 3, "title": "mid", "status": "done", "assignee": "agent-a",
         "objective_id": 10, "updated_at": "2026-08-20T00:00:00Z"},
    ]
    out = collector._collect_where_it_stopped(tasks)
    assert out is not None
    assert out["id"] == 2
    assert out["updated_at"] == "2026-10-13T04:00:00Z"
    assert isinstance(out["age_days"], int) and out["age_days"] >= 0


def test_collect_where_it_stopped_ignores_test_tasks():
    """A noisy flow-test should NOT be reported as the project's last move."""
    tasks = [
        {"id": 1, "title": "flow test content-simple",
         "status": "done", "updated_at": "2026-10-16T00:00:00Z"},
        {"id": 2, "title": "Actual work", "status": "in_progress",
         "updated_at": "2026-10-10T00:00:00Z"},
    ]
    out = collector._collect_where_it_stopped(tasks)
    assert out is not None and out["id"] == 2


def test_collect_where_it_stopped_none_when_no_parseable_tasks():
    assert collector._collect_where_it_stopped([]) is None
    assert collector._collect_where_it_stopped(
        [{"id": 1, "title": "no ts", "status": "open", "updated_at": None}]
    ) is None


def test_collect_sets_where_it_stopped_only_when_window_silent(monkeypatch):
    """When nothing moved in-window, where_it_stopped points at the last
    task move; when the window has movement, the field stays None."""
    project = {"id": 24, "slug": "flint", "title": "Flint"}
    objectives = []
    old = {
        "id": 100, "title": "Older task", "status": "done",
        "project_id": 24, "objective_id": None, "assignee": "agent-a",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-15T00:00:00Z",
    }

    def fake(subpath):
        key = subpath.lstrip("/")
        if key.startswith("api/v1/projects"):
            return [project]
        if key.startswith("api/v1/objectives"):
            return objectives
        if key.startswith("api/v1/tasks/search"):
            return []
        if "project_id=24" in key:
            return [old]
        return None

    monkeypatch.setattr(collector, "_planner_get", fake)
    monkeypatch.setattr(collector, "MESH_URL", "")
    monkeypatch.setattr(collector, "DOCI_URL", "")
    monkeypatch.setattr(
        collector, "_owner_identifiers", lambda: {})
    monkeypatch.setattr(
        collector, "_messages_rows_in_window",
        lambda since_dt, until_dt, limit, owner: [])

    since = datetime(2026, 10, 10, tzinfo=timezone.utc)
    until = datetime(2026, 10, 13, 23, 59, 59, tzinfo=timezone.utc)
    out = collector.collect("flint", since, until)
    # Window is silent: moved is empty, where_it_stopped points at `old`.
    assert out["moved"] == []
    assert out["where_it_stopped"] is not None
    assert out["where_it_stopped"]["id"] == 100
    assert out["where_it_stopped"]["updated_at"] == "2026-04-15T00:00:00Z"


# --------------------------------------------------------------------------- endpoint shape

@pytest.fixture
def as_owner():
    """Call as the node's owner (same pattern as test_work_now.py)."""
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


def test_endpoint_returns_documented_shape(as_owner, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    _stub_planner(monkeypatch, {
        "api/v1/projects": [{"id": 24, "slug": "flint", "title": "Flint"}],
        "api/v1/objectives": [],
        "api/v1/tasks": [],
    })
    client = TestClient(app)
    r = client.get("/api/project-state/collect",
                   params={"slug": "flint", "since": "-7d", "fresh": "1"})
    assert r.status_code == 200, r.text
    data = r.json()
    for key in ("as_of", "window_from", "window_to", "project", "goals",
                "linked_counts", "moved", "comments", "mentions",
                "memory", "documents", "channels", "waiting_for_owner",
                "where_it_stopped", "sources_completed",
                "notes", "schema_version"):
        assert key in data, f"missing {key} in response: {list(data.keys())}"
    assert data["project"]["slug"] == "flint"
    assert data["schema_version"] == collector.COLLECTOR_SCHEMA_VERSION


def test_endpoint_404_on_unknown_slug(as_owner, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    _stub_planner(monkeypatch, {
        "api/v1/projects": [{"id": 1, "slug": "other", "title": "Other"}],
    })
    client = TestClient(app)
    r = client.get("/api/project-state/collect",
                   params={"slug": "flint", "since": "-7d", "fresh": "1"})
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["detail"]["error"] == "project_not_found"
    assert body["detail"]["slug"] == "flint"


def test_endpoint_400_on_bad_window(as_owner, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    _stub_planner(monkeypatch, {})
    client = TestClient(app)
    r = client.get("/api/project-state/collect",
                   params={"slug": "flint", "since": "last week", "fresh": "1"})
    assert r.status_code == 400, r.text


def test_endpoint_400_when_since_not_before_until(as_owner, monkeypatch):
    from fastapi.testclient import TestClient
    from main import app

    _stub_planner(monkeypatch, {
        "api/v1/projects": [{"id": 24, "slug": "flint", "title": "Flint"}],
    })
    client = TestClient(app)
    r = client.get("/api/project-state/collect",
                   params={"slug": "flint",
                           "since": "2026-10-13",
                           "until": "2026-10-10",
                           "fresh": "1"})
    assert r.status_code == 400, r.text
