# task-development

The development pipeline an owner approves: the project manager plans, the
owner approves the plan, the developer executes it, the tester verifies it
independently, the project manager delivers. Version 1.0.0
(`metadata.version`).

## When to use

- Development or operations work where the owner wants to see and approve the
  plan before anything changes.
- Work an independent tester can check against acceptance criteria.

Not for: small tasks with no sign-off (`task-simple`), or anything that needs
breaking into subtasks first (not supported).

## This is a sample

The flow is shipped as a sample to copy, not to run. It hands work to `pm`,
`developer` and `tester` and returns the task to `owner`. These four words are
placeholders: no node has agents with these names.

To use it:

1. Copy the directory under your own name (`dev-pipeline`, say) and rename the
   `.yaml` and the test to match, and `name:` inside the file.
2. In `planner.on_block`, replace each placeholder with the name of an agent on
   your node, and `owner` with the owner's handle. One agent may appear twice,
   but the tester must not be the developer: the verification is independent
   only if someone else did the work.
3. Remove `sample: true` from `metadata`.
4. Run `flow-test.sh --flow <your name>` on the node. It fails if any name is
   not an agent there, so a typo or a placeholder left in is known before the
   flow is ever used. Then register the flow.

A flow is written with the names of real agents. Nothing stands between the
flow and the node's agents, so to change who does a step you edit the flow and
test it again.

A sample is not registered: it does not appear among the workflows a task can
be bound to.

## How to use

Create a planner task bound to the flow (its `workflow_id`) with status `open`;
the runner starts it. A task filed by an agent waits for the owner's approval
and never passes through `open`; the runner starts it once it is approved
(no tag is needed). When the gate asks, answer with a comment on the task
whose first line is `DECISION: approve`, `DECISION: modify` or
`DECISION: reject`, with the note under it; the comment lifts the hold. The
task drawer has no buttons for a flow gate.

## What happens

1. **plan**: the project manager writes a `PLAN:` comment; the task goes to the
   owner.
2. **gate**: asks the owner and holds the task until a decision is recorded.
   Approve goes on, Modify sends the plan back, Reject ends at **rejected**.
3. **execute**: the developer does the work and reports.
4. **verify**: the tester checks it independently: PASS goes on, REVISE sends
   the work back, BLOCKED ends at **stopped**.
5. **deliver**: the project manager writes the delivery; the task returns to
   the owner.

A failed plan or execution also ends at **stopped**. Every block's work is done
by a real agent in its own session; the flow only orders the hand-offs. What
each agent is asked to do is the block's order file, `orders/<block>.md`.

## Who acts when

Every block after a decision starts in `logic/branch.py`: it reads the verdicts
the deciding blocks wrote in this run and runs the block's own script only when
the flow was sent there, otherwise it reports `SKIPPED` and writes nothing. The
engine keeps a branch nobody chose skipped by itself since Rein 3.3.3; the table
stays as the flow's own statement of who acts when, and the tests walk it.

| Block | Acts when | Runs |
|-------|-----------|------|
| plan | always (first step), and when the owner sends the plan back | `logic/delegate.py` (pm) |
| gate | plan reported (anything but `FAIL` or `WAITING`) | `logic/gate.py` (the owner) |
| execute | gate `approved` | `logic/delegate.py` (developer) |
| verify | execute reported (anything but `FAIL` or `WAITING`) | `logic/delegate.py` (tester) |
| deliver | verify passed (anything but `REVISE` or `BLOCKED`) | `logic/delegate.py` (pm) |
| rejected | gate `reject` | `logic/end.py` |
| stopped | plan or execute `FAIL`, verify `BLOCKED`, or a gate answer it does not know | `logic/end.py` |

## Edges

| Block | Verdict | Goes to | Hand-off (`planner.on_block`) |
|-------|---------|---------|-------------------------------|
| plan | `WAITING` | plan | assignee pm, status in_progress |
| plan | `FAIL` | stopped | none |
| plan | `DONE`, anything else | gate | assignee owner |
| gate | `blocked` | gate (waits for the owner) | none |
| gate | `revise` | plan | none |
| gate | `reject` | rejected | none |
| gate | `approved` | execute | none |
| gate | anything else | stopped | none |
| execute | `WAITING` | execute | assignee developer, status in_progress |
| execute | `FAIL` | stopped | none |
| execute | `DONE`, anything else | verify | none |
| verify | `WAITING` | verify | assignee tester, status in_progress |
| verify | `REVISE` | execute | none |
| verify | `BLOCKED` | stopped | none |
| verify | `PASS`, anything else | deliver | none |
| deliver | `WAITING` | deliver | assignee pm, status in_progress |
| deliver | `DONE`, anything else | stop | assignee owner |
| rejected, stopped | any | stop | assignee owner |
| any block after a decision | `SKIPPED` | stop | none |

The owner's decision is read as data: the tag `decision:<approve|modify|reject>`
or the first line `DECISION: <what>` of a comment after the gate's request.

## What the agents are told, and what holds if they do not listen

An agent in a block decides; the flow acts. Each order ends by telling the agent
not to change the task's status or assignee, and the planner instructions every
agent has say the same about any task that carries a work order. That is an
instruction, not a lock: nothing refuses the write. So the flow does not depend
on it. Every hand-off writes the status together with the assignee, and the
runner keeps driving a run whose task an agent moved to `pending_review`.

## What it does not guarantee

- The model decides what execute changes; the plan and the tester bound it.
- Loop limits: plan 8, gate 10, execute 8, verify 6, deliver 4 runs.
- A finished run closes the task whichever end it reached; the end's comment
  says which. A task that has a reviewer is not closed by the flow: it goes to
  the reviewer in `pending_review`.
- It does not break a request into subtasks. The node has one owner identity:
  it cannot tell one approver from another.

## Tests

`tests/task-development.test.sh`: every edge and negative case, every end on
the real engine, the branch table, the hand-offs, the contract. No model and no
planner are needed.

    /opt/smart-node/flows/tools/flow-test.sh --flow task-development

## Reference

- `companions/flows/README.md`: what a flow is made of, versioning, README and
  CHANGELOG.
- Flow determinism: blocks report verdicts, only the engine writes a task's
  status and assignee.
- `tools/flow-stubrun.py`: runs a flow on the real engine with stub blocks (no
  model). `tools/flow-fuzz.py`: random walks over the routing.
