# Flows: the workflow runtime tree

This is what a node gets at `/opt/smart-node/flows`. It ships empty of any
business: one reference flow and the two generic specialists it needs, and
nothing else. A node may be a bakery, a salon, a distillery or a steel mill, and
none of them should find another one's processes installed on their machine.

## Layout

```
/opt/smart-node/flows/
├── run-workflow.py            the surface behind POST /api/workflows/{name}/run
├── agents/                    what you author; Rein reads this as --agents-dir
│   ├── flows/<name>/<name>.yaml       one directory per flow
│   ├── flows/<name>/logic/*.py        logic-block scripts for that flow
│   ├── specialists/<name>.md          block prompts
│   ├── teams/team-<name>.yaml         which specialists a flow may use
│   ├── tasks/                         one directory per run (output)
│   └── state/                         Rein bookkeeping
└── runner-agents/             what the dispatcher runs; assembled at install
    ├── flows   -> ../agents/flows
    ├── teams   -> ../agents/teams
    ├── specialists/                   block prompts for runner-driven flows
    ├── tasks/                         one directory per run
    └── state/
```

The paths are not arbitrary: `companions/brin/runner.py` and
`companions/core-api/routes/workflows.py` name them, and
`register-workflows.py` registers what it finds under `agents/flows` into
`sn_core.workflows`. A flow that is not under `agents/flows/<name>/<name>.yaml`
is a flow nothing can find.

## A flow is a directory

Everything that describes a flow lives inside the flow's own directory, so it can
be copied to another node and still be readable, checkable and runnable there:

```
agents/flows/<name>/
├── <name>.yaml            the machine, and its version under metadata
├── README.md              the contract: the edges, the verdicts, the effects,
│                          and what the flow does not guarantee
├── logic/                 block scripts, if the flow has any
└── tests/*.test.sh        the machine walked without a model
```

Only `<name>/<name>.yaml` is read as a flow. `register-workflows.py` globs one
level deep, so keep fixtures and test data under `tests/`, not beside the YAML,
or they will be registered as workflows of their own.

## The tools

`tools/` ships with this tree and lands on the node beside it, because the people
who need it are there: an owner checking a flow they wrote in the panel, an agent
asked to author one, a run that wants to know whether the flow it is about to
start is well formed.

```
tools/flow-lint.py     the engine's schema, then the house rules (S0-S12)
tools/flow-trace.py    walk a flow with verdicts you supply: no gateway, no model
tools/flow-test.sh     lint every flow, then run the tests each flow carries
```

The rules the linter holds are the ones that make a flow behave the same way
twice. The two that catch the most: the planner's `status` and `assignee` are
written in one place, by the flow itself (a flow declares its hand-offs in a
top-level `planner.on_block` map, and the block's post hook
`logic/planner_effect.py` writes them, rather than a model being told to PATCH
the task), and routing must be total, because an undeclared verdict that falls
through to `depends_on` scheduling is a flow doing something nobody wrote down.

## Who moves the task

The Runner launches a flow and steps it. It does not write the task's status or
its assignee: that is the flow's logic and lives in the flow.

- **The start.** The first block takes the task (`open` to `in_progress`).
- **Each hand-off.** `planner.on_block` says, per block and verdict, who holds the
  task and in what status. `logic/planner_effect.py`, the block's `post` hook,
  writes it. It reads the task first and writes only what differs, so a block
  that runs again writes nothing.
- **The end.** The last block declares it. A content or development flow ends by
  handing the task to a person in `pending_review`; the person decides whether
  it is done.
- **A failure.** A top-level `on_error: logic/on_error.py` moves the task to
  `blocked` with the error as the reason and hands it to the person the flow
  names for its stopped end.

A flow that declares hand-offs and carries no `post` hook runs and never moves
its task. The linter refuses it (S12).

A delegated block names its agent in the work order as a mention (`@name`). The
planner tells a mentioned actor about the comment, so the order reaches its
agent even when the task is already with that agent.

## Time budgets

