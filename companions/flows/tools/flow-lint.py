#!/usr/bin/env python3
"""flow-lint.py -- house rules for flows, on top of the engine's own schema.

The engine's schema (companions/rein/schemas/workflow-v<version>.json) answers "is this a
well-formed workflow". It does not answer "will this flow behave the same way
twice", which is the question that matters once a flow is a business process
rather than a demo. This asks the second question.

    flow-lint.py                  lint every flow under the flows tree
    flow-lint.py --flow NAME      one flow
    flow-lint.py --quiet          errors only
    flow-lint.py --root DIR       a tree other than the one in this checkout

    exit 0 = no errors, 1 = at least one ERROR

Rule numbers are shared with the determinism spec, so two estates can name the
same finding the same way.

    S0  the flow matches the engine's schema
    S1  planner state is not written from a prompt or by hand in the flow file
    S2  routing is total: every block routes, every route lands, there is a _default
    S3  a verdict a block can emit is a verdict routing knows, and keys are lowercase
    S4  a loop is bounded by max_runs on the block it returns to
    S5  the flow declares a terminal
    S6  two routing keys cannot match at once, because the engine picks by set order
    S7  a declared planner effect names a block, a lowercase verdict and a real status
    S8  the specialists and the team the flow names exist
    S9  an input a prompt reads is an input the flow declares
    S10 every name the flow hands a task to is an agent on this node, unless the
        flow is a sample (metadata.sample: true), whose names are placeholders
    S11 every script under the flow's logic/ parses
    S12 a planner effect the flow declares has something that writes it (the
        block's post hook), and a flow that declares effects handles a failed
        block (on_error)
"""
import argparse
import json
import os
import re
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
# S0 needs jsonschema, which lives in the same venv. A system python can have
# PyYAML and not jsonschema (Debian ships python3-yaml): the import above then
# passes, and every flow failed S0 with "jsonschema is not installed". The import
# door runs this file as a program, so it got exactly that python and refused
# every flow. Without the venv the S0 check below still says what is missing.
try:
    import jsonschema  # noqa: F401
except ImportError:
    _reexec_into_engine_python()

HERE = os.path.dirname(os.path.abspath(__file__))
# The tools ship inside the flows tree, so the tree is always one level up:
# /opt/smart-node/flows/tools -> /opt/smart-node/flows/agents on a node, and
# companions/flows/tools -> companions/flows/agents in a checkout.
DEFAULT_ROOT = os.environ.get("SN_FLOWS_AGENTS") or os.path.join(os.path.dirname(HERE), "agents")


# The schema versions a flow on a node may declare. A flow is checked against
# the one it declares: 3.4.0 adds time budgets, and a flow that sets one and
# was checked against 3.3.0 could never be published.
SCHEMA_VERSIONS = ("3.3.0", "3.4.0")


def _find_schema(version=SCHEMA_VERSIONS[0]):
    """The engine's schema of that version, wherever the engine is: beside it
    in a checkout, in the unpacked source on a node, or wherever
    SN_REIN_SCHEMAS says. "" when the engine here does not ship it."""
    candidates = [
        os.environ.get("SN_REIN_SCHEMAS", ""),
        os.path.join(os.path.dirname(os.path.dirname(HERE)), "rein", "schemas"),
        "/opt/smart-node/workflows/rein-src/schemas",
    ]
    for base in candidates:
        if not base:
            continue
        path = os.path.join(base, "workflow-v%s.json" % version)
        if os.path.exists(path):
            return path
    return ""


