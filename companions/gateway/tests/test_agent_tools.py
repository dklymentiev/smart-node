"""An agent's --tools list: the base set plus the servers it was granted."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backends import cli_claude  # noqa: E402


class AgentToolsTest(unittest.TestCase):
    def test_base_set_plus_granted_servers(self):
        with tempfile.TemporaryDirectory() as cwd:
            Path(cwd, ".mcp.json").write_text(json.dumps(
                {"mcpServers": {"reminders": {}, "messaging": {}}}))
            tools = cli_claude._agent_tools(cwd).split(",")
        self.assertEqual(tools[:len(cli_claude.AGENT_BASE_TOOLS)],
                         list(cli_claude.AGENT_BASE_TOOLS))
        self.assertEqual(tools[len(cli_claude.AGENT_BASE_TOOLS):],
                         ["mcp__messaging", "mcp__reminders"])

    def test_a_session_may_withhold_tools(self):
        """A chat wake withholds the inbox tools: they become --disallowedTools,
        which only takes away. Anything that is not an MCP tool name is ignored."""
        meta = {"withhold_tools": ["mcp__messaging__inbox_read", "mcp__messaging__inbox_dismiss", "Bash", 7]}
        self.assertEqual(cli_claude._withheld_tools(meta),
                         "mcp__messaging__inbox_read,mcp__messaging__inbox_dismiss")
        self.assertEqual(cli_claude._withheld_tools({}), "")
        self.assertEqual(cli_claude._withheld_tools(None), "")


class AnsweredTest(unittest.TestCase):
    """A chat wake ends once each of its messages has a successful inbox_reply."""

    def test_a_successful_reply_counts_and_a_refusal_does_not(self):
        calls = {"t1": 340, "t2": 345}
        ok = {"type": "tool_result", "tool_use_id": "t1", "content": '{"ok": true, "message_id": 9}'}
        refused = {"type": "tool_result", "tool_use_id": "t2",
                   "content": [{"type": "text", "text": '{"error": "HTTP 409", "detail": "own turn"}'}]}
        other = {"type": "tool_result", "tool_use_id": "t9", "content": '{"ok": true}'}
        failed = dict(ok, is_error=True)
        self.assertEqual(cli_claude._answered_id(ok, calls), 340)
        self.assertIsNone(cli_claude._answered_id(refused, calls))
        self.assertIsNone(cli_claude._answered_id(other, calls))
        self.assertIsNone(cli_claude._answered_id(failed, calls))


if __name__ == "__main__":
    unittest.main()
