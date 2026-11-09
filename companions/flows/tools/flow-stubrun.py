#!/usr/bin/env python3
"""
flow-stubrun.py -- run a flow through the real Rein engine on stubs, with no model.

flow-trace.py walks the routing table; it never executes a block. Two defects
only show up when Rein really runs the flow, step by step, the way the runner
does (`rein --step 1`, a new process each time):

  * a block that runs nothing (no script, prompt or command) fails every time,
    so a flow ending in an empty block never completes;
  * a branch the routing did not choose can still start through `depends_on`.

This copies the flow into a temporary agents tree, replaces what every block
runs with a stub, and drives it with `rein --step 1` until Rein reports the run
completed. The stub of a block answers with the next verdict scripted for it, or
`SKIPPED` once its script is used up: a branch block that was not sent anywhere
says SKIPPED, as the flow's own branch checks do. When the flow has
logic/branch.py, the stub asks its RULES first, so a block the flow was not sent
to stays SKIPPED and the flow's own "who acts when" table is exercised too.

    flow-stubrun.py FLOW_DIR "draft=DONE,approve=approve,deliver=DONE"

Prints the blocks that acted, in order ("draft>approve>deliver>"), on the first
line and the run status on the second. Exit 0 when the run completed, 1 when it
did not, 2 on a usage error. No planner, gateway or model is touched.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PY = os.environ.get("SN_REIN_PY", "/opt/smart-node/workflows/venv/bin/python")
REIN_CWD = os.environ.get("SN_REIN_CWD", "/opt/smart-node/workflows")
MAX_STEPS = 80

STUB = r'''import json, os, sys
ctx = json.load(sys.stdin)
name = (ctx.get("block_config") or {}).get("name") or "?"
plan_f, log_f = os.environ["STUBRUN_PLAN"], os.environ["STUBRUN_LOG"]
# The flow's own branch rules decide whether this block acts at all, exactly as on
# a live run: a block the flow was not sent to reports SKIPPED without using up the
# verdicts scripted for it. Flows without logic/branch.py act whenever they start.
here = os.path.dirname(os.path.abspath(__file__))
acts = True
if os.path.exists(os.path.join(here, "branch.py")):
    sys.path.insert(0, here)
    import branch
    rule = getattr(branch, "RULES", {}).get(name)
    if rule is not None:
        acts = bool(rule(lambda b: branch.verdict(ctx.get("task_dir") or "", b)))
    if not acts and hasattr(branch, "remember"):
        branch.remember(ctx.get("task_dir") or "", name)
plan = json.load(open(plan_f))
queue = plan.get(name) or []
verdict = queue.pop(0) if (acts and queue) else "SKIPPED"
plan[name] = queue
json.dump(plan, open(plan_f, "w"))
with open(log_f, "a") as f:
    f.write(f"{name}={verdict}\n")
with open(ctx["output_file"], "w") as f:
    json.dump({"result": f"stub\nVERDICT: {verdict}"}, f)
'''


def parse_script(text):
    plan, order = {}, []
    for item in filter(None, (x.strip() for x in text.split(","))):
        block, _, verdict = item.partition("=")
        plan.setdefault(block, []).append(verdict)
        order.append(block)
    return plan


def stub_flow(src, dst, yaml_mod):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    name = os.path.basename(src.rstrip("/"))
    path = os.path.join(dst, f"{name}.yaml")
    wf = yaml_mod.safe_load(open(path))
    os.makedirs(os.path.join(dst, "logic"), exist_ok=True)
    with open(os.path.join(dst, "logic", "_stubrun.py"), "w") as f:
        f.write(STUB)
    for b in wf.get("blocks") or []:
        runs_something = bool((b.get("logic") or {}).get("custom") or b.get("prompt") or b.get("command"))
        if not runs_something:
            continue  # left as it is: Rein must refuse it here exactly as on a live run
        for k in ("prompt", "specialist", "agents", "command", "save_as"):
            b.pop(k, None)
        b["logic"] = {"custom": "logic/_stubrun.py"}
    wf.pop("planner", None)
    with open(path, "w") as f:
        yaml_mod.safe_dump(wf, f, sort_keys=False)
    return name


def main():
    if len(sys.argv) != 3:
        print(__doc__.split("\n\n")[3], file=sys.stderr)
        return 2
    flow_dir, script = os.path.abspath(sys.argv[1]), sys.argv[2]
    try:
        import yaml
    except ImportError:
        print("PyYAML is required (run with the engine venv)", file=sys.stderr)
        return 2
    agents_root = os.path.dirname(os.path.dirname(flow_dir))
    tmp = tempfile.mkdtemp(prefix="stubrun-")
    try:
        agents = os.path.join(tmp, "agents")
        for d in ("flows", "specialists", "tasks", "state"):
            os.makedirs(os.path.join(agents, d))
        # The team a flow names: beside the flow when it lives in an agents
        # tree, otherwise the node's, in the tree these tools ship with. A flow
        # an author is still writing sits in his own directory with no teams/.
        teams = os.path.join(agents_root, "teams")
        if not os.path.isdir(teams):
            teams = os.path.join(os.environ.get("SN_FLOWS_AGENTS")
                                 or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents"),
                                 "teams")
        shutil.copytree(teams, os.path.join(agents, "teams"))
        name = stub_flow(flow_dir, os.path.join(agents, "flows", os.path.basename(flow_dir)), yaml)
        plan_f, log_f = os.path.join(tmp, "plan.json"), os.path.join(tmp, "log")
        json.dump(parse_script(script), open(plan_f, "w"))
        open(log_f, "w").close()
        env = {**os.environ, "STUBRUN_PLAN": plan_f, "STUBRUN_LOG": log_f}
        inp = json.dumps({"topic": "Task #1: stub run", "task_id": "1"})
        out = subprocess.run([PY, "-m", "rein", "--flow", name, "--step", "1", "--agents-dir", agents,
                              "--no-ui", "--input", inp], cwd=REIN_CWD, env=env,
                             capture_output=True, text=True).stdout
        m = re.search(r"Directory:\s*(/\S+/tasks/\S+)", out)
        if not m:
            print(f"rein did not start the flow:\n{out[-800:]}", file=sys.stderr)
            return 1
        run_dir = m.group(1)
        status = ""
        for _ in range(MAX_STEPS):
            try:
                status = open(os.path.join(run_dir, "state", "status")).read().strip()
            except OSError:
                status = ""
            if status == "completed":
                break
            subprocess.run([PY, "-m", "rein", "--step", "1", "--agents-dir", agents, "--task-dir", run_dir,
                            "--no-ui"], cwd=REIN_CWD, env=env, capture_output=True)
        acted = [line.split("=", 1)[0] for line in open(log_f).read().split()
                 if not line.endswith("=SKIPPED")]
        print("".join(f"{b}>" for b in acted))
        print(f"status={status or 'unknown'}")
        return 0 if status == "completed" else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
