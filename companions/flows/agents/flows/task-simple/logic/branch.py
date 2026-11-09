#!/usr/bin/env python3
"""
branch: run a block only when the decision before it sent the flow this way.

Rein marks the branches a routing decision did not choose as `skipped`, so they
never run. The Rein vendored in this node forgets that mark between `--step`
calls (fixed upstream, not yet vendored), and the runner calls Rein once per
step, so a branch nobody chose can still start later through `depends_on`.

Every block listed in RULES below therefore starts here. It reads the last
verdict the deciding blocks wrote in this run (their result files) and, when the
flow was sent here, runs the block's own script from RUN; otherwise it reports
`VERDICT: SKIPPED` and writes nothing, and the block's routing stops there. Once
the vendored Rein keeps `skipped`, a branch nobody chose never starts and these
checks simply never say no.

The table is the flow's routing read backwards; README lists it as "who acts when".

  verify   acts after execute reported anything but FAIL
  deliver  acts after verify APPROVE
  done     acts after deliver reported anything but FAIL
  stopped  acts after execute FAIL, deliver FAIL, or verify FAIL or an unknown verdict
"""
import io
import json
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Verdicts that mean "this block has not decided anything (yet)".
UNDECIDED = {None, "waiting", "skipped"}

# block -> predicate over v(block_name) (the last verdict of that block, lowercase).
RULES = {
    "verify": lambda v: v("execute") not in UNDECIDED | {"fail"},
    "deliver": lambda v: v("verify") == "approve",
    "done": lambda v: v("deliver") not in UNDECIDED | {"fail"},
    "stopped": lambda v: (v("execute") == "fail" or v("deliver") == "fail"
                          or v("verify") not in UNDECIDED | {"approve", "revise"}),
}

# block -> the script that does the block's work, relative to logic/.
RUN = {"verify": "verify.py", "deliver": "deliver.py", "done": "done.py", "stopped": "stopped.py"}


def verdict(task_dir, block):
    """The last VERDICT `block` wrote in this run, lowercase, or None."""
    try:
        with open(os.path.join(task_dir, block, "outputs", "result.json")) as f:
            text = json.load(f).get("result") or ""
    except (OSError, ValueError):
        return None
    found = None
    for line in text.splitlines():
        s = line.strip().strip("*_`").strip()
        if s.upper().startswith("VERDICT:"):
            found = s.split(":", 1)[1].strip().strip("*_`").strip().lower() or None
    return found


def write(out, text):
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump({"result": text}, f, indent=2)


def main():
    raw = sys.stdin.read()
    ctx = json.loads(raw)
    out = ctx.get("output_file", "/tmp/branch-result.json")
    block = ((ctx.get("block_config") or {}).get("name")) or "?"
    task_dir = ctx.get("task_dir") or ""
    v = lambda b: verdict(task_dir, b)  # noqa: E731
    rule = RULES.get(block)
    if rule is None:
        write(out, f"branch: no rule for block {block!r}\nVERDICT: FAIL")
        return
    if not rule(v):
        seen = ", ".join(f"{b}={v(b)}" for b in DECIDERS.get(block, ()))
        write(out, f"{block}: not sent here ({seen})\nVERDICT: SKIPPED")
        print(f"branch | {block} | skipped | {seen}")
        return
    sys.stdin = io.StringIO(raw)
    sys.argv = [os.path.join(HERE, RUN[block])]
    runpy.run_path(os.path.join(HERE, RUN[block]), run_name="__main__")


# block -> the blocks its rule reads, for the SKIPPED note.
DECIDERS = {"verify": ("execute",), "deliver": ("verify",), "done": ("deliver",), "stopped": ("execute", "verify", "deliver")}

if __name__ == "__main__":
    main()
