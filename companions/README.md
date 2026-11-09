# Smart Node companions (Phase 2)

Phase 1 (`bootstrap/`, stages B01-B06) brings a node to a working sign-in
screen. Phase 2 installs the **companions**: the services that make the node
useful. `orchestrator/install-on.sh` runs both; `--components A,B` limits Phase 2
to the named modules, what they require and the core.

Companions are **named, not numbered**. They have no fixed order beyond what
each one declares it requires.

## Layout

```
companions/
  <name>.sh               installer for one companion (the stage contract below)
  <name>.manifest.json    what it is, what it requires, whether it is core,
                          and its sidecars: the directories shipped beside the script
  <dir>/                  the code a companion installs (a sidecar)
  _shared/                shell helpers several installers source
```

Where a companion wraps a component that also lives in its own repository, the
directory is a pinned snapshot and its `VENDORED` file records which one. See
[NOTICE.md](../NOTICE.md) for where each one comes from and its licence.

## Script contract

```
./<name>.sh              full run, idempotent
./<name>.sh --verify     post-check; exit 0, or the number of failures
./<name>.sh --status     read /var/lib/smart-node/stages/<name>.state
./<name>.sh --uninstall  reverse the install
./<name>.sh --help       header documentation
```

State lives in `/var/lib/smart-node/stages/<name>.state`; the registry record
in `/etc/smart-node/companions.d/<name>.json`.

## Companions

From `*.manifest.json`. Core companions are always installed.

| Companion | Core | Requires | Ships | What it is |
|---|---|---|---|---|
| admin-runner   | yes | | `admin-runner/` | runs the node's privileged operations for the panel |
| core-api       | yes | | `core-api/`, `srv-skeleton/`, `node-ops/`, `tools/` | the node's controller: agents, grants, people, messages |
| gateway        | yes | | `gateway/`, `gateway-ops/` | the one door to the AI models |
| dispatcher     | yes | gateway | `brin/` | wakes the agent when a message arrives |
| flint          | yes | | | an agent runtime for any model in the OpenAI format |
| studio         | yes | core-api, dispatcher | `studio/` | the web app: chats with agents, team, settings |
| agent-runtimes | | gateway | | the Claude Code agent runtime |
| communication  | | dispatcher | `communication/` | email, Telegram and other channels for agents |
| documents      | | core-api | `doci/` | documents for people and agents |
| hitl           | | | `hitl/`, `hitl-upstream/` | asks a person before an agent acts outside |
| memory         | | | `mesh/` | long-term memory for agents |
| tracking       | | | `planner/`, `tracking/` | the task planner |
| voice          | | | `voice/` | talk to agents by voice |
| workflows      | | | `rein/`, `flows/` | scheduled and multi-step flows |