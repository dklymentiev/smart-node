# What the machinery is

One entry per component: what it is and its single job. Read this before
debugging, so you argue about the thing rather than about its name. Several
components have two names in common use; where that is true it is said plainly,
because most confusion here is two words for one thing.

## The path of one request

A message arrives and lands on the **bus**. The **dispatcher** notices it, decides
which agent it belongs to, composes that agent's context, and calls the
**gateway**. The model runs, the agent acts through planner, memory, documents and
tools under its rules, and the result is written back to the bus. Multi-step
processes run through the **orchestrator**.

## Intake

**Channel.** A way to reach the node: email, chat, voice, telegram, internal,
system. Each external channel has an adapter.

**Mail is external.** The node does not run a mail server. It reads mailboxes on
a server it does not own, over IMAP, and sends through that server's SMTP,
authenticated as the mailbox that was written to. That server holds the reverse
DNS, the certificate and the signing keys, which is why sending from anywhere
else fails or lands in spam. A node that appears to host mail is misconfigured.

## Storage and exchange

**Bus, also called the message registry.** One thing, two words. The append-only
log of every message: email, chat, agent-to-agent, system doorbells. `sn_core.messages`,
with `message_state` tracking new, routed, processed. "Bus" because work flows
through it, "registry" because it is stored.

**Events.** The immutable journal of what happened: `sn_core.events`. The bus is
the work queue; events are the history. This is the first place to look when you
need to know what actually happened rather than what should have.

Start by asking what kinds of event this node has ever recorded, so you are
reading the journal it has instead of the one you expect:

    SELECT event_type, count(*) FROM sn_core.events GROUP BY 1 ORDER BY 2 DESC;

Then read the window you care about, newest last:

    SELECT id, ts, event_type, entity_id, payload
    FROM sn_core.events WHERE ts > now() - interval '15 minutes' ORDER BY id;

**Read that census before you trust an absence.** The journal is incomplete
today: cron runs, tool calls, grants, agent creation and `agent.blocked` are
recorded, but the message path writes nothing at all -- no `message.inbound`, no
`agent.woken`, no `message.sent`. So an empty result for a message question means
"this node does not record that", NOT "it did not happen". Confirm against
`sn_core.messages` and `message_state`, which do carry every message and its
state. When the missing events land this paragraph goes away; until then,
believing the silence will send you looking for a fault that is not there.

Two things the journal does answer well, both of which look like nothing from
outside: `agent.blocked` says an agent was woken and could not run and why, and a
`cron.completed` with a near-zero duration says a wake did nothing rather than
failing. An agent that appears idle is usually one of those two.

**Core API and its database.** The REST API on `127.0.0.1:8030` over the
configuration and registry database. `sn_config.*` holds configuration (agents,
channels, policies, tools, integrations); `sn_core.*` holds the domain (messages,
events, actors, runtime); the identity and login layer is separate again.

## The brain

**Dispatcher, also called brin.** One process on `:8101`. Its logger says "brin",
diagrams say "Dispatcher": the same thing. It does three jobs, which is why it
looks like three services:

1. notice new work: the watchers below;
2. decide who runs: resolve which agent owns the message;
3. compose context and call the gateway: the executor.

**Message watcher.** A loop inside the dispatcher that watches the bus for new
messages and resolves whose they are. For email it resolves the owner of the
mailbox through the channel bindings; for internal and system messages, the named
recipient. It is a loop, not a separate service. It calls the core API as a
configured actor: if that actor names an agent this node does not have, every poll
is refused and inbound mail silently stops.

**Task watcher.** A loop inside the dispatcher that watches the planner for newly
assigned tasks and wakes the assignee. Also a loop, not a service.

**Executor.** The part of the dispatcher that assembles the prompt (role, tool
contracts, rules, memory) and calls the gateway.

## Execution

**Gateway.** Runs the actual model session for an agent on `:8000`. The dispatcher
prepares the turn; the gateway executes it. A bare model name runs a CLI session
with a shell; a slugged provider name runs a plain chat completion with no shell.
The gateway holds provider credentials; agents never do.

**Agent.** A worker: persona, rules, access, memory, model, identity. The thing
that does the work. Its files live in `/srv/agents/<slug>/`.

**Orchestrator.** Runs multi-step flows with approval gates, for work that is a
defined process rather than a single answer.

## Where things live on disk

| Path | Holds |
|---|---|
| `/opt/smart-node/<component>` | component code and its runtime config |
| `/etc/smart-node` | node configuration and secrets |
| `/var/lib/smart-node` | runtime state |
| `/srv/agents/<slug>` | one agent's instructions, grants, logs, state |
| `/srv/tools` | the tools agents call |
| `/var/log/smart-node` | logs, including one tree agents write to |

## How components arrive

Not every component is the same kind of thing, and this decides how it is updated.

- **Git working copy.** A real clone: edit in place, commit, push. Updated by pull.
- **Vendored snapshot.** A copy with no git history, replaced by re-deploying.
  Editing it in place loses the edit at the next deploy.
- **Container image.** No source on the node at all; updated by a new image.
- **System package.** Installed by the operating system's package manager.

Knowing which one you are looking at prevents the most expensive mistake here:
editing a file that the next deploy overwrites, and believing the change is
persistent.
