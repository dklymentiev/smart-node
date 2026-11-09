#!/usr/bin/env python3
"""deliver block of task-simple.

Reads the planner task and all comments, calls the AI gateway with the pm
specialist persona, posts the delivery comment to the planner, and returns
VERDICT: DONE to route to the done block.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _planner import (
    get_task, get_comments, post_comment, gateway_call,
    extract_verdict, read_context, write_result,
)

PM_PERSONA = """
You are a project manager delivering the final result of a completed task.

You have the task title, description, all comments, and the approved work product.
Write the delivery comment: a concise, final-form summary of what was done and what
the outcome is. This comment goes on the planner task as the closing record.

Do not re-explain the process. Write for someone who will read this comment weeks later
to understand what happened and what was decided.

Do not use long dashes or double hyphens. Use commas, colons, or full stops instead.
Do not use emoji. Do not summarise yourself at the end.

End with exactly one line:
VERDICT: DONE
"""


def build_prompt(task: dict, comments: list) -> str:
    title       = task.get("title", "(no title)")
    description = task.get("description", "(no description)")
    status      = task.get("status", "?")
    priority    = task.get("priority", "?")

    parts = []
    for c in comments:
        author  = c.get("author", "?")
        content = c.get("content", "")
        created = (c.get("created_at") or "")[:19]
        parts.append(f"[{created}] {author}: {content}")
    comment_block = "\n\n".join(parts) if parts else "(no comments)"

    return f"""{PM_PERSONA.strip()}

---

TASK #{task.get('id', '?')} ({status}, {priority} priority)
Title: {title}

Description:
{description}

All comments (the approved work product is in the most recent [execute block] comment
that followed a [verify block] APPROVE):
{comment_block}

---

Write the delivery comment now. End with VERDICT: DONE.
"""


def main():
    ctx         = read_context()
    output_file = ctx.get("output_file")
    task_input  = ctx.get("task_input", {})
    task_id     = task_input.get("task_id")

    if not task_id:
        msg = "ERROR: task_id not provided."
        print(msg, file=sys.stderr)
        if output_file:
            write_result(output_file, f"VERDICT: FAIL\n{msg}")
        sys.exit(1)

    task_id = int(task_id)

    try:
        task     = get_task(task_id)
        comments = get_comments(task_id)
    except RuntimeError as exc:
        msg = f"ERROR: cannot read task: {exc}"
        print(msg, file=sys.stderr)
        if output_file:
            write_result(output_file, f"VERDICT: FAIL\n{msg}")
        sys.exit(1)

    prompt = build_prompt(task, comments)

    try:
        response = gateway_call(prompt)
    except Exception as exc:
        msg = f"ERROR: gateway call failed: {exc}"
        print(msg, file=sys.stderr)
        if output_file:
            write_result(output_file, f"VERDICT: FAIL\n{msg}")
        sys.exit(1)

    verdict = extract_verdict(response)
    if verdict != "DONE":
        print(f"WARNING: expected VERDICT: DONE from pm, got '{verdict}'", file=sys.stderr)

    try:
        post_comment(task_id, f"[deliver block]\n\n{response}")
    except RuntimeError as exc:
        print(f"WARNING: could not post comment: {exc}", file=sys.stderr)

    if output_file:
        write_result(output_file, f"VERDICT: DONE\n{response}")

    print(f"deliver | task #{task_id} | verdict=DONE")


if __name__ == "__main__":
    main()