TERMINAL = {"_stop", "_end", "_done"}
# extract_verdict_signals() emits these in addition to the raw lowercased verdict.
ALIASES = {"pass": "needs-review", "approved": "needs-review", "revise": "revise"}
# A verdict that says the block did not do its job. Routing it where success goes
# is how a flow walks past its own failure.
# The planner's statuses (migration 0010): queued, in_progress, closed, with a
# wait_reason for queued and a close_outcome for closed. A flow may only ask for
# one of these; whether the move fits the state the task is in is the planner's
# business, and it refuses at write time, which the Runner logs.
PLANNER_STATUSES = {"queued", "in_progress", "closed"}
# The one-word names before 0010. The planner still accepts them; a flow that
# uses one gets a warning naming what to write instead.
LEGACY_STATUSES = {
    "open": "status: queued",
    "assessing": "status: in_progress",
    "awaiting_approval": "status: queued, wait_reason: approval",
    "pending_review": "status: queued, wait_reason: review",
    "blocked": "status: queued, wait_reason: person (or error)",
    "escalated": "status: queued, wait_reason: owner",
    "done": "status: closed, close_outcome: completed",
    "rejected": "status: closed, close_outcome: cancelled (or rejected)",
    "incident": "status: in_progress",
}
PLANNER_FIELDS = {"status", "assignee", "wait_reason", "close_outcome"}

FAILURE = re.compile(r"^(fail|failed|failure|error|blocked|rejected|missing|timeout)$")


class Report:
    def __init__(self):
        self.errors, self.warnings = [], []

    def error(self, flow, where, rule, msg):
        self.errors.append((flow, where, rule, msg))

    def warn(self, flow, where, rule, msg):
        self.warnings.append((flow, where, rule, msg))


def block_text(flow_dir, block):
    """Everything this block can execute: its prompt plus its logic scripts."""
    parts = [block.get("prompt") or ""]
    logic = block.get("logic") or {}
    for key in ("custom", "pre", "post", "error", "validate"):
        ref = logic.get(key)
        if isinstance(ref, str):
            path = os.path.join(flow_dir, ref)
            if os.path.exists(path):
                parts.append(open(path, errors="ignore").read())
    return "\n".join(parts)


def emitted_verdicts(text):
    """Verdict tokens this block can produce, read from its prompt and scripts."""
    out = set()
    for m in re.finditer(r"VERDICT:\s*([A-Za-z0-9_:-]+)", text):
        tok = m.group(1).lower()
        if tok and not tok.startswith("{"):
            out.add(tok)
            if tok in ALIASES:
                out.add(ALIASES[tok])
    return out


