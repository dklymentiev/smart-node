#!/usr/bin/env python3
"""verify block of task-simple.

Reads the planner task and all comments (including execute's output), calls the
AI gateway with the tester specialist persona, posts the verdict comment to the
planner, and returns VERDICT: APPROVE (-> deliver) or VERDICT: REVISE (-> execute).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _planner import (
    get_task, get_comments, post_comment, gateway_call,
    extract_verdict, read_context, write_result,
)

TESTER_PERSONA = """
You are an independent tester evaluating work you did not produce.

You have the task title, description, all comments, and the developer's work product
(in the most recent comment labelled [execute block]).

Check the developer's work against the task description: is it complete, is it correct,
does it address the actual request? Name specific failures, not general worries.

You have a 3-minute budget. Be efficient. Focus on the most important issues.

Approving something that fails the task is worse than asking for one revision.
Approving something imperfect when it genuinely meets the request is also wrong: approve it.

Do not use long dashes or double hyphens. Use commas, colons, or full stops instead.
Do not use emoji. Do not summarise yourself at the end.

If the work meets the task requirements, end with exactly one line:
VERDICT: APPROVE

If the work needs changes, end with:
VERDICT: REVISE
followed by a numbered list of specific things that must change.
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

    return f"""{TESTER_PERSONA.strip()}

---

TASK #{task.get('id', '?')} ({status}, {priority} priority)
Title: {title}

Description:
{description}

All comments (most recent is the developer's work product):
{comment_block}

---

Evaluate the developer's work now. End with VERDICT: APPROVE or VERDICT: REVISE.
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
    if verdict not in ("APPROVE", "REVISE"):
        print(
            f"WARNING: tester returned unexpected verdict '{verdict}'; defaulting to REVISE",
            file=sys.stderr,
        )
        verdict = "REVISE"
        response = response + "\nVERDICT: REVISE\n(verdict defaulted because the AI did not emit APPROVE or REVISE)"

    try:
        post_comment(task_id, f"[verify block]\n\n{response}")
    except RuntimeError as exc:
        print(f"WARNING: could not post comment: {exc}", file=sys.stderr)

    if output_file:
        write_result(output_file, f"VERDICT: {verdict}\n{response}")

    print(f"verify | task #{task_id} | verdict={verdict}")


if __name__ == "__main__":
    main()
