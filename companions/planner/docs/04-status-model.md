# Status model

Since migration 0010 a task has three statuses and two dictionaries.

## The columns

| column | values | meaning |
|---|---|---|
| `status` | `queued`, `in_progress`, `closed` | whether the task moves |
| `wait_reason` | a row of `wait_reasons`, or NULL | why a queued task is not being worked on; NULL means it waits for someone to pick it up |
| `close_outcome` | a row of `close_outcomes` | how a closed task ended; required when closed, cleared otherwise |
| `stage` | one word | the legacy name of the situation, derived by trigger through the dictionaries; read it if you only know the old names, never write it by hand |

`status` is a CHECK and will not grow. Everything a node may need to add is a
dictionary row.

## The dictionaries

`wait_reasons` and `close_outcomes` are tables, edited through
`/api/v1/dictionaries/...` by the owner. Each row has a code (the identity,
never changes), a label and description for people, `legacy_stage` (the
one-word name a task with this row shows as to old readers), `legacy_default`
(which row an old writer gets when it sends that one-word name), `effects`
(declared side effects, visible in the panel) and `enabled`.

Seeded rows:

| wait_reasons | shows as | effects |
|---|---|---|
| `approval` | `awaiting_approval` | notify assignee |
| `review` | `pending_review` | notify reviewer |
| `owner` | `escalated` | comment required |
| `person` | `blocked` | comment required |
| `dependency` | `blocked` | comment required |
| `schedule` | `blocked` | comment required |
| `error` | `blocked` | comment required |

| close_outcomes | shows as | effects |
|---|---|---|
| `completed` | `done` | success, an agent may set it |
| `cancelled` | `rejected` | a person's call |
| `rejected` | `rejected` | a person's call |
| `not_possible` | `rejected` | a person's call |
| `duplicate` | `rejected` | an agent may set it |

Known effect keys: `notify` (`assignee`, `reviewer`, `owner`, `none`),
`comment_required`, `agent_may_set`, `success`. Unknown keys are kept and
ignored.

A row in use cannot be deleted; disable it instead, so history keeps meaning
what it meant.

## Two vocabularies on the wire

A writer may send either:

```
PATCH /api/v1/tasks/42  {"status": "done", "comment": "..."}
PATCH /api/v1/tasks/42  {"status": "closed", "close_outcome": "completed", "comment": "..."}
PATCH /api/v1/tasks/42  {"status": "queued", "wait_reason": "person", "comment": "..."}
```

Every task answer carries `status`, `stage`, `state`, `wait_reason` and
`close_outcome`. A request chooses the words it wants back: the header
`X-Status-Vocabulary: new` (or `?vocabulary=new`) makes `status` the
three-valued status in that response, `legacy` the old stage name, nothing the
node default (`PLANNER_STATUS_COMPAT`, `legacy` unless set). So a consumer
moves to the new names when it is ready and no other reader is touched.
`stage` and `state` are always what they say.

A Smart Node sets `PLANNER_STATUS_COMPAT=new` (companions/tracking.sh writes
it into tracking.env): every reader on the node, the agents first, sees the
three statuses. The old names stay accepted on input, `stage` still carries
them, and a reader that has not moved asks for `X-Status-Vocabulary: legacy`.

The objective progress answer (`/objectives/{id}/progress`) counts the subtree
both ways: `tasks_by_status` by stage, as before, and `tasks_by_state`,
`queued_by_reason` and `closed_by_outcome` in the three statuses.

`assessing` and `incident` are accepted on input and mean `in_progress`.

## No matrix

Any stage may follow any other. The rules that remain are guards, and each
says why it refuses: a close carries a report (unless the outcome's row says
otherwise), only the reviewer closes a reviewed task, a decision answers only
a task that waits for one, a claim answers only an open task, an incident is
reported on delivered work. A task under way may close with any outcome in one
move.

## For flows

A flow's `planner.on_block` effects may keep the old words (`status: done`,
`status: blocked`) and work unchanged, or say what they mean:

| old | new |
|---|---|
| `{status: done}` | `{status: closed, close_outcome: completed}` |
| `{status: blocked}` | `{status: queued, wait_reason: person}` (or `error`) |
| `{status: rejected}` | `{status: closed, close_outcome: cancelled}` |
| `{status: in_progress}` | unchanged |

The flow's logic decides verdicts; only this table changes.
