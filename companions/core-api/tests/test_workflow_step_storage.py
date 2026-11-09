"""The publish door stores the complete step record used by the runner."""

import json
import unittest

from routes import workflows


class Cursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, args):
        self.calls.append((sql, args))

    def fetchone(self):
        return {"id": 7}


class WorkflowStepStorageTest(unittest.TestCase):
    def test_materialized_steps_keep_execution_fields(self):
        blocks = [
            {"name": "prepare", "logic": {"custom": "prepare.py"}, "phase": 1,
             "parallel": True, "max_runs": 2, "next": "publish"},
            {"name": "publish", "specialist": "writer", "phase": 2,
             "depends_on": ["prepare"], "parallel": False, "max_runs": 3},
        ]
        cursor = Cursor()
        workflows._materialize("sample-flow", {
            "schema_version": "3.4.0", "blocks": blocks,
        }, cursor, path="/tmp/sample-flow.yaml")
        sql, args = cursor.calls[0]
        self.assertIn("INSERT INTO sn_core.workflows", sql)
        steps = json.loads(args[7])
        self.assertEqual(
            [{key: step[key] for key in ("kind", "phase", "depends_on", "parallel", "max_runs")}
             for step in steps],
            [
                {"kind": "logic", "phase": 1, "depends_on": [], "parallel": True, "max_runs": 2},
                {"kind": "specialist", "phase": 2, "depends_on": ["prepare"],
                 "parallel": False, "max_runs": 3},
            ],
        )


if __name__ == "__main__":
    unittest.main()
