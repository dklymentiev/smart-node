# task-simple changelog

## 2.1.0 — 2026-10-19 — the planner's three statuses
- The planner effects say what they mean: a finished run goes to review as
  `status: queued, wait_reason: review`, not the one word `pending_review`.
- A failed block stops the task as `status: queued, wait_reason: error` with the
  reason, not `blocked`; a task already closed or stopped for a person, a
  dependency, a date or an error is left alone, as before.
- The planner still accepts the old words; the flow no longer uses them.

## 2.0.0 — 2026-10-02 — ends as branches, no task writes, first version
- The blocks write to the planner with the Runner's planner key, as task-complex
  does. Since release 1.12.48 the core-api socket refuses planner writes from a
  process that is not an agent (403), and the blocks swallowed the error: every
  comment was lost. Found on the live check (tasks 236, 237).
- `metadata.version` added. 1.0.0 below is the release flow as it shipped.
- The post hook `logic/post.py` is gone: it set status `pending_review` and the
  reviewer itself, and the runner then closed the task as `done` anyway. Only the
  engine writes the task now (flow determinism). Incompatible, hence 2.0.0.
- A failure or an unknown verification verdict ends at a new `stopped` block
  with one comment, instead of routing to `_stop`, which let delivery still run.
- `logic/branch.py` in front of verify, deliver, done and stopped: this node's
  Rein forgets skipped branches between steps.
- Tests: every edge, every end on the real engine, the branch table.

## 1.0.0 — 2026-09-30 — README, CHANGELOG and tests on the node
Release 1.12.48. The contract written down as it stood; tests reported the post
hook writing status and the missing version. Behaviour unchanged since 2026-08-28
(Option A: planner-grounded blocks, `fail` routes to stop, tester approval
required for delivery).