def lint_schema(name, path, wf, rep):
    # A flow that was not checked against the schema is not a checked flow: each
    # way of not checking is an error, never a remark that leaves the lint green.
    try:
        import jsonschema
    except ImportError:
        rep.error(name, os.path.basename(path), "S0",
                 "jsonschema is not installed, so the flow cannot be checked against the engine schema; run with the engine venv")
        return
    # Checked against the version the flow declares. An undeclared or unknown
    # version is checked against the oldest, whose own list of versions then
    # says what is wrong with it.
    declared = str(wf.get("schema_version") or "")
    version = declared if declared in SCHEMA_VERSIONS else SCHEMA_VERSIONS[0]
    schema_path = _find_schema(version)
    if not schema_path:
        rep.error(name, os.path.basename(path), "S0",
                 "the engine schema %s was not found, so structure was not checked: either "
                 "the engine on this node is older than the flow asks for, or SN_REIN_SCHEMAS "
                 "must name the directory holding workflow-v%s.json" % (version, version))
        return
    try:
        schema = json.load(open(schema_path))
    except Exception as exc:
        rep.error(name, os.path.basename(path), "S0", "schema unreadable: %s" % exc)
        return
    validator = jsonschema.Draft7Validator(schema)
    for err in sorted(validator.iter_errors(wf), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or os.path.basename(path)
        rep.error(name, where, "S0", err.message)


# A flow hands work to agents by name. The names are checked here, when the flow
# is written and tested, so that a placeholder left in from a sample, a typo or
# an agent that no longer exists is known at once and not when a task reaches
# that step. One directory per agent is how a node holds its agents.
AGENTS_ROOT = os.environ.get("SN_AGENTS_ROOT", "/srv/agents")
OWNER = os.environ.get("APPROVAL_OWNER", "admin")


def node_has(root, kind, filename):
    """Is this team or specialist known: beside the flow, or on the node.

    A flow an author is still writing sits in his own directory, with no teams/
    or specialists/ beside it, and uses the node's. So a name not found beside
    the flow is looked for in the tree these tools ship with.
    """
    return any(os.path.exists(os.path.join(r, kind, filename)) for r in (root, DEFAULT_ROOT))


def node_agents():
    """Names of the agents on this node, or None when this is not a node."""
    if not os.path.isdir(AGENTS_ROOT):
        return None
    return {d for d in os.listdir(AGENTS_ROOT)
            if not d.startswith(("_", ".")) and os.path.isdir(os.path.join(AGENTS_ROOT, d))}


def lint_assignees(name, path, wf, on_block, rep):
    named = {}
    for bn, rules in sorted((on_block or {}).items()):
        if not isinstance(rules, dict):
            continue
        for verdict, effect in sorted(rules.items()):
            who = effect.get("assignee") if isinstance(effect, dict) else None
            if isinstance(who, str) and who.strip():
                named.setdefault(who.strip(), "%s/%s" % (name, bn))
    if not named:
        return
    if (wf.get("metadata") or {}).get("sample") is True:
        rep.warn(name, os.path.basename(path), "S10",
                 "a sample: %s are placeholder names, so this flow is not meant to run; "
                 "copy it and write your agents' names in" % ", ".join(sorted(named)))
        return
    agents = node_agents()
    if agents is None:
        rep.error(name, os.path.basename(path), "S10",
                  "the flow hands tasks to %s, and there is no agents directory at %s to "
                  "check the names against; lint it on the node it will run on, or set "
                  "SN_AGENTS_ROOT" % (", ".join(sorted(named)), AGENTS_ROOT))
        return
    for who, where in sorted(named.items()):
        if who != OWNER and who not in agents:
            rep.error(name, where, "S10",
                      "hands the task to '%s', and no agent on this node has that name "
                      "(the owner is '%s')" % (who, OWNER))


def lint_flow(name, path, rep, root):
    flow_dir = os.path.dirname(path)
    raw = open(path).read()
    try:
        wf = yaml.safe_load(raw) or {}
    except Exception as exc:
        rep.error(name, os.path.basename(path), "S0", "YAML does not parse: %s" % exc)
        return

    lint_schema(name, path, wf, rep)

    blocks = wf.get("blocks") or []
    names = {b.get("name") for b in blocks}

    # -- S1: the planner belongs to the engine --------------------------------
    for m in re.finditer(r"--(status|assignee)\b", raw):
        line = raw[:m.start()].count("\n") + 1
        rep.error(name, "%s:%d" % (os.path.basename(path), line), "S1",
                  "flow writes tasks.%s; that field belongs to the engine" % m.group(1))
    for m in re.finditer(r"PATCH[^\n]*", raw):
        tail = raw[m.start():m.start() + 400]
        hit = re.search(r'"(status|assignee)"\s*:', tail)
        if hit:
            line = raw[:m.start()].count("\n") + 1
            rep.error(name, "%s:%d" % (os.path.basename(path), line), "S1",
                      "a prompt tells the model to PATCH tasks.%s; that field belongs "
                      "to the engine, and prose is not a transition" % hit.group(1))

    # -- S5: a terminal exists ------------------------------------------------
    has_terminal = any(v in TERMINAL
                       for b in blocks for v in (b.get("routing") or {}).values())
    if not has_terminal:
        rep.error(name, os.path.basename(path), "S5",
                  "no block routes to a terminal, so the walk can only end by exhaustion")

    # -- S11: a block script is a program ---------------------------------------
    # A script that does not parse fails only when a run reaches its block, and
    # the block that reports a stop is reached least often of all: task-simple
    # shipped a stopped.py with a string broken across lines, and every test of
    # the flow passed, because none of them ran that script.
    logic_dir = os.path.join(flow_dir, "logic")
    for fn in sorted(os.listdir(logic_dir)) if os.path.isdir(logic_dir) else []:
        if not fn.endswith(".py"):
            continue
        try:
            compile(open(os.path.join(logic_dir, fn)).read(), fn, "exec")
        except SyntaxError as exc:
            rep.error(name, "logic/%s:%s" % (fn, exc.lineno), "S11",
                      "the script does not parse: %s" % exc.msg)

    declared_inputs = set((wf.get("inputs") or {}).keys())

    for i, b in enumerate(blocks):
        bn = b.get("name", "#%d" % i)
        where = "%s/%s" % (name, bn)
        routing = b.get("routing") or {}
        text = block_text(flow_dir, b)

        # -- S1 in scripts ---------------------------------------------------
        if re.search(r"UPDATE\s+tasks\s+SET\s+status", text):
            rep.error(name, where, "S1", "a block script writes tasks.status")

        # -- S2: routing is total --------------------------------------------
        if not routing and not b.get("next"):
            rep.error(name, where, "S2",
                      "no routing and no next: the verdict is discarded and only "
                      "depends_on can move the flow")
        if routing and "_default" not in routing:
            rep.error(name, where, "S2",
                      "routing has no _default: an undeclared verdict falls through "
                      "to depends_on scheduling")
        for k, v in routing.items():
            if v not in names and v not in TERMINAL:
                rep.error(name, where, "S2",
                          "routing '%s' leads to '%s', which does not exist" % (k, v))

        # -- S3: keys the engine can actually match ---------------------------
        for k in routing:
            if k != "_default" and k != k.lower():
                rep.error(name, where, "S3",
                          "routing key '%s' is not lowercase; the engine lowercases "
                          "every verdict, so this key can never match" % k)
        emitted = emitted_verdicts(text)
        for v in sorted(emitted):
            if v in routing or v == "_default":
                continue
            if routing.get("_default"):
                rep.warn(name, where, "S3",
                         "emits '%s', which no routing key matches, so it takes _default" % v)
            elif routing:
                rep.error(name, where, "S3", "emits '%s', which no routing key matches" % v)
        for v in sorted(emitted):
            if FAILURE.match(v) and routing.get(v) and routing.get(v) == routing.get("_default"):
                rep.warn(name, where, "S3",
                         "failure verdict '%s' routes where success routes" % v)

        # -- S4: a loop is bounded ---------------------------------------------
        for k, v in routing.items():
            if v in TERMINAL or v not in names:
                continue
            target_index = next((j for j, x in enumerate(blocks) if x.get("name") == v), None)
            if target_index is not None and target_index <= i:
                if not blocks[target_index].get("max_runs"):
                    rep.error(name, where, "S4",
                              "'%s' routes back to '%s', which declares no max_runs, "
                              "so the loop is unbounded" % (k, v))

        # -- S6: two keys cannot match at once ---------------------------------
        matchable = {v for v in emitted if v in routing}
        if len(matchable) > 1:
            rep.warn(name, where, "S6",
                     "can emit %s, and all of them are routing keys; if two appear in "
                     "one result the engine picks by set order, not by the flow"
                     % ", ".join(sorted(matchable)))

        # -- S8: the people it names exist --------------------------------------
        sp = b.get("specialist")
        if sp and not node_has(root, "specialists", sp + ".md"):
            rep.error(name, where, "S8", "specialist '%s' has no file" % sp)

        # -- S9: the inputs it reads are declared -------------------------------
        for m in re.finditer(r"\{\{\s*task\.input\.([a-zA-Z0-9_]+)", text):
            if m.group(1) not in declared_inputs:
                rep.error(name, where, "S9",
                          "reads task.input.%s, which the flow does not declare" % m.group(1))

    # -- S7: the planner effects the flow declares ----------------------------
    planner = wf.get("planner") or {}
    on_block = planner.get("on_block") or {}
    if planner and not isinstance(on_block, dict):
        rep.error(name, os.path.basename(path), "S7", "planner.on_block is not a mapping")
        on_block = {}
    for bn, rules in sorted(on_block.items()):
        where = "%s/%s" % (name, bn)
        if bn not in names:
            rep.error(name, where, "S7",
                      "planner effects declared for '%s', which is not a block" % bn)
        if not isinstance(rules, dict):
            rep.error(name, where, "S7", "planner effects for '%s' are not a mapping" % bn)
            continue
        for verdict, effect in sorted(rules.items()):
            if verdict != verdict.lower():
                rep.error(name, where, "S7",
                          "planner effect keyed on '%s'; verdicts arrive lowercased, so "
                          "this key can never match" % verdict)
            if not isinstance(effect, dict):
                rep.error(name, where, "S7",
                          "planner effect for '%s' is not a mapping of fields" % verdict)
                continue
            for field, value in sorted(effect.items()):
                if field not in PLANNER_FIELDS:
                    rep.error(name, where, "S7",
                              "planner effect writes '%s'; only status, wait_reason, "
                              "close_outcome and assignee are applied, so this would be "
                              "silently ignored" % field)
                if field == "status" and value in LEGACY_STATUSES:
                    rep.warn(name, where, "S7",
                             "planner effect asks for the old status '%s'; write %s"
                             % (value, LEGACY_STATUSES[value]))
                elif field == "status" and value not in PLANNER_STATUSES:
                    rep.error(name, where, "S7",
                              "planner effect asks for status '%s', which is not a planner "
                              "status" % value)
            # planner 0010: a reason belongs to queued, an outcome to closed, and a
            # close without an outcome is refused by the planner at write time.
            st = effect.get("status")
            if "wait_reason" in effect and st not in (None, "queued"):
                rep.error(name, where, "S7",
                          "planner effect sets wait_reason with status '%s'; a reason goes "
                          "with status queued" % st)
            if "close_outcome" in effect and st not in (None, "closed"):
                rep.error(name, where, "S7",
                          "planner effect sets close_outcome with status '%s'; an outcome "
                          "goes with status closed" % st)
            if st == "closed" and not effect.get("close_outcome"):
                rep.error(name, where, "S7",
                          "planner effect asks for status closed without a close_outcome; "
                          "the planner refuses that close")
        if "_default" not in rules and rules:
            rep.warn(name, where, "S7",
                     "planner effects for '%s' have no _default, so a verdict none of them "
                     "names leaves the task where it was" % bn)
    lint_assignees(name, path, wf, on_block, rep)

    # -- S12: a declared effect is written by the flow itself ------------------
    # The Runner launches a flow and writes neither status nor assignee for it.
    # A flow written while the Runner still applied planner.on_block declares
    # its hand-offs and carries nothing that writes them: it runs block after
    # block and its task never moves, with no error anywhere. That has to be
    # known when the flow is linted, not when a task stands still.
    by_name = {b.get("name"): b for b in blocks}
    declares = False
    for bn, rules in sorted(on_block.items()):
        if not isinstance(rules, dict) or bn not in by_name:
            continue
        if not any(isinstance(effect, dict) and effect for effect in rules.values()):
            continue
        declares = True
        if not (by_name[bn].get("logic") or {}).get("post"):
            rep.error(name, "%s/%s" % (name, bn), "S12",
                      "declares a planner effect and has no post hook, so nothing writes "
                      "it: the Runner does not move a flow's task. Give the block "
                      "'post: logic/planner_effect.py' under logic")
    if declares and not wf.get("on_error"):
        rep.error(name, os.path.basename(path), "S12",
                  "declares planner effects and no on_error, so a block that fails leaves "
                  "the task where it was and tells nobody. Add 'on_error: logic/on_error.py'")

    team = wf.get("team")
    if team and not node_has(root, "teams", team + ".yaml"):
        rep.error(name, os.path.basename(path), "S8", "team '%s' has no file" % team)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="the agents dir holding flows/, specialists/ and teams/")
    ap.add_argument("--flow", default="", help="lint one flow by name")
    ap.add_argument("--quiet", action="store_true", help="errors only")
    args = ap.parse_args()

    flows_dir = os.path.join(args.root, "flows")
    if not os.path.isdir(flows_dir):
        print("no flows under %s" % flows_dir, file=sys.stderr)
        return 2

    rep = Report()
    count = 0
    for d in sorted(os.listdir(flows_dir)):
        if args.flow and d != args.flow:
            continue
        path = os.path.join(flows_dir, d, d + ".yaml")
        if not os.path.exists(path):
            continue
        count += 1
        lint_flow(d, path, rep, args.root)

    for _flow, where, rule, msg in rep.errors:
        print("ERROR %-3s %s\n        %s" % (rule, where, msg))
    if not args.quiet:
        for _flow, where, rule, msg in rep.warnings:
            print("warn  %-3s %s\n        %s" % (rule, where, msg))
    print("\n%d flow(s) linted | %d errors | %d warnings"
          % (count, len(rep.errors), len(rep.warnings)))
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
