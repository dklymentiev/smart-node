# Changelog

All notable changes to `smart-node-core` are documented here.
Format loosely follows Keep a Changelog; versions are date-stamped.

## [Unreleased]

### Changed
- Crons: the registry says whether a job is already in the node crontab. `GET /api/crons`,
  `GET /api/crons/{id}` and the answer to a write carry `materialized` (read from
  `/etc/cron.d/smart-node-agents`, null when unreadable). A write also carries a `note` that
  the reconcile timer installs the job within five minutes, and the refusal of
  `POST /api/crons/render` says the same, so an agent that registered a job does not take
  that refusal for a request to the owner.
- One secret store for the node: `sn_config.secrets` under pgcrypto with the
  node secret key (`lib/secret_store.py`), replacing the gateway's own table
  and the sops file. The gateway's provider keys keep their refs
  (`gateway/<provider>-key`, `gateway/claude-cli-token`) and move over by a
  schema migration; the sops file is imported once at the first start after
  the update and journalled as `secret.store.imported`, then left for the
  operator to remove. Earlier values are kept thirty days in
  `sn_config.secret_versions` (the snapshot routes read them). `/api/gateway/secrets`
  and the Google OAuth writer write to the same store; events are
  `secret.set` and `secret.deleted` for every writer.
- Every read of a value is journalled as `secret.read` with the actor and
  how (agent, operator, version), so the Secrets page can say who read a
  secret last.

### Added
- The owner's workflow controls, permission `workflows.write` (super-admin
  only): `PATCH /api/workflows/{name}` puts a flow on (`available`) or `off`,
  and `PUT /api/workflows/{name}/agents` sets the agents who may start it. Off,
  the run route refuses the flow (409). The agents whose prompt lists the flow
  are recomposed. An agent taken off a flow is kept in the new table
  `sn_config.agent_workflow_revokes`, and the registrar and the import door no
  longer grant the flow back to it from its blocks at the next update.
- `/api/node` (routes/node_settings.py) for the panel's Settings page:
  `GET /api/node/about` (system.status.read) gives the node's name, domain,
  release, commit, install time and whether it is paused; `GET /api/node/access`
  (access.read, super-admin only) gives each sign-in account with the role of
  the actor it maps to and the actors merged into it, and every role's holders
  and permissions. From the auth provider's account file only the name, role
  and creation date are read. Read only.
- `/api/memory` (routes/memory.py), the owner's read of the node memory (mesh)
  for the panel's Memory page: workspaces with counts and their agents, the
  document types, the newest documents of one workspace or all of them merged
  by date, search by meaning over the chosen workspaces, and one document in
  full. Every row says whose it is (mesh lists across workspaces as "*", so the
  "all" view asks each one). A handoff is named by its subject and its preview
  skips the bookkeeping lines. Permission `memory.read`, super-admin only: it
  reads every agent's memory. Read only.
