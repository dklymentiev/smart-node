# Flint runtime for Smart Node (1.12.50)

Flint is an optional `SessionBackend` for v2 sessions. An agent selects it with
`flint-cli/<provider-model-id>` in the model catalog. Claude sessions and the v1
Claude-only API keep their existing paths. The CLI process cap, idle reaper,
interrupt, desk activity, usage and restart paths are shared with Claude.

## Install and configure

1. Install Node.js 22.12 or newer and Flint 1.16.0 or newer (`flint.sh` does
   both). The gateway reads `flint --version` and refuses an older release
   before passing it a provider key. Install its `flint` executable before
   running `gateway.sh`, so the systemd unit records the absolute `FLINT_PATH`.
   Each Flint agent gets `--tools`: a base set of file, shell and web tools plus
   the MCP servers in its `.mcp.json`, and reads its identity from `FLINT.md`.
2. Run the Smart Node schema seed. It adds the `flint-cli` CLI provider with
   `FLINT_PROVIDER=openrouter` and `OPENROUTER_API_KEY` referencing
   `gateway/flint-key`. The seed adds no Flint model or agent.
3. Have the owner store the Flint OpenRouter key through
   `POST /api/gateway/cli/flint-cli/credential` (body `{"value":"..."}`). The
   key stays in the gateway: Flint gets a token of the gateway in its place
   (see "Model calls pass the gateway" below). Do not project it to
   `agent-runtime.env`; the projection route rejects Flint.
4. Add an enabled model through `POST /api/models` with provider `flint-cli` and
   model equal to the provider's full model id. Assign `flint-cli/<model>` only to
   a dedicated canary agent. Keep its OS access scoped to its workspace.
5. Re-run `gateway.sh` with `SMART_NODE_FLINT_ENABLED=true`. The installer
   preserves this setting on later runs. `FLINT_SKIP_PERMISSIONS` defaults to
   false even though Claude uses `SKIP_PERMISSIONS=true`. Check
   `GET /api/v1/backends`: Flint must report `available` before the canary.

For unattended agents that already have the required Smart Node tool grants,
set `SMART_NODE_FLINT_SKIP_PERMISSIONS_USERS=agent-example-a,agent-example-b` when
running `gateway.sh`. This passes Flint's local approval prompts only for those
OS users; the global `FLINT_SKIP_PERMISSIONS` flag can remain false. The
installer persists the list across later runs. Agent OS access and Smart Node
tool grants still apply. Recreate existing Flint sessions after changing it:
their CLI processes keep the mode chosen at spawn.

The initial provider is OpenRouter. To use another Flint provider, the owner
changes `sn_config.cli_providers` through the `cli/flint-cli/env-vars` API to set
`FLINT_PROVIDER` and the corresponding API key `secret:` reference. The
wrapper accepts OpenRouter, OpenAI and Anthropic key names; the backend report
reads the configured reference rather than assuming `gateway/flint-key`.

An optional fallback model is set the same way: `FLINT_FALLBACK_MODEL` on the
provider row, a model id on the same provider and key. It applies to every
agent on `flint-cli`. The wrapper passes only the names it lists, and a name it
does not list stops every launch on the provider, so a Flint variable that is
new to the node has to be added to the wrapper before it is set on the row.

## Model calls pass the gateway

Flint calls the model through the door, as Claude Code does (`MODEL_RELAY`,
on by default). At spawn the runtime takes the provider key out of Flint's
environment and puts a gateway token (`model_tokens.py`) under the name Flint
reads its key from, with `FLINT_BASE_URL` set to the door:

| `FLINT_PROVIDER` | door | upstream |
|------------------|------|----------|
| `openrouter` | `/openai/v1` (`openai_relay.py`) | `https://openrouter.ai/api/v1` |
| `openai` | `/openai/v1` | `https://api.openai.com/v1` |
| `anthropic` | `/anthropic/v1` (`anthropic_relay.py`) | `api.anthropic.com` |

`FLINT_BASE_URL` on the provider row names another OpenAI-compatible
upstream: with `FLINT_PROVIDER=openai`, `OPENAI_API_KEY` its key and
`FLINT_BASE_URL=https://integrate.api.nvidia.com/v1`, Flint's calls go to
NVIDIA's API through the door. The runtime gives Flint the door's address in
its place; the row's address is where the door sends the calls on.

The door checks the token, puts the key from the provider row in its place
and records every chat call in `sn_core.model_calls`. `/openai/v1` serves
`POST chat/completions` and `GET models` (Flint reads prices and limits from
the list); any other path is 404, and a bearer the gateway did not issue is
refused with 401, so a provider key never leaves the node from an agent's
process. Another `FLINT_PROVIDER` does not start under the relay.

This needs a Flint build that reads `FLINT_BASE_URL` (flint-agent after
1.14.11). An older build ignores it and sends the token to the provider,
which refuses it: every turn fails, nothing is spent.

## Canary and rollback

During a running desk turn, `POST /api/v1/sessions/desk-steer` with `desk_id`
and `text` writes a `control_request` to Flint's stdin. Its immediate status
means accepted or too late; `GET /api/v1/sessions/desk-activity` reports the
subsequent `delivered` or `too_late` status. Flint injects accepted text before
its next model call in the same turn. The activity response also carries the
full live `text` separately from the bounded tool `steps`; neither is the
stored final answer. Other runtimes answer `unsupported` to steer. HTTP model
tool loops need their own steering hook before they can use this API.

Before using a real key, run
`SN_TEST_FLINT_BIN=/usr/local/bin/flint python3 companions/gateway/tests/flint_stdio_stand.py`.
It drives two turns and a resumed third turn against a local fake model API,
checking role loading, usage and ephemeral credentials.

Use one agent and verify two turns in one session, a process restart with
`--resume`, MCP tool discovery, an interrupt during a command, desk activity,
usage and an error result. Inspect the agent's `~/.flint` for `keys.enc` (it
must not contain the host key), confirm no Flint HTTP listener, and confirm
that the agent's OS account cannot access another agent's files. Run the
filesystem and command containment checks for the reviewed Flint build before
allowing file or shell work beyond that canary. In particular, Flint's local
shell can access paths outside `AGENT_ALLOWED_PATHS`; the OS user and its
granted directories are the actual boundary.

To stop using Flint, return the canary agent to its former model and rerun
`gateway.sh` with `SMART_NODE_FLINT_ENABLED=false`. Existing Claude sessions
and models are unaffected.
