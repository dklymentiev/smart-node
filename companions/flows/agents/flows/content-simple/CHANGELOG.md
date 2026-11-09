# content-simple changelog

## 1.1.0 — 2026-10-19 — the planner's three statuses
- The planner effects say what they mean: a finished run goes to review as
  `status: queued, wait_reason: review`, not the one word `pending_review`.
- A failed block stops the task as `status: queued, wait_reason: error` with the
  reason, not `blocked`; a task already closed or stopped for a person, a
  dependency, a date or an error is left alone, as before.
- The planner still accepts the old words; the flow no longer uses them.

## 1.0.0 — 2026-10-03 — first version
- One material, one approval, one publication, run by real agents: the writer
  writes the material, the owner approves it or sends it back, the publisher
  publishes it once.
- Shipped as a sample: the assignees are the placeholder words `writer`,
  `publisher` and `owner`. A node's own copy carries the names of its agents,
  and the linter checks each of them (rule S10). The sample is marked
  `metadata.sample: true` and is not registered.
- Only the engine writes the task's status and assignee (`planner.on_block`).
  Every hand-off is a work order comment answered with a `VERDICT:` line.
- Nothing is published without the owner's approval: publish acts only on gate
  `approved`.
- Three ends: published, rejected by the owner, stopped on a failure.
- Tests without a model: every edge, every end on the real engine, the branch
  table, the hand-offs, the orders.