- `GET /api/secrets/refs` carries `items`: namespace, where the ref is wired
  in (a CLI provider's env var, an API provider), which agents may read it,
  who set it and who read it last. `GET /api/secrets/requests`: refused agent
  reads of the last thirty days where the agent still lacks the ref.
- A desk keeps what its owner has read: `PATCH /api/desks/{id}` takes
  `read_upto`, a moment (the newest message the office showed, so an answer that
  lands meanwhile stays new), kept in `metadata.read_upto`; it never moves back
  and never past the server's clock. `GET /api/desks/unread` (desks.read) gives
  each of the caller's active desks the count of messages from others after it,
  from the same sources as /history and without what the thread never draws
  (NO_REPLY, the action-claim guard's scaffolding, a relay not yet delivered),
  and its newest message. New desks start read; a desk from before counts 0
  until it is first read.
- `GET /api/agents/processes`: the node's live agent processes from the
  runtime's process table, what each is doing and its last model call.
  `POST /api/agents/{slug}/stop` (permission `agents.stop`, super-admin and
  admin) disables the agent and then has the runtime end its processes; the
  result, including a gateway that did not answer, is journalled as
  `agent.stopped`. `PUT /api/agents/{slug} {"enabled": true}` is the way back.
- @mention of an agent in an office chat. POST /api/desks/{id}/mentions writes one
  internal message for the named agent (metadata kind chat, reason mentioned,
  origin the desk), the same primitive as a mention in a task comment. Only the
  desk owner may write it; the chat's own agent (409) and a name that is no active
  agent (404) are refused. The agent's inbox_reply is relayed into the chat as
  "Answer from <agent>" and the answer row, addressed to a person, is closed.
  GET /api/messages/{id}/chat-history serves the mentioned agent that one chat,
  paged from the end, and nothing else: no attachments, no other desk.
  GET /api/desks/{id}/waiting lists the colleagues a mention from the chat is
  still waiting on, with working, busy (on something taken earlier) or queued.
  direct=true sends to the named colleague only (a Reply to its message, or a
  message that starts with @colleague) and makes the row the person's turn in
  the chat; turn=false on the other rows of a message to several colleagues
  keeps it one turn.

### Fixed
- Limit definitions and profiles provide node defaults and agent overrides for spending, unattended wakes and turn length. The owner can edit them and pause the node in the panel; profile changes and limit trips are journalled.
- Roster changes recompose every active agent's Team section; text knowledge and
  attached document pointers reach the composed prompt. Grants with no catalogue
  row now return 400 and emit `grant.rejected`; secrets keeps its MCP grant and
  prompt contract, while catalogue row changes are audited. MCP registration and
  discovery publish a grantable umbrella row, and startup backfills existing
  servers. Archiving an agent disables its budgets with `budget.disabled` events.
- Channel binding refuses an enabled mailbox already received by another active
  agent, naming its holders. Concurrent bind requests serialize on the channel
  row. Duplicate-bound inbound messages are blocked with a routing-denied event,
  excluded from wake selection and hidden from agent reads and actions; the owner
  can inspect the message and the existing Panel problem signal. Resolution uses
  the channel and mailbox and never selects a receiver from a conflict. Existing
  bindings are retained; no schema change.
- Creating an agent provisions its system user synchronously through admin-runner
  before the registry transaction commits. Transport errors and per-agent failures
  abort creation and remove the new directory; partial directory rendering and
  later database errors also roll back without archiving failed agents. Cron
  rendering no longer creates system users. Creation audit events are emitted
  after the privileged call to avoid locking the runner's separate audit transaction.
  The provisioning call forwards the request actor and is subject to admin-runner's
  owner gate; `created_by` remains an audit label and does not grant privileges.

## [0.20.1] - 2026-09-26

Includes entries that shipped with node 1.12.46 without a version heading of their own.

### Fixed
- seed-tools no longer fails a fresh install on the planner rows: it skips them
  while the planner integration is not registered and logs it; the tracking stage
  reseeds once it has registered the integration (the merge review).
- `PUT /api/agents/<slug>/grants?merge=true` keeps grants that exist only in
  sn_config.agent_tools (seed-tools grants several there) instead of deleting
  them; the kept rows are written into grants.json too (the merge review).

### Fixed
- `POST /api/messages/send` refuses an agent writing into a desk it does not own,
  before any row is written; an answer to that desk's open question is re-addressed
  as the reply. The send_message tool description says so.

### Added
- Planner api rows for agents on an HTTP model: planner_task, planner_comments,
  planner_search, planner_comment, planner_advance, seeded against the planner
  integration; reads for every planner holder, writes only where the grant writes.
  PUT grants pairs them with `planner` the same way.
  Only for an agent whose runtime dispatches them, i.e. one on an HTTP model
  (the merge review review): a CLI agent reaches the same routes through the tracking MCP
  server, and granting it rows it cannot call made "who may move a task"
  unreadable on an all-CLI node. The pairing in PUT grants drops its own planner
  additions for a CLI agent; a row asked for explicitly stays.
  Each of the five schemas is closed (`additionalProperties: false`) and
  planner_advance describes itself as a status change and nothing else, which is
  what brin now enforces before the call.

### Fixed
- The planner proxy treats a call carried by a verified service for that same agent
  as proved, the rule auth.get_principal already applies.
  It stayed 'asserted', so every planner write from brin for an HTTP agent was
  refused as an asserted task mutation, and delegated reads were journalled as
  asserted. Approvals still require an owner session.
- The model catalog is writable: `POST /api/models`, `PATCH` and `DELETE
  /api/models/<provider>/<model>`, for the owner, the operator and agents whose
  capability is configurator. The provider must be one the gateway reports
  (`/api/v1/backends`); a model an agent runs on cannot be disabled or deleted.
  `GET /api/models/providers` shows each provider's status, the key it lacks, and
  its models and agents in one call. Events model.added/updated/removed.
- Agent documents in core-api: `GET/POST /api/agents/<slug>/documents`,
  `POST .../documents/upload?name=` (markitdown to markdown), `GET` and `DELETE
  .../documents/<guid>`. Owner attachments only (via:ui, source:upload,
  source:google-drive); the agent's own memory is neither listed, read nor deleted here.
  Every change recomposes CLAUDE.md and restarts the agent's open sessions.

### Fixed
- The prompt's "Documents you can read" listed every mesh doc tagged `source:*`, i.e. the
  agent's own notes and every context handoff (24 "Session handoff" lines for alex, up
  to 100 on others). Only owner attachments are listed now.
- Pinning a document recomposed CLAUDE.md but left open sessions on the old prompt; it
  now restarts them, like regenerate.

### Security
- Desk writes are intent, not delivery. Any agent
  could write into any office desk whose id it knew, and the desk showed the row the
  instant it was written. Now `POST /api/messages/{id}/deliver-desk` decides, in one
  transaction: the desk's own agent, `system` or a person -> processed (delivered);
  any other agent -> blocked + `message.routing_denied` (not_owner / no-desk). The desk
  history shows processed rows only.
- A colleague's answer reaches the person who asked: `/send` from an agent that holds
  an open question (with an origin) from the recipient is written as the reply
  (thread, in_reply_to, origin) and closes the question; an answer whose origin is the
  asker's own desk is relayed into it by the SYSTEM ("Answer from <agent>:",
  `message.relayed`) and is no longer handed to the asker as `to_relay`.
- The prompt tells an agent to answer a colleague with a reply (inbox_reply / comm.sh
  reply), never a new send, and never to write into another agent's desk.

### Fixed
- The installer provisions /srv/tools/host-utils with markitdown 0.1.8 (pdf, docx, xlsx,
  pptx), owned by core-api with no group write, and verify checks it. Every
  document upload in the office and through core-api answered 503 "extractor
  unavailable" because no step ever installed it.

