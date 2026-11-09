#!/opt/smart-node/workflows/venv/bin/python
"""Run a workflow and emit its steps into sn_core.events so the run is visible
in the panel Events history.

This is the surface behind POST /api/workflows/{name}/run. It knows nothing
about any particular flow: block names, inputs and actors are read from the
flow's own YAML, so a flow authored on this node five minutes ago emits the
same events as one that shipped with it.

    run-workflow.py <flow> "<request text>" [approve|reject]

It replaces a demo harness that hardcoded one business's block names, document
GUIDs and sample customer, and therefore emitted nothing at all for any other
flow.

The run happens in place under the flows tree, not in a copy under /tmp: a run
you cannot inspect afterwards is not much of a record.
"""
import glob
import json
import os
import subprocess
import sys
import urllib.request

# Provided by the engine venv this script's shebang names. Imported here rather
# than inside the function that needs it so a wrong interpreter is a startup
# error naming the module, not a traceback three steps into a run.
import yaml

FLOWS_ROOT = os.environ.get("SN_FLOWS_ROOT", "/opt/smart-node/flows")
AGENTS_DIR = os.path.join(FLOWS_ROOT, "agents")
REIN_PY = os.environ.get("SN_REIN_PY", "/opt/smart-node/workflows/venv/bin/python")
REIN_CWD = os.environ.get("SN_REIN_CWD", "/opt/smart-node/workflows")
CORE = os.environ.get("SN_CORE_EVENTS", "")
GATEWAY = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000")
TENANT = os.environ.get("AI_GATEWAY_TENANT_ID", "1")

if len(sys.argv) < 2:
    print(__doc__.strip(), file=sys.stderr)
    sys.exit(2)

FLOW = sys.argv[1]
REQUEST = sys.argv[2] if len(sys.argv) > 2 else ""
DECISION = sys.argv[3] if len(sys.argv) > 3 else "approve"

if not CORE:
    print(
        "SN_CORE_EVENTS is not set. "
        "The node installer should write this to the core-api service environment "
        "(companions/core-api.sh). Without it every workflow launch fails at the "
        "first event emit.",
        file=sys.stderr,
    )
    sys.exit(1)


def _service_headers():
    """The proof core-api asks of an event writer.

    This script is started by core-api and inherits its environment, which
    carries the service credential. core-api admits an event write only from a
    listed node service that proves itself, so the write names this one. There
    is no bare form: without the credential the write would be refused, and the
    reason is said here instead.
    """
    secret = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()
    if not secret:
        raise RuntimeError("SN_SERVICE_AUTH_SECRET is not set in the environment "
                           "this run inherited (re-run the core-api companion)")
    return {"X-SN-Service": "flows", "X-SN-Service-Auth": secret}


def emit(actor, event_type, payload, parent=None):
    body = json.dumps({"actor": actor, "event_type": event_type,
                       "entity_type": "workflow", "entity_id": FLOW,
                       "payload": payload, "parent_event_id": parent}).encode()
    try:
        req = urllib.request.Request(CORE, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              **_service_headers()})
        return json.loads(urllib.request.urlopen(req, timeout=8).read()).get("id")
    except Exception as exc:
        # The run is the point; the event trail is commentary. Losing the trail
        # must not lose the run, but it must not pass silently either.
        print("emit failed (%s): %s" % (event_type, exc), file=sys.stderr)
        return None


def load_flow():
    path = os.path.join(AGENTS_DIR, "flows", FLOW, FLOW + ".yaml")
    if not os.path.exists(path):
        print("no such flow on disk: %s" % path, file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(open(path)) or {}


def build_input(doc):
    """Fill the inputs the flow declares, without knowing what they mean.

    The request text goes to the first declared input that looks like the
    subject, or failing that to the first required one. Anything else declared
    gets its default, so a flow that wants more than a subject line still starts
    rather than failing on a missing key.
    """
    declared = doc.get("inputs") or {}
    values = {}
    for name, spec in declared.items():
        spec = spec or {}
        if spec.get("default") is not None:
            values[name] = spec.get("default")
    for preferred in ("topic", "inquiry", "request", "subject", "text"):
        if preferred in declared:
            values[preferred] = REQUEST
            break
    else:
        required = [n for n, s in declared.items() if (s or {}).get("required", True)]
        if required:
            values[required[0]] = REQUEST
    if "owner_decision" in declared:
        values["owner_decision"] = DECISION
    if "task_id" in declared and "task_id" not in values:
        values["task_id"] = 0
    return values


def task_dirs():
    return set(glob.glob(os.path.join(AGENTS_DIR, "tasks", "*/")))


def pick_task_dir(before):
    """The directory this run created.

    Taking the newest one instead would attribute a concurrent run's output to
    this one, and two runs at once is the normal case for a node with a panel
    and a dispatcher both able to start a flow.
    """
    fresh = task_dirs() - before
    if fresh:
        return sorted(fresh, key=os.path.getmtime)[-1]
    return None


def block_result(task_dir, name):
    path = os.path.join(task_dir, name, "outputs", "result.json")
    if not os.path.exists(path):
        return None
    try:
        result = json.load(open(path)).get("result")
    except Exception:
        return None
    return result if isinstance(result, str) else json.dumps(result)


doc = load_flow()
blocks = [b.get("name") for b in (doc.get("blocks") or []) if b.get("name")]

root = emit("system", "workflow.started",
            {"flow": FLOW, "request": REQUEST[:500], "owner_decision": DECISION,
             "blocks": blocks})

if not os.path.exists(REIN_PY):
    emit("system", "workflow.completed",
         {"flow": FLOW, "ok": False, "reason": "engine interpreter missing: " + REIN_PY},
         root)
    print("the Rein interpreter is not installed at %s -- is the workflows "
          "companion installed?" % REIN_PY, file=sys.stderr)
    sys.exit(1)

env = dict(os.environ, AI_GATEWAY_URL=GATEWAY, AI_GATEWAY_TENANT_ID=TENANT)
before = task_dirs()
run = subprocess.run(
    [REIN_PY, "-m", "rein", "--flow", FLOW, "--agents-dir", AGENTS_DIR,
     "--no-ui", "--input", json.dumps(build_input(doc))],
    cwd=REIN_CWD, env=env, capture_output=True, text=True)

task_dir = pick_task_dir(before)
produced = 0
if task_dir:
    for block in blocks:
        text = block_result(task_dir, block)
        if text is None:
            continue
        produced += 1
        spec = next((b for b in doc["blocks"] if b.get("name") == block), {})
        emit(spec.get("specialist") or "system", "workflow.step",
             {"block": block, "specialist": spec.get("specialist"),
              "summary": text[:500]}, root)

# What "completed" means here is that rein exited and some block produced a
# result. A flow that ran every block and a flow that died in the first one both
# reach this line, so the difference has to be in what is said, not in whether
# anything is said at all.
ok = run.returncode == 0 and produced > 0
emit("system", "workflow.completed",
     {"flow": FLOW, "ok": ok, "exit_code": run.returncode,
      "blocks_with_output": produced, "blocks_declared": len(blocks),
      "task_dir": task_dir}, root)

if not ok:
    tail = (run.stderr or run.stdout or "").strip().splitlines()[-5:]
    print("WORKFLOW RUN FAILED flow=%s exit=%s blocks_with_output=%d"
          % (FLOW, run.returncode, produced), file=sys.stderr)
    for line in tail:
        print("  %s" % line, file=sys.stderr)
    sys.exit(1)

print("WORKFLOW RUN COMPLETE flow=%s blocks=%d/%d task_dir=%s"
      % (FLOW, produced, len(blocks), task_dir))
