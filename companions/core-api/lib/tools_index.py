"""Render a /srv/tools registry index from sn_core.tools rows.

The DB -> filesystem projection for tools: sn_core.tools (+ integration_slug)
is the source of truth; this renders a human/agent-readable REGISTRY.md grouped
by integration. The static convention lives in /srv/tools/CLAUDE.md and is left
untouched; this writes a sibling REGISTRY.md.

Pure function `render_tools_index` is import-friendly for unit tests.
"""

HEADER = (
    "# Tools registry -- GENERATED from sn_core.tools by core-api. DO NOT EDIT.\n"
    "# Add/edit tools in the panel; the controller re-renders this file.\n"
)


def render_tools_index(tools: list[dict]) -> str:
    """Render tools grouped by integration into a markdown registry."""
    by_integration: dict[str, list[dict]] = {}
    for t in tools:
        key = t.get("integration_slug") or "(unmapped)"
        by_integration.setdefault(key, []).append(t)

    lines = [HEADER.rstrip("\n"), ""]
    if not tools:
        lines.append("_No tools registered yet._")
        return "\n".join(lines) + "\n"

    for integration in sorted(by_integration):
        lines.append(f"## {integration}")
        for t in sorted(by_integration[integration], key=lambda x: x["name"]):
            status = t.get("status", "available")
            title = t.get("title") or t["name"]
            desc = t.get("description") or ""
            suffix = f" -- {desc}" if desc else ""
            lines.append(f"- `{t['name']}` ({t.get('kind', '?')}, {status}): {title}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