### Fixed
- The owner-designated secret writers are recognised by capability
  (`SN_SECRETS_WRITER_CAPABILITIES`, default `sysadmin,configurator`), not by
  slug. The slug default `sysadmin,foreman` stopped matching anyone
  when those agents were renamed to max and john, so no agent could write a
  secret or a secret grant. `SN_SECRETS_AGENT_WRITERS` is no longer read;
  if it is still set, core-api logs a warning at startup that names
  `SN_SECRETS_WRITER_CAPABILITIES`.

### Added
- Shipped hot on example-node first, then carried here:
  `GET /api/system/crons` and a rewritten `GET /api/system/services` serve the
  host collector's output;
  `GET|POST /api/system/runner` and `/api/system/models` are the Overview
  switches (browser session only, executed by admin-runner with a confirm
  token); `/api/system/summary` reports node timers from the collector instead
  of the retired agent-cron registry, and returns `window.bucket_seconds`;
  `GET /api/events/histogram` and `/api/messages/histogram`; `GET
  /api/runtimes/live`; live status on `/api/integrations` (companion + units,
  MCP server, helper-tool count) and `/api/mcp/servers` (endpoint answers /
  command present); the AI summary reads gateway health and prompt-cache use;
  database rows come newest first with a total.

### Fixed
- `GET /api/tools` no longer fails as a whole on one row whose name breaks the
  model rule (22 MCP-discovered names with capitals made it 500); such a row is
  returned flagged `name_invalid`.

### Added
- The installer renders the service-MCP secret-store door from
  `SMART_NODE_SECRETS_OPERATOR_USERS`: the same declaration grants Unix-socket
  group membership and writes core-api's exact OS-user allow-list. Existing
  declarations survive an update, and undeclared services remain refused.
- `/srv/tools/attachments/attachment-read.py` materializes one attachment for
  the calling agent through the kernel-authenticated core-api socket. It uses a
  per-uid 0700 directory in gateway PrivateTmp and a MIME-derived suffix so
  Claude Read recognizes images and PDFs; the canonical prompt teaches every
  existing and future agent the same contract.
- A principal-gated avatar upload endpoint owns both the durable bytes under
  `/var/lib/smart-node/agents/avatars/custom` and the existing agent DB pointer;
  the core-api service is the only application process with write access.
- `POST /api/budgets/{id}/notify-exhaustion` serialises concurrent hard-limit
  hits, resolves the owner's enabled email channel, queues the complete alert,
  and sets `notified_at` in the same transaction after the outbox insert.
  It refuses without a real `SN_NOTIFY_SENDER`.

### Removed
- The per-agent `run.sh` is gone. It was the cron entry point. Although the Runner replaced per-agent waking, each new agent still received a copy and every update restored it.
  Run by hand, it spent a model call outside brin and the gateway: no
  session, no usage, no budget rule. New agents get no entry point; on update
  the installer moves every `run.sh` and `run.sh.pre-*.bak` to
  `/srv/agents/.archive/run-sh-retired-<ts>/` (kept, not executable) and deletes
  the disabled wake crons that pointed at it, one event per removal. The
  onboarding seed no longer offers `SN_ONBOARD_WAKE_ENABLED`. Agents wake only
  through the Runner and brin.

### Added
- `/api/desks/{id}/history` marks a reply cut short by Stop with
  `stopped: true`.
- A canonical bulk context-status read model reports the latest completed
  provider request against the effective handoff threshold for active sessions
  and owner-scoped Office desks.

### Fixed
- Desk uploads are stored as `<uuid>.<content-derived suffix>` instead of a bare
  UUID. The generic attachment API now refuses every status except `approved`
  and `processed`, matching the desk-specific door used by the browser; knowing
  an id cannot expose an unscanned `received` file.
- The `bash` and `fetch` builtins carry an input schema (`command`; `url`,
  `method`, `body`), rewritten by `seed-tools.py` on every start. With
  `{}` a model on an HTTP runtime had to guess the argument names.
- The models an agent may be given are now the enabled chat rows of
  `sn_config.models`, and nothing else states them. The CHECK
  `agents_model_check` enumerated 13 ids and had drifted from the catalog both
  ways: an enabled catalog model (`gemini-flash-latest`) no agent could be set
  to, and ids the catalog never had; adding a model meant an ALTER TABLE. It is
  replaced by the trigger `agents_model_in_catalog`, and the same list is gone
  from `schemas/agent.py`. A refused model is a 422 naming the catalog, not a
  500. Only a changed model is checked, so agents already on a model are left
  exactly as they are, and `none` (no LLM) stays accepted.
- `/api/desks/{id}/sessions` matched only the bare `desk:<id>` key and so
  returned nothing for every current desk; it now matches `<agent>:desk:<id>`
  too, like `/history`.

### Changed
- An agent's model is always `<runtime>/<model>`: `claude-cli/sonnet`,
  `openrouter/openai/gpt-5`. The catalog trigger checks the id against
  `provider || '/' || model` of an enabled chat row; the default is
  `claude-cli/sonnet`; the update rewrites every agent whose bare model names
  exactly one catalog row, leaving version and updated_at alone. `sn_config.models`
  gains `vendor` (NOT NULL), and `sn_core.ai_sessions.vendor` is stamped from the
  catalog by the database on every write, so spend per vendor is a GROUP BY.
  `GET /api/agents/{slug}` carries `runtime` and `runtime_kind` (`cli` when the
  runtime is an enabled row of `sn_config.cli_providers`, else `http`), and
  `GET /api/models` carries `id` and `vendor`. A bare model sent to create or
  update is still accepted for one release: it is rewritten to its catalog row and
  recorded as `agent.model_legacy_alias`.
