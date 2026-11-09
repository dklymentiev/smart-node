#!/usr/bin/env python3
"""done block of task-simple.

Posts a final "flow complete" comment to the planner task and writes
VERDICT: DONE. This block has a minimal prompt to avoid the Bug A tick
collision (an empty done block raises "No command or agents+prompt for block done"
in this node's orchestrator build).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _planner import post_comment, read_context, write_result


def main():
    ctx         = read_context()
    output_file = ctx.get("output_file")
    task_input  = ctx.get("task_input", {})
    task_id     = task_input.get("task_id")

    if task_id:
        try:
            post_comment(
                int(task_id),
                "[done block]\n\ntask-simple flow completed. "
                "Work executed, verified, and delivered.",
            )
        except RuntimeError as exc:
            print(f"WARNING: could not post done comment: {exc}", file=sys.stderr)

    if output_file:
        write_result(output_file, "VERDICT: DONE\nFlow complete.")

    print(f"done | task #{task_id}")


if __name__ == "__main__":
    main()
