#!/usr/bin/env python3
"""execute block of task-simple.

Reads the planner task and its comments, calls the AI gateway with the
developer specialist persona, posts the AI's work product as a planner comment,
and returns VERDICT: DONE to route to the verify block.
"""

import os
import sys

# Add the logic directory to the path so _planner is importable.
sys.path.insert(0, os.path.dirname(__file__))
from _planner import (
    get_task, get_comments, post_comment, set_status, gateway_call,
    extract_verdict, read_context, write_result,
)

DEVELOPER_PERSONA = """
You are a developer taking ownership of a task.

You have been given the task title, description, and all existing comments. Read them carefully.
Produce the actual work the task asks for: analysis, code, a plan, a document, or a decision.
Be concrete and specific. Do not hedge or defer; if something is missing, state the assumption you made.

Write your output as a comment that will be posted to the task. It should be complete enough
that a tester can independently evaluate it without asking you follow-up questions.

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

    comment_block = ""
    if comments:
        parts = []
        for c in comments:
            author  = c.get("author", "?")
            content = c.get("content", "")
            created = (c.get("created_at") or "")[:19]
            parts.append(f"[{created}] {author}: {content}")
        comment_block = "\n\n".join(parts)
    else:
        comment_block = "(no comments yet)"

    return f"""{DEVELOPER_PERSONA.strip()}

---

TASK #{task.get('id', '?')} ({status}, {priority} priority)
Title: {title}

Description:
{description}

Existing comments:
{comment_block}

---

Produce your work product now. End with VERDICT: DONE.
"""


def main():
    ctx         = read_context()
    output_file = ctx.get("output_file")
    task_input  = ctx.get("task_input", {})
    task_id     = task_input.get("task_id")

    if not task_id:
        msg = "ERROR: task_id not provided in task input. Pass task_id=<planner task id> when starting the flow."
        print(msg, file=sys.stderr)
        if output_file:
            write_result(output_file, f"VERDICT: FAIL\n{msg}")
        sys.exit(1)

    task_id = int(task_id)

    try:
        task     = get_task(task_id)
        comments = get_comments(task_id)
    except RuntimeError as exc:
        msg = f"ERROR: cannot read task from planner: {exc}"
        print(msg, file=sys.stderr)
        if output_file:
            write_result(output_file, f"VERDICT: FAIL\n{msg}")
        sys.exit(1)

    # The flow takes its own task. The Runner used to move it to in_progress
    # when it started the run; a launcher has no business with a task's status
    #. A task that is already past open is left as it is, so a round
    # sent back here by a REVISE writes nothing.
    if (task.get("stage") or task.get("status")) == "open":
        try:
            set_status(task_id, "in_progress", "task-simple started: the developer block has the task.")
        except RuntimeError as exc:
            print(f"WARNING: could not move the task to in_progress: {exc}", file=sys.stderr)

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
        # The AI should always return VERDICT: DONE from this block.
        # If it did not, treat the response as-is and still post it.
        print(f"WARNING: expected VERDICT: DONE from developer, got '{verdict}'", file=sys.stderr)

    # Post the AI's output as a planner comment.
    try:
        post_comment(task_id, f"[execute block]\n\n{response}")
    except RuntimeError as exc:
        print(f"WARNING: could not post comment: {exc}", file=sys.stderr)
        # Do not exit -- the work is done even if the comment failed.

    # Always route to verify after execute.
    if output_file:
        write_result(output_file, f"VERDICT: DONE\n{response}")

    print(f"execute | task #{task_id} | verdict={verdict or 'DONE'}")


if __name__ == "__main__":
    main()
