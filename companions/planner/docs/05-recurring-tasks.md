# Recurring tasks

Since migration 0011 a task may come back on a schedule. One task row carries
the whole series; each cycle is closed the ordinary way, with a report, and
those reports are the record of the series.

## The columns

| column | meaning |
|---|---|
| `recur_interval` | how often: Postgres interval text (`1 day`, `2 weeks`, `1 month`); NULL for a one-off task |
| `next_run` | when the next cycle opens; set and cleared with the interval |

## Writing

```
POST  /api/v1/tasks      {"title": "...", "assignee": "sam", "recur_interval": "daily"}
POST  /api/v1/tasks      {"title": "...", "recur_interval": "1 week", "next_run": "2026-10-26T07:00:00-05:00"}
PATCH /api/v1/tasks/42   {"recur_interval": "2 weeks"}
PATCH /api/v1/tasks/42   {"next_run": "2026-11-01T07:00:00-05:00"}
PATCH /api/v1/tasks/42   {"recur_interval": "off"}
```

`daily`, `weekly`, `monthly` and `yearly` are accepted as words. Without
`next_run` the first cycle opens one interval from now; a new interval on a
task that already recurs keeps its scheduled `next_run`. `off` (or null) ends
the series and clears both columns. `next_run` alone is refused on a task that
does not recur. The same fields go through the MCP tools (`task_create`,
`task_update`) and the CLI (`planner task create --recur daily --next-run ...`,
`planner task update ID -f recur_interval=off`).

## The sweep

`Pipeline.recurrence_sweep()` takes every task whose `next_run` has passed:

| the task is | the sweep |
|---|---|
| closed with a success outcome (`completed`) | reopens it to `in_progress`, keeps `approved_at`, clears `completed_at`, comments "Recurring cycle ... opened", wakes the assignee |
| closed with any other outcome (`cancelled`, `rejected` ...) | ends the series: both columns cleared, with a comment. A person stopped it |
| still open from the previous cycle | comments that the cycle came due while the previous one is open, wakes the assignee (event `recurring_due`) |
| waiting for the owner's approval | wakes nobody; the cycle is skipped |

Then `next_run` moves forward by whole intervals until it is in the future:
a node that was down for a week opens one cycle, not seven, and the series
stays on its grid (07:00 stays 07:00). Rows are locked `FOR UPDATE SKIP
LOCKED`, so two sweeps never open the same cycle. Wakes go through the
planner's ordinary notify path after the transaction commits.

The owner approves a series once: a task filed by an agent waits for approval
like any other, and its cycles are skipped until it is approved.

It runs from cron every five minutes (`planner maintenance recur`, installed
by `companions/tracking.sh` in `/etc/cron.d/smart-node-planner-sweep`) and on
demand at `POST /api/v1/maintenance/recurrence-sweep`.
