# Changelog

All notable changes to `smart-node-gateway` go here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are semver. Tags on `main` correspond to the `VERSION` file.

## [Unreleased]

### Added
- A Flint provider row can name its upstream: `FLINT_BASE_URL` on the row is
  where `/openai/v1` sends the calls on (another OpenAI-compatible service,
  NVIDIA's API for one, or a local server), the way `ANTHROPIC_BASE_URL`
  works for a Claude Code row. Flint itself still gets the door's address.

### Changed
- The door checks every model call, not only the start of a turn. An agent
  stopped, or a hard budget spent, in the middle of a long turn ends it at
  the next call: 403 or 402 at `/anthropic` and `/openai/v1`, and 409
  `agent_stopped` or 402 `budget_exhausted` (the shapes brin already reads)
  for `/api/v1/chat` and session turns, whose session stays intact. Before,
  `/api/v1/chat` checked no budget at all.
- `/anthropic` refuses a bearer the gateway did not issue (401) instead of
  forwarding it. That was for processes started before the relay, with the
  node credential in their environment; none is left.

### Changed
- The gateway's own model calls pass the door too. `/api/v1/chat` and the
  HTTP session backends called OpenRouter, Gemini and Anthropic with clients
  of their own and left no per-call record; now those clients send through
  `model_door.Transport`, and each call is kept in `model_calls_live` and
  recorded in `sn_core.model_calls` with the agent and session it was for
  (`auth` internal). Turn usage and budgets are unchanged.

### Changed
- Flint calls the model through the gateway, like Claude Code. It used to get
  the provider key into its own process and call OpenRouter itself, so the
  key sat in an agent's process and the calls were in no record. Now the
  runtime gives Flint a gateway token in the key's place and `FLINT_BASE_URL`
  pointing at the door; the new `/openai/v1` route (`openai_relay.py`) swaps
  the token for the key, forwards chat calls and the model list to OpenRouter
  or OpenAI, and records each chat call in `sn_core.model_calls`. A bearer the
  gateway did not issue is refused there. Flint on an Anthropic provider goes
  through `/anthropic/v1`, which now also takes the token as `x-api-key`. The
  wrapper passes `FLINT_BASE_URL`. Needs a Flint build that reads it.

### Removed
- Removed code nothing called: `parse_duration.py`, `slugify.py` (and their
  tests), `sse_parser.SSEEventParser`, `CLI_DEFAULT_MODEL`,
  `CLI_MAX_SESSIONS_PER_IP`, the commented-out backend stubs.

### Fixed
- The turn limit ends the turn, not the session. A turn cut off by
  `turn_seconds` answers `stopped` (`stopped_by: turn_seconds`, `outcome:
  stopped`) and its session stays active, so the next message resumes the
  same conversation with `--resume`. Before, the session was set failed and
  the agent started over in a new one without what it had done. The cutoff
  is still journalled and the owner still notified as configured.

### Fixed
- An agent stopped and enabled again resumes its session on the next message
  every time. The idle sweep forgot a dead process before it looked at the
  send lock, so a respawn that exited at once lost its bookkeeping mid-send and
  the turn ended 502 `KeyError`; about one resume in five failed on a stand.
- The process cap refuses a turn, not the session. A refusal raises
  `SpawnCapReached`, the synchronous route answers 503 with `Retry-After: 10`
  and `error: spawn_cap`, and the session stays active; before, it was set
  failed and every later message got 409.

### Added
- `sn_core.model_calls_live` (UNLOGGED): one row per model call in flight,
  written by the relay when the call goes upstream, stamped while answer
  events stream (pings and comments are not progress, at most every 5 s,
  never awaited by the stream), deleted when the call ends. The door empties
  it at start. A call that hangs is visible while it hangs, not only once it
  ends.
- `GET /api/agents/processes` gives each process `model_call` (the call in
  flight), `waiting_on` (`model`, `tool`, `cli` or none), `last_activity_at`
  and `silent_seconds`: a live process that is not working can be told from
  one that is.

### Changed
- A tool step in the live desk activity carries the model's own one-line
  description of the call as `desc` (read from the tool input's `description`
  field). The command stays in the step text; a call without a description gets
  no `desc`, never the command.
- A turn the CLI ended with an error result ("API Error: 529", "Prompt is
  too long") is `failed`, not an answer: `final_answer` is empty, the reply
  carries `failed` and `outcome` (`ok`, `stopped`, `failed`), and the failure
  is journalled like a turn that raised, so `turn.failed` monitors see it.
  The session is not failed.

### Added
- `POST /api/v1/sessions/{id}/messages` takes `wait_seconds`: the caller
  waits that long for the answer instead of `SN_SYNC_RESPONSE_TIMEOUT`, at
  most `SN_MAX_SYNC_WAIT` (3600). A live channel that has nowhere to put a
  late answer asks for the whole turn.

### Added
- A row of `sn_config.cli_providers` that runs the Claude Code binary is a
  backend under its own slug. `GET /api/v1/backends` lists it, the catalog
  accepts models on it, the runtime runs the same binary with the row's
  `env_vars`, and the door routes the process's model calls by the provider
  its token names: the row's `ANTHROPIC_BASE_URL` is the upstream and its
  `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` the credential. An
  upstream of the row's own needs no credential, and the token is never sent
  as one. This is how a test node runs its agents against a local model
  server, and how a second Anthropic account is set up, without code.
  `sn_core.model_tokens` and `sn_core.model_calls` gain a `provider` column.

### Changed
- Provider keys and the CLI token are read from the node's one secret store,
  `sn_config.secrets`, under the same refs as before. Each load of a key is
  journalled once as `secret.read` by `component:gateway`, and again only when
  the value changes.

### Added
- The process table, `GET /api/v1/sessions/processes`: every live agent
  process from the runtime's own state (agent, OS user, pid, whether a turn
  is running and what it does, how long idle) with its last model call from
  `sn_core.model_calls`. `POST /api/v1/sessions/agent-stop` ends every
  process of an agent now: a turn in flight is interrupted so its loop reads
  the end as stopped, not as a crash to retry; sessions are kept for
  `--resume`.
- A disabled agent runs no turn anywhere. A message to one of its sessions is
  refused with a structured 409 (`agent_stopped`); before, "disabled" only
  kept the runner from waking it while an open chat still started its process.

### Changed
- The gateway is two processes of one companion. The door (`main.py`,
  `smart-node-gateway.service`, :8000) is the one address: cloud providers,
  `/anthropic/*` for the agents' own model calls, budgets, usage. The runtime
  (`runtime_main.py`, `smart-node-runtime.service`, loopback :8002) owns the
  agent CLI processes: `/api/v1/sessions/*`, the session-first v2 API,
  activity, interrupt and steer, and a chat on the local CLI. The door
  forwards those as they arrive, so no caller changes its address, and a
  restart of the door no longer ends a single agent's turn. The door runs
  with `NoNewPrivileges=true` again; sudo belongs to the runtime alone.
- A process's model token is stored as a digest in `sn_core.model_tokens`
  (`model_tokens.py`), minted by the runtime, checked by the door. A door
  restart leaves every running agent's token valid; a runtime start or stop
  revokes all of them, since nothing it minted for is running.
- `/api/v1/health` reports the `cli` provider from the runtime's own health
  (`active_sessions`, `live_children`); a runtime that does not answer is
  reported as unavailable with the address, not guessed.

### Added
- The gateway is the one door to the model for CLI agents too. An agent's
  Claude Code process no longer receives the node credential: at spawn the
  gateway mints a token for that process and hands it, with the gateway's
  own address as `ANTHROPIC_BASE_URL`, over the private stdin frame the
  wrapper already used for Flint. `/anthropic/*` forwards every call to the
  provider, swapping the token for the node credential on the way out, holds
  the process to the model family its session was created with (plus haiku,
  the CLI's own side model), and records each call in `sn_core.model_calls`
  with the gateway session id the CLI sends. A process started by an earlier
  gateway keeps working: a bearer the gateway did not issue is forwarded as
  it is. `MODEL_RELAY=false` restores the direct path.

### Fixed
- A CLI turn in flight keeps its session's `last_activity_at` current, stamped on every idle sweep. The session reaper judges idleness by that column alone, and a turn writes no message row until it ends, so a turn longer than the reaper's fifteen minutes was killed as idle while it worked.
- Every reclaim of a CLI process (idle, cap eviction, respawn, close, turn ceiling, shutdown) writes a `session.reclaimed` event with the agent, the reason, whether a turn was in flight and for how long. Before, the only trace was a line in the service log.
- CLI turn ceilings and money policy are read from the limits registry at call time. A turn cut at its ceiling writes a chat line and a `limit_tripped` event and queues an owner alert.
- CLI usage keeps one row per provider request id, replacing repeated content
  events with the latest measurement while preserving request order.
- A cumulative CLI turn without per-request usage records reports unknown
  context instead of retiring a session based on its total billed input.
  Turns with records continue to use the last request's context.

## [2.7.0] - 2026-09-26

Includes entries that shipped with node 1.12.46 without a version heading of their own.

### Fixed
- The longest silence allowed between two CLI response lines follows the turn
  ceiling instead of a flat 300 s (#7). A turn with one quiet step longer than
  five minutes was killed as a readline timeout although `SN_TURN_MAX_SECONDS`
  allowed twice as much. `SN_CLI_READLINE_TIMEOUT` restores a shorter silence
  detector for a deployment that wants one.

### Added
- `GET /api/v1/backends`: the upstreams this gateway can run, from
  `backends.UPSTREAMS` (the table `resolve_backend` dispatches on), with kind,
  status and the key ref an http upstream needs. core-api validates catalog rows
  against it.
- An agent's console: `GET /api/v1/sessions/agent-activity?agent=<slug>` returns
  every turn of that agent in flight on any channel (runner, desk, ...) with its ring of
  steps, and the turn it finished last, copied when send() ends because the ring is reset
  by the next turn. Before, the steps were served only by desk id, so a turn the runner
  started had no viewer. Tool output is still never carried.
- The live steps of a Claude CLI turn: every tool call is recorded with
  one short detail (the command, file or query, cut at 80 chars) and every line the
  agent says, in a per-session ring of 40, reset when a turn starts. Served with a
  `since` cursor from the desk activity route. Tool OUTPUT is never carried.

### Fixed
- The context handoff threshold is compared with the context of the last
  provider request of the turn (`turn_context_tokens`), not with the CLI
  `result` usage, which sums every request of the turn. Sessions were
  being reset far below the threshold: 63k of real context recorded as 481k.
  The `context_tokens` field of `session.context_threshold_crossed` now means
  what it says.
- Opaque Claude CLI desk sessions pass their real `desk:<id>` conversation to
  stdio MCP children, allowing the messaging MCP to preserve the person-facing
  origin on a question sent to a colleague.
- Hard-limit exhaustion waits for core-api to accept the owner notification;
  gateway no longer claims `notified_at` before a best-effort background send.
  A failed or unconfigured send stays retryable on the next 402.

### Added
- The `gemini` runtime: `backends/http_google.py` (direct Google API, key
  `gateway/gemini-key`) and the seeded catalog row `gemini/gemini-flash-latest`
  (vendor google). No release carried it before; example-node ran
  a hand-deployed copy.
- The Gemini provider does function calling: OpenAI tool specs become
  `functionDeclarations` (`parametersJsonSchema`), `functionCall` parts come back
  as OpenAI `tool_calls` with their `thoughtSignature` kept and returned, and
  tool results go back as `functionResponse` named after their call. Tools used
  to be accepted and dropped, so an agent on Gemini could not use any.
- A turn can be stopped without closing its session: `interrupt()` sends the
  CLI's own stream-json interrupt, the turn ends at once, the process and the
  conversation stay. A stopped turn is not retried and is stored with what it
  had said, `is_partial = true`. `POST /api/v1/sessions/desk-interrupt`,
  `POST /api/v1/sessions/{id}/interrupt`, `GET /api/v1/sessions/desks-in-progress`.
- Office may retire an idle session for a genuinely clean next provider
  context. The action emits `session.context_cleared`, carries no transcript or
  handoff forward, and is separate from restart/resume.

### Changed
- A model enabled in `sn_config.models` is allowed even when it is not on the
  built-in `ALLOWED_MODELS` list, on `/api/v1/chat`, `/chat/stream` and
  CLI sessions. `/chat/stream` no longer checks openrouter slugs, like `/chat`.
- `/api/v1/chat` picks the provider from the runtime segment of the model id
 : `claude-cli/sonnet` runs on the CLI as `sonnet`,
  `openrouter/openai/gpt-5` on OpenRouter as `openai/gpt-5`, and a CLI runtime the
  gateway cannot run is a 400 instead of a guess. It used to send any id with a
  slash to OpenRouter. For one release a bare name still means the CLI and a
  vendor-prefixed id still means OpenRouter. The stateless chat now logs the
  runtime (`claude-cli`) as the session provider. `/api/v1/sessions` accepts
  `claude-cli/<model>`, which rein sends from agent.yaml.

## [2.6.10] - 2026-09-16

### Added
- Opaque agent sessions now use the latest provider response's input plus cache
  read/write tokens as a context gauge. At the configured boundary the same CLI
  writes a durable handoff to the agent's Mesh workspace before the gateway
  completes and closes the old session.
- A new session for the same agent and conversation injects only the latest
  handoff on its first real turn. Older handoffs and provider transcripts remain
  available; transcript pruning is deliberately outside this change.
- Mesh or handoff generation failures leave the old session usable and emit a
  `session.handoff_failed` event instead of risking context loss.
- A route-level per-session turn claim spans the response AND its handoff
  maintenance. Overlapping user turns QUEUE behind it (as they used to queue on
  the CLI backend's send lock) instead of being refused, so a second message
  typed while the agent is working is never dropped.
- The person's answer is published as soon as the turn itself is done; the
  handoff runs after that, inside the same claim, so a threshold-crossing turn
  does not spend its sync window on a second full-context request.
- A turn that queued behind a handoff and woke to a retired session gets a
  structured `409 session_reset`; brin re-sends it on the clean session.
- A failed handoff backs off (time or context growth) instead of paying for a
  full-context generation on every following turn.
- Handoff tags are lowercased the way Mesh stores them, and the injection reads
  the document id recorded on the retired session instead of searching by tag,
  so a conversation id's spelling cannot silently cost a session its memory.
- Claude CLI receives a 200k auto-compact safety window. The product-managed
  handoff defaults to 120k and is expected to happen first.

## [2.6.7] - 2026-09-01

Credential tokens no longer appear in the systemd journal, for real.

### Fixed
- The FD-based fix in 2.6.5 failed on the live node: sudo closes all inherited
  file descriptors above 2 before exec, so the CLI received a dead FD number
  and reported "Not logged in". The approach was reverted and the journal leak
  resumed.
- Root cause: sudo logs the FULL environment it runs with (log_env on,
  !log_env unavailable in sudo 1.9.15p5). Any token in the env dict passed to
  create_subprocess_exec appears verbatim in the journal. Passing by FD did not
  help because sudo closed it.
- Fix (PATH B -- credential wrapper): credentials are popped from the env dict
  entirely before sudo sees it. A new Python wrapper script
  (claude-agent-wrapper, installed by gateway.sh) is what sudo calls instead of
  the CLI directly. The wrapper runs as the target agent user (who is a member
  of smart-node-agents), reads CLAUDE_CODE_OAUTH_TOKEN from
  /etc/smart-node/agent-runtime.env (root:smart-node-agents, mode 0640), and
  execs the real claude binary with the credential set. sudo's audit log sees
  only the wrapper path and non-credential env vars. The token never travels
  through sudo.
- The sudoers rule in gateway.sh now permits the wrapper path, not the CLI
  path. The wrapper path is written into the systemd unit as
  CLI_AGENT_WRAPPER_PATH so the gateway can read it without hardcoding it.
- This fix was tested on a throwaway stand before deployment, with both
  acceptance halves confirmed simultaneously: agent completes a real turn AND
  journalctl shows no sk-ant.

## [2.6.6] - 2026-08-31

A completed CLI turn now reaches the person even when the HTTP request that
started it has already timed out.

### Fixed
- `POST /api/v1/sessions/{id}/messages` ran the CLI turn as a directly awaited
  coroutine. If brin's 300s timeout fired first, the await was cancelled, the
  CLI process kept running to completion, and the answer was discarded: the
  model was billed, the work was done, and the person saw nothing.
- Fix: the backend turn is now run as an independent asyncio.Task. The HTTP
  handler waits on it via `asyncio.shield()` with a 285s window. If the CLI
  finishes inside the window, the response is returned synchronously (no
  behaviour change for fast turns). If the window closes first, the handler
  returns 202 {"status": "working"} and the task continues running; when the
  CLI finishes, the task writes the completed turn to sn_core.ai_session_messages
  via the existing `session_service.append_message()` path, and the desk history
  shows the answer on the next refresh. Fatal background errors are logged at
  ERROR and the session is marked failed; no silent data loss.
- `SN_SYNC_RESPONSE_TIMEOUT` env var overrides the 285s window.

## [2.6.5] - 2026-08-31

Credential tokens no longer appear in the systemd journal.

### Fixed
- When the gateway spawned the CLI as an agent's OS user via sudo, every
  credential in `--preserve-env` was written verbatim into the sudo audit log
  (journald), because sudo 1.9.15p5 on this node has `log_env` enabled and
  offers no `!log_env` suppression directive. On every agent wake, the full
  OAuth token appeared in `journalctl -u smart-node-gateway`.
- Fix: credentials are now delivered through pipe file descriptors instead of
  the environment. The CLI binary supports `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`
  and `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`. The gateway creates a pipe, writes
  the token to the write end, closes the write end, and passes the read FD via
  `pass_fds` in `create_subprocess_exec`. Only the FD number (not the token value)
  appears in the environment that sudo sees and logs. The read FD is closed in
  the parent once the child is running. The non-sudo path is unchanged.

## [2.6.4] - 2026-08-25

The same fix, on the path every agent actually runs on.

### Fixed
- 2.6.3 raised the per-line ceiling in `session_manager` and left
  `backends/cli_claude` on asyncio's 64 KiB default. That is the path the Runner
  drives for every agent wake: its session spawn passed no `limit=`, and its read
  guarded only `asyncio.TimeoutError`, so the overrun propagated raw. The CLI
  echoes the assembled system prompt back on its FIRST output line, escaped, and
  those prompts measure 27-35 KiB on our own nodes -- just under the default,
  which is exactly why this never showed here. On a node whose prompt was larger
  the first read failed, the wake died in about 80ms, the Runner marked the agent
  broken, and the node stopped doing autonomous work while `agent_runtime` stayed
  free and every service reported healthy. Found from outside, by the node it was
  happening to.
- `STREAM_LIMIT` now lives in `config`, one definition for both paths. It sits
  beside `SPAWN_ENV_KEYS`, which is there because these same two files held two
  copies of a spawn constant and drifted -- the OAuth token reached one and not
  the other, and nodes spawned a CLI that was not logged in.

### Added
- The spawn logs the system-prompt size every time, and warns within 4x of the
  ceiling. Not a limit, a gauge: nobody knew ours were half the default until
  somebody else's went over it.

### Note
- Capping the system prompt was considered and rejected. The ceiling is an
  artifact of the pipe reader, not of the model, and the same limit applies to
  EVERY output line -- so a cap would have left 2.6.3's original case, a turn
  quoting a document, failing exactly as before.

## [2.6.3] - 2026-08-25

Reading a document killed the session.

### Fixed
- `session_manager` spawned the CLI without a `limit=`, so each pipe kept
  asyncio's default 64 KiB StreamReader buffer while the CLI speaks one JSON
  object per line. A turn that quotes a document back clears that easily, and
  `readline()` then raised `ValueError("Separator is found, but chunk is longer
  than limit")` -- uncaught, so an ordinary read surfaced as a dead session.
  Reported from a node reading a ~15KB document, which is worth stating
  precisely: the document does not trip the limit; the turn quoting it back,
  escaped onto one line, does.
  - Both spawns now pass an explicit `STREAM_LIMIT`. It is far above any real
    turn and still a ceiling: an unbounded reader turns a runaway child into
    the gateway's own memory problem.
  - Both read loops catch the oversize line and say so, naming the limit.
    Raising the ceiling alone would only move the cliff to the next document.
  - The exception caught is `ValueError`, NOT `LimitOverrunError`: asyncio
    catches the latter inside `readline()` and re-raises the former. Written
    against the documented name, a test for this passes while the bug is live.
  - `tests/e2e_stream_limit.py` constructs the failing state and runs against
    `--baseline REF`, so the difference is demonstrated rather than asserted.

## [2.6.1] - 2026-08-21

Cached tokens were billed and counted nowhere.

### Fixed
- The usage mapping kept only the uncached input, so a turn displayed "8 in"
  beside a real $0.10. Extended the provider-agnostic usage shape with
  `cache_read_tokens` and `cache_write_tokens`; each backend translates its own
  provider's names (`cache_creation_input_tokens` / `cache_read_input_tokens` on
  the CLI path, `prompt_tokens_details.cached_tokens` on the OpenAI-compatible
  one). Nothing above the backend learns which provider it is talking to.
- The two providers differ in a way that is easy to double-count: Anthropic
  reports cached tokens BESIDE prompt input, OpenAI-compatible APIs count them
  INSIDE it. The OpenAI path therefore surfaces the figure without adding it back.
- `record_usage` rolls the pair up. Note `jsonb_build_object` REBUILDS the object,
  so a key absent from that list is dropped on the next write; the docstring now
  says so.
- Sessions recorded before this release have no cache figures and cannot be
  corrected: the data was never captured.

## [2.6.0] - 2026-08-14

### Changed
- An agent woken through the gateway now arrives as ITSELF. The gateway
  spawned every agent's CLI under its own service account, so on that path all
  agents shared one uid and per-agent group membership -- the entire access
  mechanism of a project work area -- applied to nobody. The value was already
  resolved and carried (`session_service` puts `agents.os_user` into session
  metadata) and then dropped at the last step; this picks it back up and drops
  privilege with `sudo -n -H -u <os_user>`, gated by a sudoers rule the
  installer writes: one binary, runas the agent GROUP, never root.
  - Verified on test-node during a live wake, in `ps` and in `/proc/<pid>/status`
    rather than from code: `Uid: 988` (agent-foreman), `Groups: 994 1001` --
    the project group present, a project it is not a member of absent. A member
    write lands with the project group by setgid; a non-member write is refused.
  - The credential is passed by NAME through `--preserve-env`, never on the
    command line, so it does not appear in `ps` to other agents on the node.

### Fixed
- Transcripts land in the AGENT's home instead of pooling in the gateway's, and
  the resume lookup follows them there. Reading `$HOME` was right only by
  accident while every CLI ran as the gateway; left alone it would answer "no
  transcript" for a session that has one, and the CLI refuses a re-used
  `--session-id` -- a 502 on the second message of every panel chat, invisible
  until a session is resumed. The failing state was constructed to be sure the
  check can fail: with the old lookup the node answered `Session ID ... is
  already in use`; with the fix a word planted before a restart came back after
  it.
- Every reclaim path now signals through the same privilege drop it spawned
  through. A process owned by another user cannot be signalled by the gateway at
  all -- measured, a direct kill answers "Operation not permitted" and the child
  lives on -- so idle sweep, turn timeout, cap eviction and shutdown would each
  have become a no-op that logs success: the orphaned-process leak with a clean
  log over it. Restarting the gateway now leaves zero agent-owned CLI processes,
  counted rather than assumed.

### Security
- `NoNewPrivileges` is `false` on the unit, deliberately. Dropping to another
  user goes through `sudo`, which is setuid, and that flag forbids exactly it.
  What can be gained is bounded by the generated sudoers rule: one absolute
  binary plus `kill`, runnable only as a member of `smart-node-agents`, and root
  is not a member.

## [2.5.2] - 2026-08-13

### Fixed
- The gateway owns the lifetime of the `claude` children it spawns. It
  did not, and the omission was structural rather than accidental: a live CLI
  process is kept after its turn ON PURPOSE so `--resume` can restore the
  conversation, and the only thing that ever reclaimed one was a NEW spawn
  hitting `SN_CLI_MAX_PROCS` and evicting an idle session. On a node where no
  fifth session arrives, the first four live forever. archive-node held exactly four,
  all children of one gateway, four days old, ninety minutes of CPU each,
  burning a steady 3-4% doing nothing. An agent genuinely waiting on an API
  costs ~0%; a flat 4% for days is a spin loop.
  - An idle session is now reclaimed on a CLOCK (`SN_CLI_IDLE_TIMEOUT`, 900s)
    rather than on cap pressure, swept every `SN_CLI_IDLE_SWEEP` (60s). A
    session with a turn in flight is never touched: its send lock is held, and a
    long turn is work, not a leak.
  - A hard wall clock for the whole turn (`SN_CLI_TURN_TIMEOUT`, 600s).
    `READLINE_TIMEOUT` bounds the wait for ONE line, which bounds nothing
    overall -- a child emitting a line every 299s satisfies it forever. The cap
    sits above brin's 300s and the Runner's 310s on purpose, so in normal
    operation the caller gives up first and this only catches a turn that has
    escaped everyone else.
  - Children are spawned with `start_new_session=True` and killed by PROCESS
    GROUP. `claude` has children of its own, and terminating only the parent is
    how they end up orphaned. Cgroup membership is unaffected, so systemd still
    sweeps the group on stop.
  - `shutdown_all()` on app shutdown: no child outlives the gateway. On a live node the
    orphans' parents were three DIFFERENT gateway pids, so restarts were leaving
    children behind.

### Added
- `/api/v1/health` reports `live_children`, the number of CLI processes actually
  alive. It is NOT the same number as `active_sessions`, and that gap was the
  bug: on a live node eight children outlived the sessions that owned them, so any answer
  to "how many agents are running" was wrong by eight.
- `tests/e2e_cli_child_lifetime.py` -- constructs each failing state with REAL
  child processes (so the group-kill path is genuinely exercised) and runs every
  scenario against the pre-fix module, refusing to pass unless the baseline
  fails.

## [2.5.1] - 2026-06-16

### Fixed
- Extend `SN_AGENT_SLUG` injection to the v2 `ClaudeCliBackend`
  (`backends/cli_claude.py` `_spawn`) -- the live agent-driven spawn path.
  v2.5.0 only patched the legacy v1 `session_manager`, which agent-driven
  sessions do not use, so the slug never reached the running agent. Now both
  paths inject it; verified a brin-routed agent session gets
  `SN_AGENT_SLUG=<slug>` from its `/srv/agents/<slug>` cwd.

## [2.5.0] - 2026-06-16

### Added
- Agent identity injection: when a CLI session runs in an agent's own directory
  (`/srv/agents/<slug>`), the spawned Claude process now gets `SN_AGENT_SLUG=<slug>`
  in its environment. Node tools (e.g. the planner CLI) read it as the caller
  identity (Remote-User), so attribution is sourced from the runtime, not the
  prompt -- the model can neither forget nor spoof who it acts as. Applied at
  both spawn sites (`create_session`, `_respawn_session`) via `_inject_agent_slug`.
  Non-agent sessions get no slug, so identity-requiring tools fail closed.

## [2.4.3] - 2026-06-16

### Removed
- Dead v1 SessionManager routes that only fed v1/v2 confusion:
  `POST /api/v1/sessions/{id}/chat/stream` (zero consumers; targeted the v1
  registry, never saw v2 sessions) and the duplicate v1
  `POST /api/v1/sessions/{id}/restart` (v2 already served it; clears the
  "Duplicate Operation ID" startup warning). Remaining v1 routes stay until their
  consumers migrate to v2.

## [2.4.2] - 2026-06-15

### Added
- `GET /api/v1/sessions/v2?conversation=desk:<id>` -- filter sessions by their
  `metadata.conversation` tag. Lets the dispatcher find a desk's existing session
  in durable storage and reuse it instead of spawning a fresh, context-less one
  after a restart. This prevents context loss across restarts.

## [2.4.1] - 2026-06-15

**Kill the session disconnect failure family (524/502/"already in use").**

### Fixed
- claude-cli backend autorespawn now uses `--resume` when a transcript already
  exists on disk, and `--session-id` only for a brand-new session. The old code
  always respawned with `--session-id`, so the second send after the process
  died re-ran `--session-id <id>` over an existing transcript and the CLI aborted
  with `Session ID is already in use` -> `EOF before result` -> 502. New helper
  `_transcript_exists()` decides whether to resume, avoiding resuming a session with no transcript.
- `create()` no longer terminates a process that is mid-turn. A turn holds the
  session `_send_lock` while streaming stdout; a concurrent `/restart` (or
  `/resume`) used to kill that process, the reader got EOF -> `EOF before result`
  -> 502, and the UI retried with another restart -- a death-spiral. `create()`
  now refuses to respawn over a live turn (raises) when the send lock is held;
  the autorespawn path is unaffected (it only respawns a DEAD process).

## [2.4.0] - 2026-06-14

**Concurrency-safe sessions + crash recovery.**

### Fixed
- Per-session send lock in the claude-cli backend: concurrent turns on one
  session no longer collide on `proc.stdout.readline()` (the "readuntil() called
  while another coroutine is already waiting" 502 race). Restores the
  per-session `_chat_lock` guarantee from the v1 SessionManager.
- Forward `CLAUDE_CODE_OAUTH_TOKEN` to the CLI subprocess. The node login flow
  (`claude setup-token`) mints an OAuth token (`sk-ant-oat...`) that the CLI
  accepts ONLY via this var -- it is rejected as `ANTHROPIC_API_KEY`. Without
  forwarding it the CLI ran unauthenticated and every reply was empty.

### Changed
- Transient send errors (read timeout / lock contention) return `409` and leave
  the session active+resumable instead of marking it `failed` (was: any error
  -> `failed` + `502`).
- `POST /sessions/{id}/restart` reactivates a `failed`/`idle_released` session
  after a successful `--resume`, so the next message is accepted (not `409`d).

## [2.3.0] - 2026-06-14

**Restartable CLI sessions (reload config without losing the conversation).**
A session's `claude` process can now be restarted in place to re-read its
`.claude` config (CLAUDE.md, `.mcp.json`, settings) and system prompt, while the
conversation is preserved. This backs a "Reload" button in the Office UI.

### Added

- `POST /api/v1/sessions/{session_id}/restart` (v2, session-first path) -- the
  one the Office UI uses. Resolves the backend; for opaque (claude-cli) sessions
  it restarts the process via `ClaudeCliBackend.restart()` (resume + fresh
  fallback). No-op for stateless backends. Returns `{status, pid, resumed}`.
- `ClaudeCliBackend.restart()` (v2) and `SessionManager.restart_session()` (v1
  `/api/v1/sessions/{id}/restart`) -- public restart with `--resume`, falling back
  to a fresh process when the session has no on-disk transcript yet.

### Changed

- Both CLI backends now spawn `claude` with `--session-id <session_id>` so Claude
  Code persists the session transcript to `~/.claude/projects/<project>/`, making
  `--resume` restarts continuity-preserving. The gateway session id doubles as the
  Claude Code session id (both UUIDs).
- v2 `_spawn` and `create()` gained `session_id` + `resume`; v1
  `_respawn_session` gained a `resume` flag. Auto-respawn on crash keeps the
  previous fresh-restart behaviour.

## [2.2.0] - 2026-06-14

**CLI agents read their MCP config.** The claude-cli backend now runs the CLI in
the agent's own directory, so it loads the `.mcp.json` that core-api renders from
the agent's MCP grants -- giving opaque (claude-cli) agents their granted MCP
tools natively.

### Changed

- `session_service.py` -- `create_from_agent` sets `metadata.cwd` to
  `$SN_AGENTS_ROOT/<slug>` (default `/srv/agents/<slug>`) when the directory
  exists; the claude-cli backend already prefers `metadata.cwd` over
  `CLI_DEFAULT_CWD`, so the agent's `.mcp.json` is finally read.
- `sessions/v2` resolves the backend from the `sn_config.models` catalog so
  callers stay provider-agnostic (commit 9133ef5, previously untagged on 2.1.0).

### Ops

- Gateway must run with `SKIP_PERMISSIONS=true` for CLI agents to load MCP
  servers non-interactively (headless subprocess otherwise blocks on approval).

## [2.0.0] - 2026-05-27

**Repo extraction + session-first gateway v3 foundation.** Previously vendored
in the installer repository as `companions/ai-gateway/`; now its own repo.

### Added

- `backends/` -- new pluggable backend interface
  - `backends/cli_claude.py` (Claude Code CLI backend)
  - `backends/http_openrouter.py` (OpenRouter HTTP backend)
- `budget.py` -- per-tenant/agent/scope budget enforcement (reads from `sn_config.budgets`)
- `db.py` -- Postgres pool wrapper, peer-auth via pg_ident map
- `session_service.py` -- SessionService API (lifecycle of `sn_core.ai_sessions`)
- `sessions_v2_routes.py` -- `/api/v2/sessions/...` endpoints (session-first contract)

### Changed

- `main.py` mounts the new v2 routes alongside the legacy v1 surface; both coexist during the v1 -> v2 migration window
- Service unit moved from repo-root `ai-gateway.service` to `systemd/smart-node-gateway.service` (canonical companion layout)

### Notes

- The legacy `providers/` package (anthropic/gemini/openrouter direct providers) is still in-tree and used by v1 routes. v2 funnels everything through `backends/`. providers/ removal is gated on v1 endpoint deprecation.
- Today the `companions/gateway.sh` installer script generates the systemd unit inline. The `systemd/smart-node-gateway.service` shipped here serves as the canonical reference for the pending P3 host-level migration.

## [1.0.0] - 2026-01-21

Initial release as `ai-gateway`, vendored inside the installer.

### Added

- FastAPI service with persistent CLI sessions
- Session management API (create, chat, close, list)
- Python client library (`client.py`) with async support
- One-shot `chat()` method for simple queries
- Multi-turn `send()` method for conversations with context
- Context manager support (`async with AIGateway()`)
- IP whitelist middleware (VPN + server access only)
- Traefik routing via https://ai.example.com
- Systemd service (`ai-gateway.service`)
- SSE streaming endpoint for real-time responses
- Health check endpoint
- README with migration guide from claude-wrapper

### Technical

- Uses Claude CLI `--print --verbose --input-format stream-json --output-format stream-json`
- Session timeout: 1 hour
- Max concurrent sessions: 10
- Listens on 172.17.0.1:19850 (Docker bridge)
