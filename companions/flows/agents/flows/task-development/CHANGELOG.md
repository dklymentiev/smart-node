# task-development changelog

## 1.1.0 — 2026-10-19 — the planner's three statuses
- The planner effects say what they mean: a finished run goes to review as
  `status: queued, wait_reason: review`, not the one word `pending_review`.
- A failed block stops the task as `status: queued, wait_reason: error` with the
  reason, not `blocked`; a task already closed or stopped for a person, a
  dependency, a date or an error is left alone, as before.
- The planner still accepts the old words; the flow no longer uses them.

## 1.0.0 — 2026-10-03 — first version
- A development pipeline run by real agents: the project manager plans, the
  owner approves the plan, the developer executes, the tester verifies
  independently and can send the work back, the project manager delivers.
- Shipped as a sample: the assignees are the placeholder words `pm`,
  `developer`, `tester` and `owner`. A node's own copy carries the names of its
  agents, and the linter checks each of them (rule S10). The sample is marked
  `metadata.sample: true` and is not registered.
- Only the engine writes the task's status and assignee (`planner.on_block`).
  Every hand-off is a work order comment answered with a `VERDICT:` line.
- Every hand-off writes the status as well as the assignee, so a status an agent
  moved on its own is put back at the next step.
- Three ends: delivered, rejected by the owner, stopped on a failure.
- Tests without a model: every edge, every end on the real engine, random
  walks, the branch table, the hand-offs.
