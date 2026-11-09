# tracking -- the planner MCP server for agents

This directory holds `mcp_planner.py`, the stdio MCP server through which
agents use the node's task planner. The planner application itself (the
FastAPI service, its CLI and migrations) is the pinned snapshot in
`companions/planner/`; this directory is the agent-facing piece the tracking
companion adds next to it.

## How it fits the node

- Installed by `companions/tracking.sh` (manifest `tracking.manifest.json`,
  title "Tasks", sidecars `planner tracking _shared`). The script copies
  `mcp_planner.py` to `/opt/smart-node/tracking/mcp_planner.py` and registers
  it with core-api as the stdio MCP server `planner`, run by
  `/opt/smart-node/dispatcher/venv/bin/python`.
- An agent gets the tools by being granted `planner`, which renders the
  server into that agent's `.mcp.json`. The CLI runtime spawns one process per
  agent session, as that agent's OS user.
- The server does not talk to the planner service directly. Every call goes to
  core-api's planner proxy (`/api/planner/...`) over the unix socket
  `/run/smart-node/api/core-api.sock` (`SN_CORE_API_SOCKET`), where the kernel
  proves which agent is calling. The agent's identity is never a tool
  parameter; it comes from `SN_AGENT_SLUG` or the working directory name.
- `SN_CORE_API_URL` exists for a test bed without the socket only; it is not a
  fallback.

## Tools

Tasks: `planner_my_tasks`, `planner_next_task`, `planner_list`,
`planner_task`, `planner_file`, `planner_advance`, `planner_recur`,
`planner_search`.

Comments: `planner_comments` (bounded index of a task's log, one comment by
id), `planner_comment`.

Tags: `planner_tag`, `planner_tags`, `planner_untag`.

Objectives: `planner_objective_create`, `planner_objective_attach`,
`planner_objective_detach`, `planner_objective_get`,
`planner_objective_close`.

The docstrings in `mcp_planner.py` are what the model sees; read them for the
exact behaviour. The usage text agents read about these tools is written by
`companions/tracking.sh`, and `tests/mcp-tools-in-contract.test.sh` checks that
every tool here is named there.

## Tests

`tests/` is a pytest suite for the pure helpers and the tool logic against a
stubbed core-api call (sorting, review-queue filtering, comment-log caps). It
needs a Python with `pytest`, `httpx` and the `mcp` SDK:

```
python3 -m pytest companions/tracking/tests -q
```

Repository-level checks that read this file include
`tests/mcp-socket-first.test.sh` and `tests/planner-review-queue.test.sh`.
