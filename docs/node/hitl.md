# HITL decisions on a Smart Node

Status: integration preview. Do not use it as the release approval path until
flow identity, signed outbound consume gate, existing-data migration, and
end-to-end node test are complete.

Install the optional companion with an HTTPS origin whose DNS and certificate
already reach this node:

```sh
orchestrator/install-on.sh --all ... --hitl-origin https://hitl.example.com
```

The origin is the passkey relying party. It is saved on the node and cannot be
changed after a caller key exists without a deliberate recipient key migration.
Later installer runs update an installed HITL companion even if the option is
omitted. The installer creates a native decision server, a private agent bridge,
and a delivery worker. It registers an `hitl` stdio MCP server in the tool
catalog. Grant both `hitl` and `planner` to agents that will ask people.

## Enrol a person

The installer generates and registers the node caller key. On the node, issue
an enrolment link for a recipient ID:

```sh
sudo -u smart-node-hitl env \
  HITL_DB=/var/lib/smart-node/hitl/hitl.sqlite \
  HITL_ORIGIN=https://hitl.example.com \
  /opt/smart-node/hitl/venv/bin/python \
  /opt/smart-node/hitl/server/bin/admin.py enrol-link owner "Owner"
```

Send the link privately. After the person enrols a passkey, compare the key
fingerprint shown on their device with the one printed by this command. Confirm
only when they match:

```sh
sudo -u smart-node-hitl env \
  HITL_STATE=/var/lib/smart-node/hitl/caller \
  HITL_URL=https://hitl.example.com \
  /opt/smart-node/hitl/venv/bin/python \
  /opt/smart-node/hitl/caller/hitl.py pin owner
```

Do not use `--yes` before comparing fingerprints. The bridge exposes only
recipients with pinned keys. Agent processes cannot read the caller key or
choose an identity: the Unix socket identifies their OS user.
Bind the pinned recipient to the same person's active actor in Planner.
This is an operator action; an agent cannot supply or change the actor ID:

```sh
sudo /opt/smart-node/hitl/venv/bin/python \
  /opt/smart-node/hitl/recipient_bindings.py owner admin
```

The binding is stored in `/etc/smart-node/hitl-recipient-actors.json` and
survives installer updates. Check the actor ID in Planner before binding it.
The delivery worker requires an active Planner actor and asks core-api whether
that ID belongs to an active human account rather than a configured agent.
Planner's historical actor `type` is not an identity source; on older nodes the
owner `admin` may have `type=agent`.

On a node adopted from the legacy Docker service, use
`HITL_STATE=/var/lib/smart-node/hitl-caller` for this command; the installer
keeps that caller identity and its prior signed requests.

## Task flow

`ask_human` takes an existing task assigned to the calling agent and a pinned
HITL recipient ID with an operator binding. It creates a signed request. The
delivery worker records the question in a `workflow:request` task comment and
transfers the task to the person without changing its status; the private link
is returned only after that handoff lands. When
the person answers, the worker verifies and consumes the answer once, records
its proof in a `workflow:answer` comment, returns the task to the asking agent,
and raises the agent's wake flag. Expiry follows the same path with an expired
receipt. Planner has no separate "waiting for" field for this exchange.

Inside a workflow, its own scripts perform the two assignee handoffs. Runner
does not decide who owns a task. A flow can use the signed decision service and
its own poller to resume after an answer.

A `workflow:answer` comment is evidence of what the person signed. It does not
override the node's separate session approval requirement or authorise an
outbound action on its own. A signed outbound consume gate is still required
before this component can be used as the release's approval path.

The server state, caller key, pins, and signed requests live under
`/var/lib/smart-node/hitl`. A separate `/var/lib/smart-node/hitl-queue` holds
delivery records and receipts. The delivery worker runs as
`smart-node-hitl-delivery`; it cannot read the server database or caller key.
Preserve both trees across updates and backup or migration. The generic
uninstall must not treat either as disposable code.

An existing Docker HITL deployment is detected before the installer changes
the node. Routine updates refuse it. For an explicit adoption, use the exact
existing passkey origin and add `--hitl-adopt-legacy` to `install-on.sh`. The
installer audits the SQLite schema and both legacy caller states, requires no
active unanswered requests, stops the old container, then saves the database
with its WAL, both caller trees, the old code and router under a private
`/var/backups/smart-node/hitl/adopt-*` directory. It checks both the raw copy
and an independent SQLite snapshot before starting the native services.
It also saves original file ownership so rollback can restore mixed legacy
owners while keeping files created after adoption. The old container's Docker
restart policy is disabled during native service and restored on rollback,
preventing both servers from starting after a reboot.

Before adopting a live deployment, compare a fresh read-only audit with the
intended backup scope: one database, both caller trees, and every answered
request not yet consumed. The migration does not consume old answers.

The old caller paths, `/opt/smart-node/hitl/app`, and the installed
`flow_poller.sh` and `flow_poller.py` remain available to flows
already using them. Their signed requests and unconsumed answers are not
rewritten or consumed by migration. The old Traefik router keeps its TLS and
VPN middleware; only its upstream changes. On an install failure the old
container is restarted. The new services are enabled for boot.

To roll back after a successful adoption, while the old container is still
stopped:

```sh
sudo /opt/smart-node/hitl/install.sh --rollback-legacy
```

Rollback stops the native units, restores the original router and starts the
old container against the **current** database and caller states. It keeps
answers that arrived after adoption. The private backup remains for recovery;
the next native adoption again needs the explicit installer flag.

Audit a legacy deployment without changing it:

```sh
sudo python3 companions/hitl/migration_preflight.py \
  --db /var/lib/smart-node/hitl/hitl.sqlite \
  --caller-state /var/lib/smart-node/hitl-caller \
  --caller-state /var/lib/smart-node/hitl-flow-caller
```

The report contains counts only, including answered requests not yet consumed.
It checks SQLite schema and integrity, registered caller keys, pins, local
request files, and consumed answers. A successful audit is a precondition for
migration, not authorization to replace the running service.
