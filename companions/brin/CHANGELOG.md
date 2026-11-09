# Changelog

All notable changes to `smart-node-brin` go here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are semver. Tags on `main` correspond to the `VERSION` file.

## [Unreleased]

### Changed
- The Runner reads and writes the planner's three statuses. It asks for them
  (`X-Status-Vocabulary: new`) and decides which flow tasks to start or step on
  status and wait_reason: queued with nobody on it starts, in_progress with
  approved_at starts, a run in flight is stepped also when queued for review;
  queued for approval or for a person, or closed, is never driven. It closes a
  released approval as `closed, completed` and an owner reject as
  `closed, rejected` with a comment (the reject used to send no comment, which
  the planner refuses for a close). An older planner's one-word status is read
  the way migration 0010 maps it. The TaskWatcher wakes on queued with nobody on
  it and on queued for review.

### Security
- The session summary no longer carries an OpenRouter key in its source. It
  read `OPENROUTER_API_KEY` with a real key as the default, so every node
  shipped with that key and spent on it. The key now comes from the
  environment only; without it the summary is skipped and the log says why.
  The key that was in the source must be revoked.

### Removed
- Removed modules nothing used: `core/system_prompt.py`, `core/session_store.py`,
  `core/named_session_store.py`, `core/context_enricher.py`.
### Changed
- The Runner starts an approved workflow task on the planner's own mark. A
  task filed by an agent or a service lands in awaiting_approval and the
  owner's approval moves it to in_progress, never to open; the Runner drove
  `open` or a task tagged `flow:start` by hand, so an approved task that
  nobody had tagged never started (four content tasks on one node waited
  for days). Now `in_progress` with `approved_at` and no run is a start, a
  task the owner sent back (modify, no `approved_at`) is not, and the tag
  means nothing. The rule is one function, `_flow_task_start`, with the
  table of what the Runner does per planner stage, and a unit test.

### Added
- `POST /api/message` takes `wait_seconds`: how long the gateway holds the
  turn before 202 "still working". fast-dispatch asks for the whole turn.
- Runner: a flow the owner switched off (status `off` in the catalog) starts no new run; a run already in flight finishes, and the waiting task starts when the flow is on again.

### Added
- chat_history(message_id) in the messaging MCP: an agent mentioned in an office
  chat reads the latest turns of that chat, and older ones with before=<index>.
- A turn refused because the owner stopped the agent (gateway 409 `agent_stopped`)
  tells the person so by name, with the way back, instead of "I could not answer".

### Fixed
- The outer gateway and runner waits derive from the largest registered turn ceiling, so an environment value cannot cut a configured long turn first.
- Message responses carry actions and memory from their own turn. Concurrent
  agents and later HTTP turns cannot receive another turn's artifacts from
  the shared executor. HTTP tool loops report their own dispatched actions.

## [1.8.0] - 2026-09-26

Includes entries that shipped with node 1.12.46 without a version heading of their own.

### Fixed
- A background turn (system or internal channel) is steered to answer its sender on
  the last tool rounds instead of ending in unsent plain text. The messaging
  MCP usage text tells an agent to answer a colleague with inbox_reply, never by
  writing into another agent's chat.

