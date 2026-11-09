"""MCP tools coverage.

We bypass the stdio transport and call ``dispatch`` directly against a live
DB pool -- transport-level concerns (framing, JSON-RPC) belong to the MCP
runtime, which we trust. The interesting surface is that every tool
round-trips against the same repo layer used by REST.
"""

from __future__ import annotations

import pytest

from planner.mcp_tools import dispatch, tool_catalogue


async def test_catalogue_is_non_empty_and_well_formed() -> None:
    cat = tool_catalogue()
    assert len(cat) >= 20
    names = {t["name"] for t in cat}
    # Spot-check a representative subset across resources.
    for required in (
        "actor_create", "project_create", "objective_create", "task_create",
        "task_list", "comment_add", "tag_add", "objective_tree", "objective_progress",
    ):
        assert required in names
    # Every tool must declare a JSONSchema-shaped input.
    for t in cat:
        assert t["inputSchema"]["type"] == "object"


async def test_full_lifecycle_through_mcp(pool) -> None:
    # 1. Create an actor (system already exists from seed).
    actor = await dispatch(pool, "actor_create", {
        "id": "agent-mcp", "display_name": "MCP agent", "type": "agent",
    })
    assert actor["id"] == "agent-mcp"

    # 2. Project + objective tree
    proj = await dispatch(pool, "project_create", {"slug": "mcp-proj", "title": "MCP"})
    strat = await dispatch(pool, "objective_create", {
        "project_id": proj["id"], "title": "S", "type": "strategy",
    })
    rdm = await dispatch(pool, "objective_create", {
        "project_id": proj["id"], "title": "R", "type": "roadmap",
        "parent_id": strat["id"],
    })

    tree = await dispatch(pool, "objective_tree", {"project_id": proj["id"]})
    assert len(tree) == 1 and tree[0]["objective"]["id"] == strat["id"]
    assert tree[0]["children"][0]["objective"]["id"] == rdm["id"]

    # 3. Task + ancillary
    task = await dispatch(pool, "task_create", {
        "title": "first", "project_id": proj["id"],
        "objective_id": rdm["id"], "assignee": "agent-mcp",
    })
    fetched = await dispatch(pool, "task_get", {"id": task["id"]})
    assert fetched["title"] == "first"

    listed = await dispatch(pool, "task_list", {"assignee": "agent-mcp"})
    assert [t["id"] for t in listed] == [task["id"]]

    # Update through the generic field updater.
    updated = await dispatch(pool, "task_update", {
        "id": task["id"],
        "fields": {"status": "in_progress", "scope": "small"},
    })
    assert updated["status"] == "in_progress"

    # Comments + tags + links
    cmt = await dispatch(pool, "comment_add", {
        "task_id": task["id"], "author": "agent-mcp", "content": "via mcp",
    })
    assert cmt["author"] == "agent-mcp"

    add_tag = await dispatch(pool, "tag_add", {"task_id": task["id"], "tag": "via-mcp"})
    assert add_tag["added"] is True
    tags = await dispatch(pool, "tags_list", {"task_id": task["id"]})
    assert "via-mcp" in tags

    link = await dispatch(pool, "link_add", {
        "task_id": task["id"], "kind": "doc",
        "url": "https://example.com/notes",
    })
    assert link["url"].startswith("https://")

    # Progress aggregation
    await dispatch(pool, "task_update", {"id": task["id"], "fields": {"status": "done"}})
    progress = await dispatch(pool, "objective_progress", {"id": strat["id"]})
    assert progress["total_tasks"] == 1 and progress["progress_pct"] == 100


async def test_unknown_tool_raises(pool) -> None:
    with pytest.raises(ValueError, match="unknown MCP tool"):
        await dispatch(pool, "no_such_tool", {})
