# task-simple

A four-step task flow: a developer executes the task, a tester verifies it
independently and can send it back, a pm writes the delivery comment, and the
run closes. Version 2.0.0 (`metadata.version`).

**Owned by the Smart Node release.** An update overwrites this directory. To
change the flow for your node, copy it under another name and edit the copy.

## When to use

- A task one developer can do without an owner sign-off, where an independent
  check before delivery is enough.
- Anything the planner task describes in full; every step reads the live task
  and its comments.

Not for: work that needs the owner to approve a plan first (`task-complex`), or
anything that must not reach a model.

## How to use

Create a planner task bound to the flow (`workflow_id` 9, the id in
`sn_core.workflows`) with status `open`. The runner starts the flow on its next
tick and passes `task_id`. By hand:

    /opt/smart-node/flows/run-workflow.py task-simple "Task #<id>: <title>"

## What happens

1. **execute**: the developer model reads the task and posts its work as a comment.
2. **verify**: the tester model checks the work against the task and answers
   `APPROVE` or `REVISE`. A REVISE sends the work back to execute.
3. **deliver**: the pm model writes the delivery comment.
4. **done**: one closing comment; the run ends.

A failure, or a verification verdict nobody declared, ends at **stopped**
instead: one comment says which step stopped the run. Nothing is delivered.
The runner closes the task when the run completes; no step writes the task's
status or assignee.

## Who acts when

Rein marks the branch a routing decision did not choose as `skipped`. The Rein
vendored in this node forgets that mark between `--step` calls (fixed upstream in
rein-orchestrator, not yet vendored), and the runner calls Rein once per step, so
a branch nobody chose could still start later through `depends_on`. Every block
after a decision therefore starts in `logic/branch.py`: it reads the verdicts the
deciding blocks wrote in this run and runs the block's own script only when the
flow was sent there, otherwise it reports `SKIPPED` and writes nothing. With an
updated Rein a branch nobody chose never starts and these checks never say no.

| Block | Acts when | Runs |
|-------|-----------|------|
| execute | always (first step) | `logic/execute.py` |
| verify | execute reported anything but `FAIL` | `logic/verify.py` |
| deliver | verify answered `APPROVE` | `logic/deliver.py` |
| done | deliver reported anything but `FAIL` | `logic/done.py` |
| stopped | execute or deliver `FAIL`, or verify `FAIL` or an unknown verdict | `logic/stopped.py` |

## Edges

| Block | Verdict | Goes to |
|-------|---------|---------|
| execute | `FAIL` | stopped |
| execute | `DONE`, anything else, none | verify |
| verify | `APPROVE` | deliver |
| verify | `REVISE` | execute |
| verify | `FAIL`, anything else, none | stopped |
| deliver | `FAIL` | stopped |
| deliver | `DONE`, anything else | done |
| done | any | stop |
| stopped | any | stop |
| any block after a decision | `SKIPPED` | stop |

## What it does not guarantee

- The models decide what execute produces and whether it passes; only the tester bounds it.
- Loop limits: 3 runs per block (execute and verify, so two revisions).
- No owner approval gate; that is `task-complex`.

## Tests

`tests/task-simple.test.sh`: every edge and the negative cases with `flow-trace.py`,
every end on the real engine with `flow-stubrun.py`, the branch table, and the
contract (no empty block, no task writes, versions, lint).

    /opt/smart-node/flows/tools/flow-test.sh --flow task-simple

## Reference

- `companions/flows/README.md`: what a flow is made of, versioning, README and CHANGELOG.
- Flow determinism: blocks report verdicts, only the engine writes a task's status and assignee.
- `tools/flow-stubrun.py`: runs a flow on the real Rein engine with stub blocks (no model).