### Fixed
- The turn ceiling is read from one place and every caller derives its timeout
  from it (#7): `core/turn_budget.py` gives the ceiling
  (`SN_TURN_MAX_SECONDS`, else `SN_CLI_TURN_TIMEOUT`, else 600) and the two
  waits above it. The runner's wake used 310 s and the executor's three gateway
  calls and `mcp_server._gateway_post` used 300 s, so a turn was cut at five
  minutes whatever the node allowed. On the reporting node this raised
  httpx.ReadTimeout out of `executor._chat_response` 207 times between
  2026-08-17 and 2026-09-11, while turns of up to 1191 s were being held.

### Added
- api rows may name path parameters: `{task_id}` in `http_path` is filled
  from the call's arguments, URL-quoted whole, and removed from the query or body.
  A missing one is named and nothing is sent. Before, the braces went out literally.
  A name used twice in one path is filled twice, and a missing one is reported once.
- A row whose `input_schema` sets `additionalProperties: false` is enforced, not
  merely advertised (the merge review review): an argument the row does not declare is refused
  before the request is built, naming what was dropped and what the row takes.
  Open schemas are unchanged, so a row opts in by closing its own schema. Without
  this, `planner_advance` (a PATCH of the whole task, described as a status move)
  passed `assignee` or `priority` straight through to the planner.
- `GET /api/agents/<slug>/console` proxies the gateway agent console for the office
 ; 3-second timeout, never raises, an error reads as nothing in flight.

### Changed
- The message watcher no longer marks a `desk:<id>` row processed on sight; it asks
  core-api's deliver-desk for the decision and leaves the row for the next tick if the
  call fails. The unit sets MESSAGE_POLL_INTERVAL=2 (was the code default 10).

### Added
- `GET /api/desks/{id}/activity` passes the `since` cursor through to the gateway, so
  the office receives only the steps it has not shown yet.

### Added
- The `fetch` builtin runs in the tool loop: GET or POST to a public
  http(s) URL, refused when the host resolves to a loopback, private or
  link-local address, redirects reported rather than followed. A builtin brin
  cannot run (`fs`) is no longer offered to the model, only reported deferred.
- `POST /api/desks/{id}/stop` stops the turn running in a desk through the
  gateway's interrupt and records `agent.turn_stop_requested` with who asked; the
  turn itself ends as `agent.turn_stopped` with its partial answer. `GET
  /api/desks/in-progress` lists desks with a turn in flight.
- An internal clean-context relay lets Office request the gateway's audited
  manual session retirement without exposing the gateway directly.

### Fixed
- Before a CLI desk turn, trailing Office attachment handles become explicit
  instructions to run the installed attachment reader and then built-in Read.
  The raw handle remains the durable chat text, so chips still render after a
  reload. Audio/video handles instead become a plain unsupported notice and are
  never presented as if the agent saw their contents.
- The messaging MCP now stamps new colleague requests with the Office desk
  origin inherited from an opaque CLI session. Answers therefore appear in
  `to_relay` and are returned to the person who asked; explicit relay calls do
  not inherit the origin again and cannot form a relay loop.
- A desk turn holds its agent for the gateway's turn cap instead of 300 s, the
  lease is released when the last desk turn of that agent ends, and a turn past
  the sync window keeps the agent until it ends and then writes its end.

### Changed
- The tool loop runs only for an agent whose runtime is HTTP, read as
  `runtime_kind` from core-api. It used to run for any model id
  containing a slash, so a `claude-cli/...` agent would have been sent to
  OpenRouter. The stateless call sends the stored id and no longer pins
  `provider: openrouter`.

## [1.7.5] - 2026-09-16

### Fixed
- A gateway `409` carrying `{"error": "session_reset"}` is no longer an upstream
  failure. It means the session was retired after its context was written to
  memory, and the message was never delivered: brin now resolves a clean
  session and sends the same text once more. Without this the person's message
  ended as a failed inbound row with no reply.

## [1.7.2] - 2026-08-20

### Fixed
- The message watcher polled the bus **as an agent**, and had been answering 403
  on every poll since the earlier change identity hardening: `_actor()` resolved an agent
  slug and sent it as `Remote-User` over the TCP door, which is exactly the
  identity core-api refuses there ("an agent identity is accepted only over the
  core-api unix socket, where the kernel establishes it"). Inbound routing had
  stopped fleet-wide: 354 messages sat at `new` on one node since 2026-08-16, and
  agents did not wake on new mail at all.

  The watcher is not an agent and now stops pretending to be one. It reads the
  whole inbound queue in order to route it, which is a node-level job, so it
  calls as the node owner at `asserted` level -- adequate for ordinary work by
  auth.py's own rule, and nothing here is an approval. `_actor()` remains, but
  only as the "has this node any agent to route to" guard it should always have
  been.

  Note the socket is **not** the fix here, unlike 1.7.1: `mcp_messaging.py` is
  spawned as the agent's own OS user so SO_PEERCRED names an agent row, while
  this service runs as `smart-node-dispatcher`, which has no agent row and is
  answered `no active user for identity 'unknown-peer'`. Neither door will carry
  an agent identity on the watcher's behalf.

- A failing poll no longer writes a traceback every ten seconds forever. The old
  shape had produced a 429 MB log on one node and a 901 MB log on another while
  the service stayed `active` and every check passed -- nobody reads a file that
  size, so the node was mute and looked healthy. Failures are now loud once, then
  rate-limited with a back-off to a 30s ceiling, and recovery says so.

## [1.7.1] - 2026-08-18

### Fixed
- The messaging MCP called core-api over TCP and asserted the agent with a
  `Remote-User` header. core-api refuses an agent identity on that door by
  design, so **every messaging tool call on every node answered 403**: an agent
  could neither read its own inbox nor reply, and the model reported it as
  having nothing to do. It now calls the unix socket, where the kernel names the
  calling process. `SN_CORE_API_URL` still overrides, for a test bed with no
  socket, and is deliberately not a fallback -- retrying over TCP would restore
  the silent 403.

## [1.7.0] - 2026-08-16

The agent recalls a person across channels, and every web-chat turn is recorded.

### Added
- On a desk (web-chat) turn brin now hands the executor the authenticated office
  user's identity, and the ledger assembles a `[conversation with <person>]`
  section from `GET /api/messages/by-contact` -- the person's recent history
  ACROSS channels (the last ~30 messages, wide enough that a chatty channel -- a
  voice session runs to hundreds of turns -- does not crowd telegram or email out
  of the window), folded into the turn. So the agent answers "what did we say on
  telegram" from context instead of grinding through eleven tool calls to dig it
  out (measured: 81s of tool-flailing before, ~16s from context after). Only desk
  turns carry the identity; telegram/email arrive with a channel, not a session.
  `ledger_service.fetch_person_history` / `render_person_history`;
  `build_ledger_sections(agent_slug, person_identity)`;
  `executor.run_direct(..., person_identity)`.
- `_persist_webchat_turn`: after the answer is produced, brin records the
  inbound + outbound web-chat pair to the registry via core-api
  `/api/messages/record-webchat`. Best-effort -- a persistence failure
  never touches the live chat -- and gated to desk turns so it never records the
  agent's own inference calls or double-records telegram/email.

## [1.6.2] - 2026-08-13

### Fixed
- A slow wake no longer condemns the agent permanently. `runner.py`
  treated any non-200 from `/api/message` as terminal and wrote `status=broken`;
  brin raises `httpx.ReadTimeout` on its own gateway call at 300s and answers
  500, so an agent whose model merely ran long was marked broken -- and a broken
  agent never appears in `/api/agents/with-pending-work` again, so nothing ever
  woke it back. The outcome is now classified in three: a timeout, transport
  error or 5xx is a failed ATTEMPT (claim released, watermark rolled back, agent
  retried after a backoff, `broken` only after `RUNNER_WAKE_MAX_FAILURES`
  consecutive tries, with the count in `last_error`); a 4xx is still terminal,
  because a malformed wake does not become well-formed by repetition; a success
  resets the counter.
  The watermark rollback is the half that is easy to miss and silently fatal:
  `tick()` advances `_last_woken` BEFORE launching, so a retry that does not put
  it back retries nothing at all.
  Not hypothetical and not new, only masked: `webmaster` sat broken from
  2026-06-28 to 2026-08-13, forty-six days, with `wake 500` as its whole
  epitaph, and `developer` broke the same way during the first drain after the
  cron was retired. The cron used to wake agents regardless of runtime status,
  so it had been quietly repairing this every sixty seconds. Timings are not
  marginal either: a normal `alex` wake on reference-node measured 210s of the 300s
  budget.
- A completed wake closes what it was woken for. The
  closure added in the 1.9.20 release lived in `run.sh`, which the Runner migration retired.
  Ported here with its semantics intact: snapshot before the model runs, close
  only ids present in that snapshot AND still unfinished after, internal inbox
  only, and emit `agent.inbox_autoclosed` when the agent left messages open so
  the fix makes the fault visible instead of hiding it.
  Measured on reference-node the morning after the cron was retired, with every excuse
  removed (17 agents free, none busy, none broken, outbound empty): eleven
  inbound rows nobody was coming for, the oldest from 2026-06-29.
- Closure counts `routed` as unfinished, not done. On a live node the whole
  backlog moved `new -> routed` within 18 seconds of the wake; `routed` is
  invisible to `/api/agents/with-pending-work` (which keys on `new`), so a
  closure that also looked only at `new` would have watched eleven messages move
  from one invisible bucket to another and reported success. Found by running the
  fix on reference-node, not in review.
- The inbox window used for closure is no longer the default 20 messages, which
  on a busy node is the last hour and cannot see a six-week-old row.

### Added
- `RUNNER_WAKE_MAX_FAILURES` (3), `RUNNER_WAKE_RETRY_BACKOFF` (60s),
  `RUNNER_INBOX_LIMIT` (100).
- `tests/e2e_wake_resilience.py`. Both defects are invisible to inspection -- the
  node reads perfectly healthy while an agent is never woken again and a message
  has nobody coming for it -- so the harness CONSTRUCTS each failing state and
  runs every scenario against the pre-fix runner as well, refusing to pass if the
  baseline does not fail. No model is called and no node is touched.

## [1.6.1] - 2026-08-11

### Fixed
- An upstream failure is no longer returned as the agent's answer. Three
  points in `core/executor.py` converted a gateway error into a string and handed
  it back as the reply: `_chat_response`, its streaming twin `_stream_response`,
  and the empty-response case in `_maybe_run_tool_loop`. They now raise. By the
  time such a string reached dispatch the failure was type-erased -- HTTP 200,
  content non-empty, both checks satisfied -- so on 2026-08-04 a gateway 502 was
  emailed to an outside address carrying internal service names and the path to a
  secret. Dispatch already had the failure path; it was simply never reached.
- `main.py`: the two live paths (streaming and synchronous) catch that failure and
  render one plain sentence to the person waiting, with the detail in the log.

### Deliberately unchanged
- `_execute_command` still returns its error as text. It is a TOOL result, read by
  the model, and raising there would kill a whole turn over one bad command. The
  task listed it among four; its own stated principle excludes it. Likewise the
  six equivalents in `core/catalog_tools.py`: an error to the model is normal, an
  error to the customer is a defect.

## [1.6.0] - 2026-07-28

Internal messaging becomes callable by an agent that has no shell.

### Added
- `mcp_messaging.py` -- a stdio MCP server exposing the node's existing messaging
  endpoints (`POST /api/messages/send`, `/reply`, `/dismiss`; `GET /inbox`) as four
  tools: `send_message`, `inbox_read`, `inbox_reply`, `inbox_dismiss`. The backend
  and the bus already shipped; what was missing was a way to CALL them. A business
  agent gets no bash, and the CLI runtime offers only what its `.mcp.json` lists,
  so the api-kind rows registered earlier were a contract with no transport. The
  sender is not a parameter: it is read from `SN_AGENT_SLUG` (exported by run.sh
  from the agent's own folder name) with a cwd fallback, so the model can neither
  forget nor spoof who it writes as. Binds `MCPServer` (SDK 2.x) or `FastMCP`
  (1.x), since nodes ship mcp 2.0.0. Registered per node by
  `companions/dispatcher.sh` under MCP-server slug `messaging`.

  Scope: this serves the CLI/wake path. brin's own live-chat loop dispatches MCP
  over SSE only, so a stdio server is invisible to it -- live chat reaches
  colleagues through the dispatcher as before.

## [1.5.0] - 2026-06-16

### Added
- `services/task_watcher.py`: bridges node-planner
  task assignments to the messages bus until the planner emits the notification
  itself. Polls the node planner for actionable tasks (`open` / `pending_review`
  assigned to a known agent) and calls core-api `POST /api/messages/inbound`, so
  the assignee is woken on the single MessageWatcher path. Throwaway; the webhook
  and the routing below are permanent.

### Changed
- `MessageWatcher`: route by recipient. A `system`-channel message is a doorbell
  addressed to an agent slug, so wake that agent with the message content, then
  mark the doorbell processed (no agent closes a system message). Email/chat is
  unchanged (wake the default agent; the secretary owns its state via comm). This
  closes the TASK branch end to end: inbound -> secretary -> PM task -> wake PM ->
  plan -> handback -> wake secretary -> threaded reply.

## [1.4.0] - 2026-06-16

### Added
- `services/message_watcher.py` (`MessageWatcher`): background poller that wakes
  the owning agent when a new inbound message is recorded. This is the dispatch
  link of the communication layer (channel adapter -> sn_core.messages -> this
  watcher -> brin `/api/message` in agent-driven mode, so the CLI backend is
  resolved from the agent DB row and the agent reads/replies via comm). Wakes are
  event-driven, so a woken agent never spends an LLM call on an empty tick (the
  acceptance criterion, met by construction rather than by a cron+guard).
  Reads new inbound via core-api, de-dups in-process (mirrors `ReinWatcher`),
  never touches message state. Started/stopped in the app lifespan.

## [1.3.1] - 2026-06-15

### Fixed
- Durable desk-session reuse. `_get_or_create_session` only reused a session via
  an in-memory cache, so a dispatcher restart wiped it and spawned a brand-new
  session for a desk that already had one -- the agent "forgot" the conversation
  (new session id after reload). On a cache miss it now looks the conversation's
  session up in the gateway DB (`GET /v2?conversation=desk:<id>`) and
  reuses/resumes it before creating a new one, so a live session is not duplicated.

## [1.3.0] - 2026-06-14

### Changed
- `executor._get_or_create_session`: when a desks cached session exists but is
  not active, try `POST /sessions/{id}/restart` (resume) before creating a new
  one. Prevents the "agent introduced itself again" context loss after a gateway
  hiccup -- the desk keeps its conversation instead of spawning a fresh session.

## [1.2.1] - 2026-06-14

### Removed

- Deleted the now-dead `core/agent.py` (`Agent` / `AgentResult`),
  `core/tool_registry.py`, `core/tool_executor.py`, and the whole
  `core/v2tools/` package -- the hardcoded-tools path that 1.2.0 unwired. No live
  code imported them; tools are catalog-driven via `core/catalog_tools.py`.

### Changed

- `services/intent_service.py` -- comment no longer references the removed
  `core/agent.py` (points at `core/executor.py`, the live egress path).

## [1.2.0] - 2026-06-14

**Phase 2: catalog-driven tools.** brin builds an agent's tools from the DB
catalog (`sn_core.tools` + `sn_config.agent_tools` grants) and executes them at
runtime, replacing the compiled-in `core/v2tools` that were registered into the
never-invoked `Agent(ToolRegistry)` path.

### Added

- `core/catalog_tools.py` -- loads an agent's grants from core-api
  `GET /api/agents/{slug}/tools`, builds OpenAI function specs, and dispatches:
  `api` over HTTP (forwarding `Remote-User`), `builtin` `bash` locally, and
  `mcp` server-grants expanded into per-operation specs and called over SSE.
- `core/mcp_client.py` -- minimal MCP-over-SSE client (official `mcp` SDK);
  `tools/list` is cached per server, so discovery is once-per-process not per call.

### Changed

- `core/executor.py` -- `run_direct` runs the catalog tool loop (over the gateway
  stateless `/api/v1/chat`) for transparent (openrouter) agents that have
  dispatchable grants; opaque (claude-cli) agents and agents with no dispatchable
  grants fall through to the normal gateway turn unchanged.

### Removed

- `main.py` -- retired the dead `Agent(ToolRegistry)` + `ToolExecutor` wiring and
  the nine `core/v2tools/*` registrations (they were registered only into the
  un-invoked Agent path). The `core/v2tools/` files remain on disk, now unused.

## [1.1.0] - 2026-05-27

**Repo extraction.** Previously vendored in the installer repository as `companions/brin/`; now its own repo. This release supersedes an experimental rename track (the "Axon" branch) -- production runs as `smart-node-brin` / `smart-node-dispatcher.service`.

### Changed

- `main.py`, `core/agent.py`, `core/executor.py` -- diverged from the 1.0.0 vendor snapshot during May 2026 on-node iteration. State at extraction matches the live node's `/opt/smart-node/dispatcher/`.

### Service unit

- `systemd/smart-node-dispatcher.service` -- canonical reference; the companion installer still generates this inline pending the P3 host-level migration.

## [1.0.0] - 2026-04 (vendored, not separately released)

Initial pre-extraction state: realtime trigger dispatcher serving `/api/message` for the communication layer, with MCP server, session store, specialist loader, tool registry, intent routing.
