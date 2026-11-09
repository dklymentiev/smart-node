#!/usr/bin/env python3
"""
flow-fuzz.py -- random walks through a flow, each one run on the real Rein engine.

Hand-written edge tests check the routes someone thought of. This draws them at
random: starting from the first block it picks, at every block, one of the
verdicts the block's routing names (or an undeclared one, which takes _default),
follows the route, and stops at _stop. Loops back are allowed as long as no block
would run past its max_runs. A flow whose logic/branch.py has REACHABLE ((block,
verdict) -> predicate over the verdicts so far) only gets that verdict when the
predicate holds: an answer whose button is offered only in some states. The walk is the expected behaviour: the blocks that
act, in order.

Every walk is then run through the engine with flow-stubrun.py: stub blocks answer
with the walk's verdicts, the flow's own logic/branch.py decides which blocks act
(so a branch nobody chose must stay quiet), and the run must complete. A walk
fails when the blocks that acted differ from the walk, or the run does not end.

    flow-fuzz.py FLOW_DIR [--runs 50] [--seed 1] [--quiet]

Prints one line per failing walk with its seed and its script, so it can be
replayed with flow-stubrun.py; exit 0 when every walk passed. No model, planner
or gateway is touched.
"""
import argparse
import importlib.util
import os
import random
import subprocess
import sys

try:
    import yaml
except ImportError:
    print("PyYAML is required (run with the engine venv)", file=sys.stderr)
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
STUBRUN = os.path.join(HERE, "flow-stubrun.py")
DEFAULT_MAX_RUNS = 3          # Rein's default_max_runs
UNDECLARED = "unlisted"       # a verdict no routing names: takes _default
NOT_CHOSEN = {"skipped"}      # what a block reports when the flow was not sent to it
MAX_STEPS = 60


def load(flow_dir):
    name = os.path.basename(os.path.abspath(flow_dir))
    wf = yaml.safe_load(open(os.path.join(flow_dir, f"{name}.yaml"))) or {}
    blocks = {b["name"]: b for b in wf.get("blocks") or []}
    first = next(b["name"] for b in wf["blocks"] if not b.get("depends_on"))
    return blocks, first


def reachable(flow_dir):
    """The flow's REACHABLE table from logic/branch.py, or {}."""
    path = os.path.join(flow_dir, "logic", "branch.py")
    if not os.path.exists(path):
        return {}
    spec = importlib.util.spec_from_file_location("_fuzz_branch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "REACHABLE", {}) or {}


def walk(blocks, first, rng, reach=None):
    """[(block, verdict), ...] ending at _stop, or None when no legal walk was found."""
    runs = {b: 0 for b in blocks}
    runs[first] = 1
    path, cur = [], first
    for _ in range(MAX_STEPS):
        routing = blocks[cur].get("routing") or {}
        options = [k for k in routing if k != "_default" and k not in NOT_CHOSEN]
        if "_default" in routing:
            options.append(UNDECLARED)
        last = {b: v.lower() for b, v in path}
        options = [o for o in options
                   if (reach or {}).get((cur, o), lambda v: True)(last.get)]
        legal = []
        for verdict in options:
            target = routing.get(verdict, routing.get("_default"))
            if target == "_stop" or target is None:
                legal.append((verdict, "_stop"))
            elif runs[target] + 1 < int(blocks[target].get("max_runs", DEFAULT_MAX_RUNS)):
                legal.append((verdict, target))
        if not legal:
            return None
        # Late in a walk, prefer the ways out so walks end.
        if len(path) > MAX_STEPS // 2:
            out = [x for x in legal if x[1] == "_stop"] or legal
            legal = out
        verdict, target = rng.choice(legal)
        path.append((cur, verdict))
        if target == "_stop":
            return path
        runs[target] += 1
        cur = target
    return None


def run(flow_dir, path):
    script = ",".join(f"{b}={v}" for b, v in path)
    py = os.environ.get("SN_FLOW_PY") or sys.executable
    out = subprocess.run([py, STUBRUN, flow_dir, script], capture_output=True, text=True)
    lines = out.stdout.splitlines()
    acted = lines[0] if lines else ""
    status = lines[1] if len(lines) > 1 else out.stderr.strip()[-200:]
    return script, acted, status


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("flow_dir")
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    blocks, first = load(a.flow_dir)
    reach = reachable(a.flow_dir)
    failed = tried = 0
    for i in range(a.runs):
        seed = a.seed + i
        path = walk(blocks, first, random.Random(seed), reach)
        if path is None:
            continue
        tried += 1
        expected = "".join(f"{b}>" for b, _ in path)
        script, acted, status = run(a.flow_dir, path)
        if acted != expected or status != "status=completed":
            failed += 1
            print(f"FAIL seed={seed}\n  script:   {script}\n  expected: {expected}\n  acted:    {acted}\n  {status}")
        elif not a.quiet:
            print(f"ok   seed={seed}  {expected}")
    print(f"{tried} walk(s) run, {failed} failed")
    return 1 if failed or tried == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