- Context-threshold `config.changed` events now record old/new values, actor,
  scope, and reason for both global and per-agent writes.

## [0.19.13] - 2026-09-16

### Added
- Signed-in-owner-gated, audited APIs for the global context-handoff threshold
  and per-agent overrides.
- Signed-in-owner-gated merge API for `cli_providers.env_vars`; audit events
  contain key names only and never record environment values.
- AI session status now exposes context-reset lifecycle markers for the panel.

## [0.19.12] - 2026-09-16

### Security
- Secret reads now require both the `secrets` tool capability and an exact or
  dotted-namespace `secret_refs` grant for kernel-proved agent callers. Network
  location alone grants nothing; anonymous loopback reads are refused. Secret
  writes and deletes are limited to authenticated owner sessions, root socket
  callers, and the owner-designated `sysadmin`/`foreman` agents. Every refusal
  emits `secret.access_denied`, and successful operator mutations carry a real
  actor instead of `unknown-peer`.

## [0.19.8] - 2026-08-22

### Fixed
- notify no longer stores the message reference inside the email body.
  It is presentation, and it is now added per rendered part at send time, so the
  stored `sn_core.messages.content` carries text and nothing else.

## [0.19.7] - 2026-08-22

### Fixed
- `tool.succeeded` / `tool.failed` now carry `method`. It was known where the
  event is written and discarded there -- only `tool.invoked` had it -- so every
  consumer had to infer whether a call read or wrote from the op NAME. One field
  removes that guessing downstream.

## [0.19.6] - 2026-08-21

An agent can account for itself, notify stops inventing what it cannot reach, and
the host boundary is stated out loud.

### Added
- `POST /api/agents/me/wakes`: how often you were woken, by what, how many
  model sessions that became and what they cost. Granted to every enabled agent by
  seed. NO parameter names the agent -- the answer is about the caller, whom the
  kernel names over the unix socket; a slug would need a rule about who
  may ask after whom. `asserted` identities are refused: a name in a header is a
  claim, and an answer about spend must not be built on one. Wakes and sessions
  are counted separately, because a wake that never reached the model has no
  session and folding them would hide exactly that case.
- Policy `the-host-is-not-yours`, category safety, so it composes into
  every agent by default. An agent's grants read as though the host were in reach
  while the boundary was enforced only by OS permissions and stated nowhere; one
  agent spent a whole session discovering it by failing. One rule for all agents,
  not a description per agent: the boundary is identical for every one of them.

### Fixed
- notify no longer falls back to `assistant@<domain>` and
  `https://<domain>/tasks`, which are syntactically fine and functionally
  impossible on any node whose mail and console are not on that exact name. The
  sender was blocked at the egress gate minutes after the API answered
  `{"sent": ["email"]}`; the link simply did not resolve. The email channel now
  REFUSES with a named reason, carried back in a new `refused` list, and warns
  once at import. `sent: []` used to mean both "this user has no channels" and
  "this node cannot notify anyone".

## [0.19.5] - 2026-08-21

One message with a malformed `origin` took down an agent's entire inbox.

### Fixed
- `my_inbox` called `org.get("conversation")` on `metadata.origin` without
  checking its type. `origin` is written by several producers and at least one,
  an owner-console injection, writes a plain string; `org.get` then raised
  AttributeError and `/api/messages/inbox` answered 500.

  The blast radius was the defect: not one unreadable message but every message
  for that agent. Reported from a live node where it blocked all message-driven
  work -- agents woke, could not read their inbox, and everything stayed at
  `new`, including a message from the owner.

  Guarded on `isinstance`, and a non-conforming row is now logged rather than
  silently skipped: an origin that cannot be parsed means a relay that will never
  happen, which is no longer worth a 500 and still worth a line.

  It surfaced only when a real end-to-end task was driven through the agents. No
  gate that reads the repository and no clean install would have found it.

## [0.19.4] - 2026-08-16

Desk chat history stops coming back empty after a reload.

### Fixed
- `desk_history` matched the gateway session on `metadata.conversation =
  'desk:<id>'`, but brin now keys the session PER agent -- `'<agent>:desk:<id>'`
  (e.g. `foreman:desk:...`). So a page reload returned an EMPTY thread over a
  session that held every turn. It now matches the bare key, the agent-prefixed
  key, and the legacy `metadata.user`. Tests in `test_desk_history.py`
  (agent-prefixed, bare, unknown-desk-empty, tenant-scoped).

## [0.19.3] - 2026-08-16

Recall is balanced across channels, so a chatty one cannot hide the others.

### Fixed
- `messages_search` and `by-contact`: when no channel is named, return the recent
  N PER channel (window function), not N total. A voice session runs to hundreds
  of turns and was crowding email and telegram out of the person's view -- so
  "what about email" came back "we did not email" over an inbox full of it. Each
  channel is now represented. A named `channel` still gets a flat window on it.
