# content-simple

One material, one approval, one publication: the writer writes it, the owner
approves it, the publisher publishes it once. Version 1.0.0
(`metadata.version`).

## When to use

- One piece of content that goes to one place: a post, an article, a letter to
  a list, a page.
- The owner wants to read it before it is public.

Not for: a material that is then adapted for several places (one flow run is one
publication), or content nobody has to approve.

## This is a sample

The flow is shipped as a sample to copy, not to run. It hands work to `writer`
and `publisher` and returns the task to `owner`. These three words are
placeholders: no node has agents with these names.

To use it:

1. Copy the directory under your own name (`blog-post`, say) and rename the
   `.yaml` and the test to match, and `name:` inside the file.
2. In `planner.on_block`, replace each placeholder with the name of an agent on
   your node, and `owner` with the owner's handle. The writer and the publisher
   may be the same agent.
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
(no tag is needed). The task says what to write and the one place it is
published to. When the gate asks, answer with a comment on the task whose first
line is `DECISION: approve`, `DECISION: modify` or `DECISION: reject`, with the
note under it; the comment lifts the hold. The task drawer has no buttons for
a flow gate.

## What happens

1. **draft**: the writer writes the material and posts a `DRAFT:` comment that
   says where it is; the task goes to the owner. Nothing is published.
2. **gate**: asks the owner and holds the task until a decision is recorded.
   Approve goes on, Modify sends the material back to the writer, Reject ends
   at **rejected**.
3. **publish**: the publisher publishes exactly the approved material, once,
   and reports where; the task returns to the owner.

A failed draft or publication ends at **stopped**. Every block's work is done by
a real agent in its own session; the flow only orders the hand-offs. What each
agent is asked to do is the block's order file, `orders/<block>.md`.

## Who acts when

Every block after a decision starts in `logic/branch.py`: it reads the verdicts
the deciding blocks wrote in this run and runs the block's own script only when
the flow was sent there, otherwise it reports `SKIPPED` and writes nothing. The
engine keeps a branch nobody chose skipped by itself since Rein 3.3.3; the table
stays as the flow's own statement of who acts when, and the tests walk it.

| Block | Acts when | Runs |
|-------|-----------|------|
| draft | always (first step), and when the owner sends the material back | `logic/delegate.py` (writer) |
| gate | draft reported (anything but `FAIL` or `WAITING`) | `logic/gate.py` (the owner) |
| publish | gate `approved`, and nothing else | `logic/delegate.py` (publisher) |
| rejected | gate `reject` | `logic/end.py` |
| stopped | draft or publish `FAIL`, or a gate answer it does not know | `logic/end.py` |

## Edges

| Block | Verdict | Goes to | Hand-off (`planner.on_block`) |
|-------|---------|---------|-------------------------------|
| draft | `WAITING` | draft | assignee writer, status in_progress |
| draft | `FAIL` | stopped | none |
| draft | `DONE`, anything else | gate | assignee owner |
| gate | `blocked` | gate (waits for the owner) | none |
| gate | `revise` | draft | none |
| gate | `reject` | rejected | none |
| gate | `approved` | publish | none |
| gate | anything else | stopped | none |
| publish | `WAITING` | publish | assignee publisher, status in_progress |
| publish | `FAIL` | stopped | assignee owner |
| publish | `DONE`, anything else | stop | assignee owner |
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

- That the material was published only once. The order says to publish once and
  not to retry after a failure, and the publish block runs once per approval;
  the flow cannot see what the publisher did outside the task. A stopped run
  says to check the task before anyone publishes again.
- That what was published is what was approved, word for word. The order says
  to publish the approved draft without edits; nothing compares the two.
- Loop limits: draft 8, gate 10, publish 4 runs.
- A finished run closes the task whichever end it reached; the end's comment
  says which. A task that has a reviewer is not closed by the flow: it goes to
  the reviewer in `pending_review`.
- The node has one owner identity: it cannot tell one approver from another.

## Tests

`tests/content-simple.test.sh`: every edge and negative case, every end on the
real engine, the branch table, the hand-offs, the orders, the contract. No model
and no planner are needed.

    /opt/smart-node/flows/tools/flow-test.sh --flow content-simple

## Reference

- `companions/flows/README.md`: what a flow is made of, versioning, README and
  CHANGELOG.
- Flow determinism: blocks report verdicts, only the engine writes a task's
  status and assignee.
- `tools/flow-stubrun.py`: runs a flow on the real engine with stub blocks (no
  model). `tools/flow-fuzz.py`: random walks over the routing.
