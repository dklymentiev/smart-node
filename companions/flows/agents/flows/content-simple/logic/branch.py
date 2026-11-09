#!/usr/bin/env python3
"""
branch: run a block only when the decision before it sent the flow this way.

The engine marks the branches a routing decision did not choose as `skipped`,
so they never run. Rein before 3.3.3 forgot that mark between `--step` calls,
and the runner calls the engine once per step, so a branch nobody chose could
still start later through `depends_on`.

Every block listed in RULES below therefore starts here. It reads the last
verdict the deciding blocks wrote in this run (their result files) and, when the
flow was sent here, runs the block's own script from RUN; otherwise it reports
`VERDICT: SKIPPED` and writes nothing, and the block's routing stops there. On
an engine that keeps `skipped`, a branch nobody chose never starts and these
checks simply never say no; they stay as the flow's own statement of who acts
when, which the tests walk.

The table is the flow's routing read backwards; README lists it as "who acts when".

  gate      acts after draft DONE (asks the owner)
  publish   acts after gate approved, and after nothing else
  rejected  acts after gate reject
  stopped   acts after draft FAIL, publish FAIL or a gate answer nobody knows
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
# What a block that failed or has not finished reports; any other verdict routes on
# through _default, so the next block must act on it.
FAILED = UNDECIDED | {"fail"}
# What logic/gate.py writes; anything else stops the flow rather than publishing.
GATE = {"approved", "revise", "reject", "blocked"}

RULES = {
    "gate": lambda v: v("draft") not in FAILED,
    "publish": lambda v: v("gate") == "approved",
    "rejected": lambda v: v("gate") == "reject",
    "stopped": lambda v: (v("draft") == "fail" or v("publish") == "fail"
                          or v("gate") not in UNDECIDED | GATE),
}

# block -> the script that does the block's work, relative to logic/.
RUN = {"gate": "gate.py", "publish": "delegate.py", "rejected": "end.py", "stopped": "end.py"}


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
DECIDERS = {"gate": ("draft",), "publish": ("gate",), "rejected": ("gate",), "stopped": ("draft", "gate", "publish")}

if __name__ == "__main__":
    main()
