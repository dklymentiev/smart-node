#!/usr/bin/env python3
"""stopped: the end of a run that does not deliver.

Reached when execute or deliver failed, or verify failed or answered something
the flow does not know (see logic/branch.py). Posts one comment that says which
block stopped the run and with what verdict, and reports VERDICT: STOPPED.
Writes nothing else: the task's status is the engine's.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _planner import post_comment, write_result  # noqa: E402

try:
    from _planner import resolve_task_id  # noqa: E402
except ImportError:  # older _planner: the input is the numeric planner id
    resolve_task_id = int
from branch import verdict  # noqa: E402


def main():
    ctx = json.load(sys.stdin)
    out = ctx.get("output_file")
    task_dir = ctx.get("task_dir") or ""
    seen = {b: verdict(task_dir, b) for b in ("execute", "verify", "deliver")}
    why = ", ".join(f"{b} {v.upper()}" for b, v in seen.items() if v and v != "skipped")
    text = f"[stopped]\n\nThe flow stopped without delivery ({why or 'no verdict'})."
    task_id = (ctx.get("task_input") or {}).get("task_id")
    if task_id:
        try:
            post_comment(resolve_task_id(task_id), text)
        except RuntimeError as exc:
            print(f"WARNING: could not post the stop comment: {exc}", file=sys.stderr)
    if out:
        write_result(out, f"VERDICT: STOPPED\n{text}")
    print(f"stopped | task #{task_id} | {why}")


if __name__ == "__main__":
    main()
