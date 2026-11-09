"""Unit tests for the DB -> /srv/tools registry renderer (no database)."""

from lib.tools_index import render_tools_index


def test_empty():
    out = render_tools_index([])
    assert "GENERATED from sn_core.tools" in out
    assert "No tools registered yet" in out


def test_grouped_by_integration():
    tools = [
        {"name": "bash", "kind": "builtin", "title": "Bash", "description": "Run a command",
         "status": "available", "integration_slug": "system"},
        {"name": "fetch", "kind": "builtin", "title": "HTTP fetch", "description": "",
         "status": "available", "integration_slug": "system"},
        {"name": "ghx", "kind": "mcp", "title": "GitHub", "description": "",
         "status": "broken", "integration_slug": "github"},
    ]
    out = render_tools_index(tools)
    assert "## github" in out and "## system" in out
    assert "`bash` (builtin, available): Bash -- Run a command" in out
    assert "`ghx` (mcp, broken): GitHub" in out
    # system group sorts its tools by name: bash before fetch
    assert out.index("`bash`") < out.index("`fetch`")


def test_unmapped_bucket():
    out = render_tools_index([{"name": "x", "kind": "cli", "title": "X",
                               "status": "available", "integration_slug": None}])
    assert "## (unmapped)" in out