- A floor on `limit`: the model sometimes passes `limit=1`; a recall of one
  message is never what was meant.

### Tests
- `test_identity_across_channels.py` gains coverage for `messages_search`:
  retrieval spans every channel (telegram/web/email/voice), the `channel` filter
  returns only that channel, a chatty channel (20 voice turns in the fixture) does
  NOT crowd email/telegram out of a no-channel recall, a `limit=1` is floored, and
  an unmapped identity is empty not a 500. Plus `record-voice-turns` idempotency
  (one row per `voice:<session>:<seq>`, pre-processed). 16 tests, green.

## [0.19.2] - 2026-08-16

Voice can be recorded turn by turn, idempotently.

### Added
- `POST /api/messages/record-voice-turns` -- write voice turns AS THEY HAPPEN,
  one row per turn, keyed `voice:<session>:<seq>`. The live per-turn write and
  the end-of-call reconciliation share that key, so whichever runs first wins and
  the other is a no-op (`WHERE NOT EXISTS`). Rows land pre-processed. This is what
  lets a call survive a crash mid-way, gives each turn its real timestamp, and
  makes the current call searchable as it accumulates -- instead of one batch at
  hangup that a crash lost whole.

## [0.19.1] - 2026-08-16

Cross-channel recall becomes a catalog tool any agent can call.

### Added
- `POST /api/messages/search` -- one person's conversation across every channel,
  with parameters: `identity` (a handle, an account email, or now a display
  name), `limit` ("look deeper"), `since`/`until` (a time window), `channel`
  (one channel only). The parameterised, catalog-callable form of by-contact.
