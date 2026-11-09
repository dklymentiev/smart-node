"""A tool step carries the model's own one-line description of what it does.

The model writes a `description` for each Bash call. The step list used to keep only
the command (the first of _DETAIL_KEYS), so the chat could show nothing plainer than
the tool's name and, in the console, a raw command that may carry a path or the name
of a secret. The description travels as its own field, `desc`, and the command stays
where it was.
"""
import unittest

from request_usage_regression import cli


class StepDescription(unittest.TestCase):
    def test_description_is_read_from_the_tool_input(self):
        got = cli._tool_desc({"command": "pytest -q", "description": "  Run the   unit tests "})
        self.assertEqual(got, "Run the unit tests")

    def test_no_description_gives_nothing_not_the_command(self):
        self.assertEqual(cli._tool_desc({"command": "ls /tmp"}), "")
        self.assertEqual(cli._tool_desc({"description": 5}), "")
        self.assertEqual(cli._tool_desc(None), "")

    def test_a_long_description_is_cut(self):
        got = cli._tool_desc({"description": "word " * 100})
        self.assertLessEqual(len(got), 123)
        self.assertTrue(got.endswith("..."))

    def test_the_step_carries_desc_to_the_desk_activity(self):
        sid = "desc-step"
        cli._TURN_STARTED[sid] = 0.0
        cli._SESSION_CONVERSATION[sid] = "agent-x:desk:desc-test"
        try:
            cli._push_step(sid, "tool", "Bash: GITLAB_TOKEN=$(curl -s x)", desc="Fetch the token")
            cli._push_step(sid, "tool", "Read: /srv/a.py")
            steps = cli.get_desk_activity("desc-test", since=0)["steps"]
            self.assertEqual(steps[0]["desc"], "Fetch the token")
            self.assertEqual(steps[0]["text"], "Bash: GITLAB_TOKEN=$(curl -s x)")
            self.assertNotIn("desc", steps[1])
        finally:
            for d in (cli._TURN_STARTED, cli._SESSION_CONVERSATION, cli._TURN_STEPS, cli._TURN_STEP_SEQ):
                d.pop(sid, None)


if __name__ == "__main__":
    unittest.main()
