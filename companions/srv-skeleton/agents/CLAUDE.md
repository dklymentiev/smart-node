# /srv/agents — agent convention

## What an agent is

An agent is one folder under `/srv/agents/`. Rule: **one agent = one folder**,
no shared/common agents. The folder is a projection of a row in
`sn_config.agents` (database = source of truth).

## How an agent is added

NOT by hand-copying the template. You create the agent through the Smart Node
panel; `core-api` then provisions `/srv/agents/<name>/` from `_template/` and
renders the data files from the database row.

## Per-agent layout

```
<name>/
├── CLAUDE.md     # GENERATED — the agent's own prompt (role, tools, limits) from sn_config
├── agent.yaml    # GENERATED — config from the sn_config.agents row
├── config.json   # GENERATED — runtime config (model, tools)
├── README.md     # purpose, dependencies
└── CHANGELOG.md  # append-only human log
```

No entry point: the Runner wakes the agent through brin into a gateway
session. The old `run.sh` was removed when the Runner became the sole waker.

`_template/` is the canonical skeleton; do not run it as an agent.
