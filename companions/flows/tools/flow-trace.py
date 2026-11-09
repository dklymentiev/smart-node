#!/usr/bin/env python3
"""flow-trace.py -- walk a flow's state machine without running it.

A flow is a state machine whose transition function is ours and whose input
symbol comes from a language model. This walks the transition function with the
input symbols supplied on the command line, so the machine can be tested
without a model, without the gateway and without a node.

It does not re-implement routing: it imports the engine's own
rein/routing_engine.py, so a change there shows up here rather than being
quietly contradicted by a copy. The max_runs rule is mirrored from
orchestrator.py (_handle_routing), which is the only part of the walk that does
not live in a pure function yet.

    flow-trace.py FLOW.yaml --verdict draft=PASS --verdict review=revise ...
    flow-trace.py FLOW.yaml --script "draft=PASS,review=REVISE,draft=PASS,review=PASS"

Verdicts are consumed in order per block, so a block that runs twice takes its
first entry, then its second. A block with no verdict left emits nothing, which
is the honest model of a block whose output carries no VERDICT line at all.

Output is one line per step, plus a final STOP line naming why it stopped:

    draft   -> review   signal=_default
    review  -> draft    signal=revise
    draft   -> review   signal=_default
    review  -> deliver  signal=_default
    deliver -> done     signal=_default
    STOP    _stop at done

Exit code is 0 when the walk ends at a stop the flow declares, 1 when it ends
because the machine ran out of transitions, which is a flow defect.
"""
import argparse
import json
import os
import sys

# PyYAML lives in the engine's venv on a node, not in the system python, and the
# person running this should not have to know that. If the import fails and that
# venv is there, re-exec into it once.
def _reexec_into_engine_python():
    venv = os.environ.get("SN_FLOW_PY") or "/opt/smart-node/workflows/venv/bin/python"
    if os.path.exists(venv) and os.environ.get("SN_FLOW_REEXEC") != "1":
        os.environ["SN_FLOW_REEXEC"] = "1"
        os.execv(venv, [venv, os.path.abspath(__file__)] + sys.argv[1:])


try:
    import yaml
except ImportError:
    _reexec_into_engine_python()
    raise

HERE = os.path.dirname(os.path.abspath(__file__))
# Import the engine's routing rather than describing it. The engine sits beside
# the flows tree in a checkout and under /opt/smart-node/workflows on a node.
_REIN_CANDIDATES = [
    os.environ.get("SN_REIN_SRC", ""),
    os.path.join(os.path.dirname(os.path.dirname(HERE)), "rein"),
    "/opt/smart-node/workflows/rein-src",
]
for _base in _REIN_CANDIDATES:
    if _base and os.path.isdir(os.path.join(_base, "rein")):
        sys.path.insert(0, _base)
        break
try:
    from rein import routing_engine
except Exception as exc:  # pragma: no cover - import failure is a setup error
    print("cannot import rein.routing_engine (looked in %s): %s"
          % (", ".join(c for c in _REIN_CANDIDATES if c), exc), file=sys.stderr)
    sys.exit(2)

TERMINAL = {"_stop", "_end", "_done"}
DEFAULT_MAX_RUNS = 3   # orchestrator.default_max_runs
STEP_CAP = 50          # the walk's own guard, not the flow's


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("flow")
    ap.add_argument("--verdict", action="append", default=[],
                    help="block=VERDICT, repeatable, consumed in order per block")
    ap.add_argument("--script", default="",
                    help="comma separated block=VERDICT list, same thing")
    ap.add_argument("--start", default="", help="start block (default: first with no depends_on)")
    ap.add_argument("--json", action="store_true", help="emit the trace as JSON")
    return ap.parse_args()


def verdict_queues(entries):
    queues = {}
    for e in entries:
        if not e:
            continue
        block, _, verdict = e.partition("=")
        queues.setdefault(block.strip(), []).append(verdict.strip())
    return queues


def result_text(verdict):
    """What the block's result.json would carry. A verdict of the form
    'A|B' emits two VERDICT lines, which is a thing blocks do and a thing the
    routing engine does not defend against."""
    if verdict is None or verdict == "":
        return ""
    return "\n".join("VERDICT: %s" % v for v in verdict.split("|"))


def main():
    args = parse_args()
    wf = yaml.safe_load(open(args.flow)) or {}
    blocks = {b.get("name"): b for b in (wf.get("blocks") or [])}
    order = [b.get("name") for b in (wf.get("blocks") or [])]
    if not blocks:
        print("flow has no blocks", file=sys.stderr)
        return 2

    queues = verdict_queues(args.verdict + args.script.split(","))
    start = args.start or next((n for n in order if not blocks[n].get("depends_on")), order[0])

    run_counts = {start: 1}
    trace = []
    current = start
    reason = None

    for _ in range(STEP_CAP):
        block = blocks.get(current) or {}
        routing = block.get("routing") or {}
        verdict = None
        if queues.get(current):
            verdict = queues[current].pop(0)
        signals = routing_engine.extract_verdict_signals(result_text(verdict))
        matched_keys = sorted(s for s in signals if s in routing)
        nxt, signal = routing_engine.match_routing_rule(routing, signals)

        step = {"block": current, "verdict": verdict, "signal": signal, "next": nxt,
                "ambiguous": matched_keys if len(matched_keys) > 1 else []}
        trace.append(step)

        if len(matched_keys) > 1:
            # match_routing_rule iterates a set, so which of these wins is decided
            # by string hashing, not by the flow. Two runs, two answers.
            reason = "ambiguous at %s: %s both route" % (current, ", ".join(matched_keys))
            break
        if not routing:
            reason = "no routing at %s: the verdict is discarded and only depends_on can move the flow" % current
            break
        if nxt in TERMINAL:
            reason = "%s at %s" % (nxt, current)
            break
        if not nxt:
            reason = "no route from %s for signal %s" % (current, signal)
            break

        target = blocks.get(nxt)
        max_runs = (target or {}).get("max_runs", DEFAULT_MAX_RUNS)
        runs = run_counts.get(nxt, 0)
        if runs >= max_runs:
            reason = "max_runs at %s: %d >= %d, routing stops and the flow is left to depends_on" % (
                nxt, runs, max_runs)
            break
        run_counts[nxt] = runs + 1
        current = nxt
    else:
        reason = "step cap reached: the walk did not terminate in %d steps" % STEP_CAP

    if args.json:
        print(json.dumps({"trace": trace, "stop": reason, "runs": run_counts}, indent=2))
    else:
        for s in trace:
            print("%-10s -> %-10s signal=%s%s" % (
                s["block"], s["next"] or "-", s["signal"],
                "  AMBIGUOUS(%s)" % ",".join(s["ambiguous"]) if s["ambiguous"] else ""))
        print("STOP  %s" % reason)

    clean = reason.split()[0] in TERMINAL or reason.startswith("max_runs")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
