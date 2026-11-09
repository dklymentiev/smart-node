"""What every agent is given without asking: the registry rows marked as such.

A tool is granted by default when sn_core.tools.default_caps is set (the caps it
is granted with); an MCP server is in every agent's .mcp.json when
sn_config.mcp_servers.default_on is true. The installer marks its own rows; no
tool name is written here. One function grants the marked tools to one agent
when it is created and to every agent when core-api starts, so a new agent no
longer waits for the next install: one created between two installs had its
messaging and reminders servers but none of their contracts (2026-10-29).
"""


def apply(cur, agent_id=None) -> None:
    """Grant the tools marked default to one agent, or to every active agent
    when agent_id is None. A grant the agent already holds is left as it is."""
    cur.execute(
        "INSERT INTO sn_config.agent_tools (agent_id, tool_id, granted_caps) "
        "SELECT a.id, t.id, t.default_caps "
        "  FROM sn_config.agents a, sn_core.tools t "
        " WHERE t.default_caps IS NOT NULL "
        "   AND a.enabled AND a.archived_at IS NULL "
        "   AND (%s::int IS NULL OR a.id = %s::int) "
        "ON CONFLICT (agent_id, tool_id) DO NOTHING",
        (agent_id, agent_id),
    )
