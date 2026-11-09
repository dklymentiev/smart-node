#!/usr/bin/env python3
"""end: the two ends of task-development that do not deliver.

  rejected  the owner rejected the plan: nothing was executed
  stopped   plan or execution failed, or verification is blocked from outside

Reached only when logic/branch.py says the flow was sent here. Posts one comment
and reports VERDICT: REJECTED or VERDICT: STOPPED. The hand-off back to the
owner is planner.on_block; this script never writes the task.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delegate  # noqa: E402
from branch import verdict  # noqa: E402


def main():
    ctx = json.load(sys.stdin)
    out = ctx.get("output_file", "/tmp/end-result.json")
    block = ((ctx.get("block_config") or {}).get("name")) or "?"
    task_dir = ctx.get("task_dir") or ""
    if block == "rejected":
        text = "Rejected: the owner rejected the plan. Nothing was executed."
    else:
        seen = {b: verdict(task_dir, b) for b in ("plan", "execute", "verify")}
        why = ", ".join(f"{b} {v.upper()}" for b, v in seen.items() if v and v != "skipped")
        text = f"Stopped: {why}. The flow ends here without delivery."
    tid = delegate.task_id_of(ctx.get("task_input") or {})
    delegate.must("POST", delegate.P + tid + "/comments", {"content": text})
    delegate.write_result(out, block.upper(), text)


if __name__ == "__main__":
    main()
