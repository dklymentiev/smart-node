"""What the Runner does with a workflow task, per planner status.

The Runner reads the planner and writes no status: it starts a run, steps the
run in flight, or leaves the task alone. The owner's approval is a start: the
planner stamps approved_at and moves the task to in_progress, never back to
queued.
"""
import os
import unittest

os.environ.setdefault("SN_CORE_API_URL", "http://127.0.0.1:1")
os.environ.setdefault("PLANNER_API_URL", "http://127.0.0.1:1")
os.environ.setdefault("AI_GATEWAY_URL", "http://127.0.0.1:1")

import runner  # noqa: E402


def what(status, wait=None, tags=(), approved_at=None, outcome=None):
    task = {"status": status, "state": status, "wait_reason": wait,
            "close_outcome": outcome, "approved_at": approved_at}
    return runner._flow_task_start(task, list(tags))


class FlowTaskRuleTest(unittest.TestCase):
    def test_new_work_nobody_took_is_a_start(self):
        self.assertEqual(what("queued"), "start")

    def test_a_run_in_flight_is_stepped(self):
        for status, wait in (("queued", None), ("in_progress", None), ("queued", "review")):
            self.assertEqual(what(status, wait, ["reindir:task-1"]), "step", (status, wait))

    def test_the_owner_approval_is_a_start(self):
        self.assertEqual(what("in_progress", None, ["decision:approve"], "2026-10-15T13:56:55Z"), "start")

    def test_a_task_sent_back_by_the_owner_is_not(self):
        self.assertIsNone(what("in_progress", None, ["decision:modify"]))

    def test_in_progress_without_a_run_or_an_approval_is_left_alone(self):
        self.assertIsNone(what("in_progress"))

    def test_the_old_tag_means_nothing(self):
        self.assertIsNone(what("in_progress", None, ["flow:start"]))

    def test_review_starts_nothing(self):
        self.assertIsNone(what("queued", "review", [], "2026-10-15T13:56:55Z"))

    def test_held_and_ended_runs_are_never_driven(self):
        for tag in ("blocked:agent-work", "flow-done:content-story", "flow-review:scribe"):
            self.assertIsNone(what("in_progress", None, [tag, "reindir:task-1"], "2026-10-15T13:56:55Z"), tag)
            self.assertIsNone(what("queued", None, [tag]), tag)

    def test_waiting_and_closed_tasks_are_never_driven(self):
        for wait in ("approval", "owner", "person", "dependency", "schedule", "error"):
            self.assertIsNone(what("queued", wait, ["reindir:task-1"], "2026-10-15T13:56:55Z"), wait)
        self.assertIsNone(what("closed", None, ["reindir:task-1"], "2026-10-15T13:56:55Z", "completed"))

    def test_an_older_planner_with_the_one_word_status(self):
        self.assertEqual(runner._flow_task_start({"status": "open"}, []), "start")
        self.assertEqual(runner._flow_task_start({"status": "pending_review"}, ["reindir:x"]), "step")
        for word in ("awaiting_approval", "blocked", "escalated", "done", "rejected"):
            self.assertIsNone(runner._flow_task_start({"status": word}, ["reindir:x"]), word)

    def test_place_reads_state_before_the_wire_status(self):
        # A legacy-vocabulary answer: status is the stage, state the real one.
        self.assertEqual(runner._place({"status": "awaiting_approval", "state": "queued",
                                        "wait_reason": "approval"}), ("queued", "approval"))
        self.assertEqual(runner._place({"status": "done", "state": "closed",
                                        "close_outcome": "completed"}), ("closed", None))


if __name__ == "__main__":
    unittest.main()