- `seed-tools.py` registers `messages_search` as an `api` catalog tool over this
  endpoint (integration `core-api`, pointed at core-api's docker0 address) and
  grants it to every enabled agent, so the tool reaches the voice line and the
  desk chat alike from the DB catalogue rather than any hardcoded list.

### Fixed
- A bare date in `until` now means the WHOLE of that day (end-of-day), not
  midnight -- "until yesterday" was silently excluding yesterday's messages.
- `by-contact`/`search` resolve a display name as a last resort, so an agent
  that passes a person's name rather than a handle still finds them.

## [0.19.0] - 2026-08-16

One identity for a person across every channel -- read side and schema,
web chat into the registry.

Until now every channel wrote the same person under a different key: telegram a
chat id, email a mailbox, the web chat an account name, the voice UI its own
string. Nothing tied them together, so an agent on one channel could not recall
the same person from another -- and the web chat, the strongest identity the node
has, left no record in the registry at all. This ships the person as one record
and the recall as a query over that person, plus the schema the trust model needs.

### Added
- `GET /api/messages/by-contact` -- the person's recent conversation across
  EVERY channel, newest-first, resolved by contact. Takes a `contact_id`, or an
  `identity` (a channel handle, an account email, or a `users.handle`) which it
  resolves through the one handle map + accounts. This is the earlier change's read side:
  cross-channel history is a query over the contact, not over a channel string.
- `POST /api/messages/record-webchat` -- lands a desk/web-chat turn (inbound +
  outbound) in `sn_core.messages`, rows pre-settled to `processed` so nothing
  mistakes an already-answered turn for work awaiting delivery. The web
  chat was the one channel with no registry record; it has one now.
- Schema (baked in B02, reference migration `026_identity_across_channels.sql`):
  `sn_core.message_identity` (the arrival-time verdict: message -> contact +
  trust + evidence, append-only beside `messages`); `sn_config.users.contact_id`
  (an account is a contact who can log in); `contact_channels.verified_by /
  verified_at / last_seen_at` (the one handle map now records how a binding was
  proved).
- `tests/test_identity_across_channels.py` -- the read side spans channels,
  resolves a handle and an account email to the contact, is empty (not 500) for
  an unmapped identity; the write side lands a processed pair; the schema has the
  new table, columns and the `trust` CHECK. (The table's missing grant to the app
  role was caught here before it could bite the stamp path.)

### Notes
- Trust stamping at arrival (writing `message_identity` per channel: email from
  `Authentication-Results`, telegram linked/unknown, web/voice session) and the
  inbound guard reading `trust` remain outside this release.
  `user_channels` is retired into `contact_channels` in that stage too, once its
  readers are repointed.

## [0.18.0] - 2026-08-14

An agent's name stops being something it types.

core-api now listens on a unix socket as well as the TCP bind. On the socket the
kernel reports the caller's uid (SO_PEERCRED), the caller never sends it, and the
uid maps to an agent through `os_user`. `Remote-User` is not consulted there at
all -- not outranked, ignored -- because a header that can fill in when the
lookup finds nothing puts the forgeable path back underneath the unforgeable one.

### Added
- `proved` on the identity scale, between `asserted` and `session`. Session
  still outranks it, and deliberately: `proved` says which PROCESS is calling,
  `session` says a human authenticated, and an approval must not come from a
  process. Verified -- an agent over the socket asserting the owner's name is
  named `foreman` by the kernel AND still refused an approval.
- `serve.py`, so one process answers on both doors. The uvicorn CLI takes
  `--host/--port` or `--uds` and never both, and two processes would be two of
  everything for one service.
- The socket's directory is declared in `tmpfiles.d`, 2750 and setgid, owned by
  the agent group. NOT `RuntimeDirectory=`: systemd re-applies a unit's exec
  context before every command, so a directory handed to another group in an
  ExecStartPre is re-owned again immediately before ExecStart. Measured here --
  the chgrp exited clean and agents still could not reach the socket.

### Changed
- An identity that names a row in `sn_config.agents` is accepted ONLY where
  something established it: the socket, or a signed-in panel session. The same
  name asserted over TCP is refused with 403 and the attempt is ledgered as a
  rejection rather than vanishing into an unauditable error. Service identities
  (runner, voice, gateway, cron-runner) are not agent rows and are untouched.
  - Consequence worth stating: an agent named the same as a service would now
    collide with this rule. Nothing on the nodes in the field does.
- The agent wake (`srv-skeleton/agents/_template/run.sh`) calls core-api over the
  socket, falling back to TCP only where no socket exists -- an old node, not a
  second door on a new one.
- `core-api.sh` converges EXISTING agents' `run.sh` to the template. Agent
  directories were create-only: a June agent ran June's wake script forever, so
  without this the release would behave differently for two agents on one
  machine depending on when each was made. A differing `run.sh` is backed up
  rather than overwritten.

## [0.17.3] - 2026-08-11

Two things an agent could do that only prompt text was stopping, and the first
work area a project can have.

### Added
- **Identity strength**. `auth.py` now records HOW a caller's identity
  was established, not only what it claims to be. `session` = the reverse proxy
  authenticated a browser session and stamped its shared proof
  (`X-SN-Proxy-Auth`). `asserted` = a `Remote-User` from the trusted network
  with nothing proving it -- which is every agent on the node, because the
  source-network check answers "may this caller assert an identity", never "is
  this caller who they say they are". New `require_session` dependency.

  What this closes: an agent sending `Remote-User: admin` WAS the owner as far
  as the API was concerned. The only thing preventing it was a sentence in the
  agent's own instructions, and anything that edits the prompt is past that.

- **The approval gate**. Recording an approval -- a comment with
  `comment_type=approval`, recognised by what the call does rather than by
  which client made it -- now requires a proven session. Checked ABOVE the tool
  grant on purpose: whether an agent may approve as the owner must not depend
  on how its grants happen to be configured. Every planner event now carries
  `identity`, which is what makes an owner's approval and one recorded by
  something claiming to be the owner tell-apart-able afterwards; they were not
  (all 26 approvals on the reference node read `author=admin`).

  FAIL CLOSED: with no secret configured nothing reaches `session`, so
  approvals are refused for everyone including the owner and the 503 names the
  missing setting. An unset secret meaning "trust everyone" would leave the
  hole on exactly the nodes nobody updated.

- **Projects**. `routes/projects.py`: create a project, add
  members, reprovision, archive. A project is a registry row
  (`sn_config.projects`), a unix group and `/srv/projects/<slug>` at 2770
  setgid -- flat, never nested under an owner, because a path expresses exactly
  one hierarchy and a project shared by two workspaces would have no home.

  The row is written FIRST and the disk second: a failed provisioner then
  leaves a row with no directory, which is visible and repairable, rather than
  a directory with no row, which is the state the drift detector exists to
  report and which nothing here could explain.

  Creating a project and adding a member are grants of access, so both take
  `require_session`. A proven session is therefore required for
  this work rather than a preference.

### Changed
- `PUT /api/agents/{slug}/channels/{channel_slug}` takes a history watermark
  when it binds a mailbox, carries the existing one over when the
  binding already points at the SAME address, and says in its response what it
  will do with the backlog. Re-saving an unchanged mailbox is something a panel
  does on every save; moving the watermark forward each time would quietly
  swallow anything that arrived since the last poll.
- `GET /api/agents/{slug}/channels` reports `history_before`.
- `routes/messages.py` keeps ONE list of queue states. There were two literals,
  which is how `blocked` came to be written by dispatch and filterable nowhere.

### Notes
- `core-api.sh` mints `SN_PROXY_AUTH_SECRET` generate-once into
  `/etc/smart-node/core-api-proxy-auth.env` (0600 root:root) and injects it into
  the Traefik route as a `customRequestHeaders` middleware, chained AFTER the
  auth middleware. Never in the unit file: that is 0644 and every agent can
  read it.
- Requires the schema in `bootstrap/B02-sn-core-seed.sql` (`sn_config.projects`,
  `project_members`, `agent_channels.history_before`). **Re-run B02 on an
  installed node.** Reference copies: migrations 022 and 023.
- Tests: `tests/test_approval_identity.py` (24 cases) -- the first tests this
  component has ever had. Verified to fail with the gate removed.
- NOT closed by this release: the node planner answers unauthenticated on
  the bridge gateway planner port, which is a second door onto the same data with none
  of these checks on it.

## [0.17.2] - 2026-08-11

An agent is told where its own work area is. `lib/agent_fs.py` defaults an
empty `fs_write` to the agent's own `work/` directory when rendering `agent.yaml`,
so an agent that was given no explicit path learns about its area instead of about
nothing. The directory itself is created and owned by admin-runner; this is the
awareness half, not the boundary. Ownership is the boundary.

## [0.17.1] - 2026-08-04

Every route that writes a message row now records it.

### Fixed
- **The message path wrote nothing to the event journal** (`routes/messages.py`).
  `/send` and `/reply` each write a `sn_core.messages` row and emitted no event,
  in either branch -- so an agent-to-agent handoff and an agent's outgoing reply
  left no trace anywhere. The note at `/inbound` claiming these were "emitted
  from comm.sh" was never true: the communication component only ever sees
  channel messages, never a row this service writes. Both routes now emit inside
  the same transaction as the row -- `message.inbound` for an internal message
  (it is inbound for its recipient), `message.sent` for a queued outbound,
  carrying the agent that actually answered.

## [0.17.0] - 2026-07-28

A created agent becomes a principal, not just a row.

### Fixed
- **Agent identity** (`routes/agents.py`, `_ensure_agent_identity`) -- agent
  creation now writes the `sn_config.users` row in the same transaction as the
  agent. Every principal-gated endpoint resolves the caller's `Remote-User`
  against `sn_config.users.email` and rejects an unknown identity with 403
  (`auth.get_principal`), and internal messaging resolves the RECIPIENT the same
  way -- so an agent without that row could neither write to a colleague nor be
  written to, however its tools were granted. Older nodes looked healthy only
  because their agent rows had been seeded by hand. `PUT /grants` ensures the row
  too, so agents built before this fix heal on their next grant.

### Changed
- **Agent runtime template** (`companions/srv-skeleton/agents/_template/run.sh`,
  shipped by this companion):
  - sources `/etc/smart-node/agent-runtime.env` with `set -a`, so a cron wake
    gets the node's `CLAUDE_CODE_OAUTH_TOKEN`. The gateway receives it through
    systemd `EnvironmentFile=`; a cron wake has no unit, so it must source the
    file itself. Without this a configurator-built agent ran with no credential
    and ended every wake with "Not logged in", exit 0, empty log -- silent, and
    indistinguishable from "no work". `set -a` matters: the file is rendered for
    systemd as bare `KEY=VALUE`, and sourcing that plainly defines a shell
    variable the CLI never sees.
  - wakes on the INBOX as well as the planner. Pushing that wake is the
    communication companion's job, but that companion is optional -- a node with
    no channels has no dispatch watcher -- so an agent asked something by a
    colleague would sleep through it. Fails closed: no messaging grant means a
    403 from `/inbox`, which matches nothing and does not wake.

## [0.16.0] - 2026-07-08

RFC-011 onboarding: the profile projector goes live and gains its provision path.

### Added
- **Provision path** (`routes/provision.py`, `/api/provision`) -- the sanctioned
  way to author `sn_config.tenant_profile` rows and materialize them:
  `GET/PUT /params`, `POST /params/{key}/retire`, `POST /project`. Gated to
  loopback/docker0 or a cross-tenant principal. sysadmin edits the profile and
  reprojects instead of mutating agents/integrations directly (raw writes produce
  drift the reconcile pass cannot see). RFC-011 W2.
- **Projector write behaviors** -- all five RFC-011 behaviors are live in
  `lib/projector.py` (config-sync, seed-once, policy-versioned, stateful-merge,
  secret-write-once), replacing the Step-2 stubs; this is the first deployment of
  the projector to the running core-api. RFC-011 Step 3.

## [0.14.0] - 2026-06-16

Reconciles deployed-node drift back into canon: two features that were live on
reference-node (vendored /opt copy) but never committed to smart-node-core. Canon is now
the source of truth again.

### Added
- **Integration capabilities** - `sn_config.integrations.capabilities` (text[]) plus
  capabilities-aware read and INSERT/UPSERT in `routes/integrations.py`, and the
  `capabilities` field in `schemas/integration.py`. The UPSERT preserves existing
  values on empty-list input. Backed by migration `014_integration_capabilities.sql`
  (idempotent ADD COLUMN IF NOT EXISTS; the column existed out-of-band on reference-node with
  no prior migration). Previously also committed, wrongly, in the installer repo
  companions/core-api (e743bd6).
- **Recipient-scoped inbox** - `GET /messages?mine=true` returns only messages
  addressed to the calling agent: its slug (internal mail) or any of its enabled
  channel mailboxes (via `sn_config.agent_channels`). Backs the recipient-scoped
  `comm inbox` + read-receipt flow. Lived only on the deployed node.

## [0.3.0] — 2026-06-04

Communication channels as a registry, and a schema consolidation that pulls the
event log and runtime/comms tables out of the default `public` bucket into `sn_core`.

### Added
- **Channels registry** — `sn_config.channels` (catalog: web-chat, voice, telegram,
  email available; whatsapp, sms, slack, discord, google-chat planned) plus
  `sn_config.agent_channels` (the agent<->channel binding, with a derived trigger:
  realtime channels route via brin, scheduled via the secretary). Migration `1_020`.
- **Agent runtime v2** — `srv/agents/_template/run.sh` is now a real wake-and-run
  entry point (model from `agent.yaml`, creds + internal-TLS handling, runs the
  worker against the agent's own `CLAUDE.md`), replacing the no-op stub.

### Changed
- **Schema consolidation: events + runtime tables move to `sn_core`.** The event log
  and comms tables lived in Postgres's default `public` schema only to mirror an
  earlier prototype, which was thrown away, so that constraint is gone and
  these are runtime data belonging next to `ai_sessions`/`crons`/`node_services`.
  - `1_021` — `events` (+ its identity sequence, indexes, immutability trigger) -> `sn_core`.
  - `1_022` — `actors`, `messages`, `message_state` -> `sn_core`.
  - `public` now holds none of our domain tables (`sn_config` = config, `sn_core` = runtime).
  - Code repointed: `events_emit.py`, `routes/{events,system,crons,messages}.py`,
    `schemas/event.py`, and the `99_local_test_actors.sql` seed.
  - Reverses migration `008a` (which had moved `sn_core.events` -> `public` to mirror that prototype).

### Notes
- Cross-schema FKs stay valid (Postgres tracks the object, not the schema name):
  `public.identity_users.actor_id -> sn_core.actors`, `sn_core.messages.ai_session_id ->
  sn_core.ai_sessions`, `*.tenant_id -> sn_config.tenants`.
- On a fresh node init the golden dump still creates these in `public`; the `1_021`/`1_022`
  migrations move them to `sn_core` (the dump itself is left untouched). Installed nodes
  pick this up on configurator redeploy.

## [0.2.0] — 2026-06-03

Registry-driven prompt composition: an agent's `CLAUDE.md` is no longer the raw
specialist body but the deterministic sum of reusable registry blocks (role +
per-tool usage contracts + policies), composed by core-api on every save. One edit
to a shared block recomposes every agent that references it.

### Added
- **Tool usage contracts** — `sn_core.tools.usage_md` (migration `1_018`); seeded
  for `planner`, `analytics`, `mesh`. A tool now carries a "how to use me" contract
  that composes into the prompt of any agent granted it.
- **Prompt composition** — `agent_fs.compose_claude_md()` assembles role
  (`specialists.body_md`) + granted tools' contracts + policies. `core-api`
  composes on agent create/update and on grants save; `GET /agents/{slug}/system-prompt`
  now includes the same tool contracts, so the file (run.sh) and API (gateway)
  runtime paths emit an identical prompt.
- **Tool grants in the DB** — grants are projected into `sn_config.agent_tools` on
  save (`_sync_agent_tools`), making "which agents have tool X" a single query
  (the basis for fan-out recompose).
- **Per-agent regenerate** — `POST /api/agents/{slug}/regenerate` recomposes one
  agent's `CLAUDE.md` from the registry.
- **Catalog seeds** — `sn_config.forbidden_actions` vocabulary (migration `1_016`);
  real tool registry + department catalog seeded, pytest tool debris removed
  (migration `1_017`).
- **Companions** — `compose-prompt.py`, `recompose-affected.py`, `sync-grants.py`
  (CLI equivalents of the composition/grant-sync for batch and cron use).
- **Tests** — suite now at 65 pytest tests (was 31).

### Changed
- Agent-edit **Save** writes config then grants **sequentially** (was parallel) so
  the final `CLAUDE.md` composition sees both the fresh role and the fresh grants.

### Fixed
- `sn_config.agent_tools.tool_id` foreign key repointed from the vestigial empty
  `sn_config.tools` to the canonical `sn_core.tools` (migration `1_019`).

### Notes
- Policy selection in the agent-edit UI still writes to localStorage; policies have
  no categories/assignments yet, so this is the deferred policies feature, not a
  composition gap. The composition engine already supports policies once assigned.

## [0.1.0] — 2026-05-29

Initial unified core: backend + frontend + database schema + `/srv` base in one
repo, runnable as a single local stack (`docker compose up -d --build`).

### Added
- **Backend** (`backend/`) — core-api FastAPI controller, 11 routers
  (agents, specialists, tools, integrations, runtimes, policies, crons, mcp,
  events, system, secrets) over `sn_config`/`sn_core`; migrations 001–012.
- **Frontend** (`frontend/`) — static admin panel (nginx), calls `/api`.
- **DB** (`db/`) — schema init + migrations; pgvector via docker-compose.
- **/srv base** (`srv/`) — agent `_template`, 5 agnostic `_specialists`
  (sysadmin, accountant, analyst, researcher, support), `scripts/lib`
  (logging.sh, env.sh), convention `CLAUDE.md`s.
- **DB→/srv projection** — `agent_fs` renders an agent dir from the template +
  DB row on create; re-renders `agent.yaml`/`config.json` on update; archives
  on delete (incl. inline specialist).
- **Crontab projection** — `lib/crontab.py` + `companions/render-crontab.py`
  render `sn_core.crons` into a cron.d file via `cron-runner.sh` (slug-keyed).
- **Tools projection** — `lib/tools_index.py` + `companions/render-tools.py`
  render `/srv/tools/REGISTRY.md` from `sn_core.tools`.
- **Specialist seed** — `seed-specialists.py` idempotently inserts the agnostic
  specialist files into `sn_config.specialists` at install.
- **Tests** — 31 pytest tests across all routers + the renderers.

### Fixed
- Actor FK on a fresh node: `emit()` self-registers the actor so
  `events.actor` always resolves (first agent create no longer 500s).
- Crons by-job run mapping made stable via a new `sn_core.crons.slug` column
  (migration 012).
- Secrets routes return a clean 503 when the sops binary is absent/timeouts
  (was an unhandled 500).
- UI slug `pattern` regex escaped (`[a-z0-9\-]`) so the 6 create forms validate
  under the browser `v` flag.

### Notes
- Source of truth is the database; `/srv` is a projection rendered by core-api.
- The installer is a separate repo that deploys this core.
- Assembled from the core-api and panel repositories; those
  remain the per-component upstream history.
