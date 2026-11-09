# hitl -- Smart Node adapter for the HITL decision service

HITL ("human in the loop") lets an agent ask a person for a signed decision
before it acts outside the node. The decision server itself lives in
`companions/hitl-upstream/`; this directory is the node-side glue around it:
how agents reach it, how answers get back to Planner, and how an older Docker
deployment is adopted.

Installed by `companions/hitl.sh` (manifest `hitl.manifest.json`, title
"Approvals", sidecars `hitl-upstream hitl _shared`). It is opt-in: the
orchestrator runs it only with `--hitl-origin https://...`, because the
passkey (WebAuthn) origin needs working DNS and TLS. Once installed it stays in
the update path.

## How it fits the node

```
agent --stdio--> mcp_hitl.py --unix socket--> bridge.py --> caller/hitl.py --> HITL server
                                                  |
                                       hitl-queue/ (staged answers)
                                                  |
                                             deliver.py --core-api socket--> Planner task
```

- The agent never sends its own name. `bridge.py` identifies it from the
  socket peer uid and holds the caller signing key and pinned recipient keys,
  which agent processes cannot read.
- A verified answer is consumed once and staged; `deliver.py` records it on
  the Planner task and hands the task back to the asking agent, retrying
  until it succeeds.

## Files

| File | Role |
|---|---|
| `mcp_hitl.py` | stdio MCP server, registered as `hitl`. Tools: `hitl_recipients`, `ask_human`, `check_human` |
| `bridge.py` | Private bridge on `/run/smart-node/hitl/agent.sock` (unit `smart-node-hitl-bridge`) |
| `deliver.py` | Answer delivery to Planner via core-api (unit `smart-node-hitl-delivery`, own user) |
| `recipient_bindings.py` | Operator CLI: bind a pinned recipient to a Planner human actor, `recipient_bindings.py <recipient> <human_actor>` |
| `migration_preflight.py`, `migration_snapshot.py`, `legacy_route.py` | Read-only audit, verified backup and Traefik upstream rewrite used when adopting a legacy Docker HITL (`--hitl-adopt-legacy`) and by `hitl.sh --rollback-legacy` |
| `flow_poller.py`, `flow_poller.sh`, `install-poller.py` | Optional poller that wakes parked Content Article workflow runs once HITL verifies an answer; installed only when that workflow is present |

`hitl.sh` also writes `/etc/smart-node/hitl.env`, the server unit
`smart-node-hitl` on port 8792, and a Traefik route for the origin host.
`hitl.sh --uninstall` refuses: the state holds signed decisions.

## Tests

- `tests/test_*.py` -- unittest, no live node needed:
  `python3 -m unittest discover -s companions/hitl/tests`.
  `tests/hitl-integration.test.sh` at the repository root runs them plus
  source checks on `hitl.sh`.
- `tests/signed-answer-e2e.mjs` -- end-to-end run against an installed node
  (`sudo node signed-answer-e2e.mjs --url https://hitl.example.com`); use a
  disposable node only.