A flow that declares `schema_version: "3.4.0"` may set `time_budget` on a block
and on the run (`90s`, `30m`, `2h`, `1d`). A block that waits for an agent that
never answers runs out of its budget, the flow's `on_error` runs, and the task
ends in front of a person with the reason. The clock is checked once a minute by
`smart-node-flow-sweep.timer`; each check is in the event log
(`flow.sweep_started`, `flow.sweep_finished`, `flow.time_budget_exceeded`,
`flow.sweep_failed`). A flow without a budget waits for as long as it is left to.

A flow with no tests is reported by name. That is not an error; it is a process
nobody has walked.

## The shipped flows

Each is a complete application: README, CHANGELOG, a version, tests that walk
it without a model. They are what a new flow is copied from.

| Flow | What it is |
|------|------------|
| `task-simple` | The smallest pipeline: execute, an independent verification that can send the work back, delivery. The work is done by the engine's own specialists. |
| `task-development` | SAMPLE. A development pipeline run by the node's real agents: the project manager plans, the owner approves the plan, the developer executes, the tester verifies, the project manager delivers. Its assignees are placeholder words; copy it and write your agents' names in. |
| `content-simple` | SAMPLE. One material, one approval, one publication, run by the node's real agents: the writer writes the material, the owner approves it or sends it back, the publisher publishes it once. Its assignees are placeholder words; copy it and write your agents' names in. |

## A node's own flows

A node's own flow is written by its author in the author's own directory and
published through one door, `POST /api/workflows/import` with the flow's name
and that directory. The door checks the directory with `flow-lint.py` and the
flow's contract (README, CHANGELOG, `metadata.version`, tests present, not a
sample), and on green copies it to

```
/var/lib/smart-node/workflows/flows/<name>/
```

and registers the copy. On red it says which check refused, and nothing is
copied or registered. The Runner runs the published copy, so what runs is what
was checked, and the author changes a flow by publishing it again. That tree is
in the node's state, not under `/opt`: the installer creates it and never writes
inside it, so an update does not touch a flow the node made, and it travels with
the node's backup. The door does not run the flow's own tests, which are the
author's scripts; the author runs them before publishing.

A shipped flow is replaced whole on every update. To use or change one, copy it
under another name and edit the copy; the copy is yours and an update does not
touch it. A flow names real agents: there is no list of roles or participants
between a flow and the node's agents. `flow-lint.py` checks every name against
the node's agents (rule S10), so run the tests on the node the flow is for. A
sample (`metadata.sample: true`) carries placeholder names, is exempt from that
check, and is not registered.

## Routing order

A block's `routing:` dict maps signal names to next-block names. When a block
emits multiple VERDICT lines and more than one of those verdicts matches a
routing key, the engine uses the **first declared routing key** as the winner.

```yaml
routing:
  revise: fix    # this wins if both 'revise' and 'approve' are in the signals
  approve: done
  _default: done
```

`_default` is always checked last, regardless of where it appears in the dict.

This is a deterministic guarantee as of the engine iterates routing keys
in YAML declaration order (Python dict insertion order, preserved since 3.7),
not in signal set order. Flow authors control priority by placing the
higher-priority key first.

In practice, well-formed blocks emit exactly one VERDICT. The declaration-order
rule matters only when a legacy alias maps the same verdict to a second signal
(e.g. `VERDICT: REVISE` produces both `revise` and `revise` via the alias,
which is the same key, so there is no collision). If you author a block that
can produce two distinct routed signals intentionally, declare the higher-
priority one first.

## Two things that will bite

A flow can name an **agent** (`agent: <name>` on a block), and Rein resolves that
to `{agents_dir}/<name>/agent.yaml`. Nothing under this tree ships an agent, so a
flow that names one needs a link to that agent's home under `runner-agents/`.
The install does not guess which agents exist.

The **teams** file gates which specialists a flow may use. Adding
`specialists/foo.md` is not enough on its own; the team the flow declares has to
list `foo` as well.
