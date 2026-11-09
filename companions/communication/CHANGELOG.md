# Changelog

All notable changes to `smart-node-communication` go here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are semver. Tags on `main` correspond to the `VERSION` file.

## [Unreleased]

### Fixed
- An attachment that arrives by mail can be opened by the agent it was sent
  to. Its body line is the composer marker brin rewrites into the fetch
  command (`[attachment: name, mime, N bytes, id UUID]`, MIME parameters
  dropped) instead of "[attachment: name (32 KB, mime)]", which nothing
  parsed. And the ingest runs the scan stage before the attachment rows are
  written, as an office chat upload does: a sender the contact gate trusts
  (contacts.may_wake) gets `approved`, anyone else `received`, a gate that
  cannot answer approves nothing, and each row is journaled as
  `attachment.stored`. The rule is restated in common/attachment_scan.py and a
  test fails if it answers differently from core-api's.
- A chat message is answered however long the turn takes. Fast dispatch gave
  up on brin after 120 s, under the gateway's own 285 s window, marked the
  message failed while the agent went on working, and the answer had nowhere
  to go. It now asks brin for the whole turn (`wait_seconds`,
  `DISPATCH_TURN_WAIT`, default 3600, the turn limit) and waits a minute
  more; the agent's claim is renewed while the turn runs, so the lease stays
  short for a dispatcher that dies.
- One agent's long turn no longer holds every other agent's chat: each turn
  runs in its own thread, one per agent, and the scan goes on beside it.
- What a person writes on a chat channel while the agent is busy is read in
  the agent's next turn, together, oldest first, and answered once. Before,
  each message was a turn of its own. Email is not batched.
- A final answer of exactly `NO_REPLY` completes the turn and delivers
  nothing, for one message or several read together. Fast dispatch had no
  silent outcome: any closing text went to the whole thread and an empty
  answer failed the turn. Text that merely mentions NO_REPLY is delivered.

### Fixed
- Fast dispatch blocks mail with multiple enabled receivers before calling an
  agent or processing machine mail. It emits a routing-denied event and never
  selects the first owner or falls back from an ambiguous address. Owner lookup
  is channel-specific; contact checks also reject ambiguous ownership. History
  watermark lookup aggregates the earliest boundary without choosing an owner.
- A fast-channel turn no longer queues its final internal summary after it has
  already replied through the messaging tool. Dispatch suppresses that second
  row only when the gateway reports an explicit send for the same external
  channel and the bus confirms a new committed `comm-send` row to the current
  person; ordinary final replies, internal handoffs, and failed tool calls are
  unchanged.
- Budget-limit email to the registered owner is treated as a narrowly scoped
  system obligation rather than an agent-originated contact decision. Both the
  enforcing contact gate and its report-only predecessor verify that the target
  is user 1's enabled email channel before allowing the exemption.
- Mailbox adoption resolves credentials through the core-api Unix socket, where
  the kernel proves the root installer process. Loopback TCP is no longer used
  as an identity boundary for secret reads.

### Added
- The installer publishes `/etc/smart-node/budget-notifications.env` for
  core-api and gateway. `SN_NOTIFY_SENDER` is selected only from an installed
  mailbox credential; an explicit uncredentialed address is refused.
- **Outbound dry-run suppression**. Two new environment variables:
  `DRY_RUN_AGENTS` (comma-separated agent slugs) and `DRY_RUN_CHANNELS`
  (comma-separated channel types, e.g. `email`). When a match is found
  `send_outbound.py` advances the message row to `dry_run` status, logs the
  composed content, and returns without dialling any transport. The flag is
  set by the operator; the agent body cannot override it. Both email and all
  other channels pass through the same suppression path.

  The `dry_run` status has been added to the `message_state` CHECK constraint
  (migration 029, `B02-sn-core-schema.sql`, `B02-sn-core-seed.sql`). A
  `message.dry_run` event is emitted to `sn_core.events` so the decision is
  readable from the journal without inspecting the row status.

## [1.9.1] - 2026-08-25

Two installer defects surfaced with hyphenated domains and repeat installs.

### Fixed
- **A hyphen in the mail domain made the installer reject the accounts file it
  had just written**. Key derivation replaces only `@` and `.` with `_`,
  so an address at a hyphenated domain keeps the hyphen in its key
  (`hello@example-co.com` -> `PW_hello_example-co_com`) -- while both validators
  gated on `PW_[A-Za-z0-9_]+=`, which has no hyphen.
  The install died claiming the file held no keys. The validator was the wrong
  side to blame: nothing sources this file as shell (only the Python parsers
  read it), and changing derivation would have orphaned every key already
  written on an installed node. Widened at BOTH sites -- the pre-install gate
  and the doctor check -- since fixing one leaves the install passing while the
  doctor still calls the node broken.
- **A companion re-run silently stripped the business name from outbound mail**.
 `send_outbound.py` reads `SN_BUSINESS_NAME` from `.env`, but no  installer input ever wrote it and `.env` is rewritten wholesale on every run,
  so the second run dropped it: outbound lost its From display name and its
  signature, and core-api's notifications quietly substituted "Smart Node"
  rather than failing. Both halves fixed: a new `SMART_NODE_BUSINESS_NAME`
  input, and the existing `.env` value carried forward when the input is
  absent. The carry-forward is the half that repairs nodes already installed.

## [1.8.3] - 2026-08-22

### Fixed
- The outbound pass appends the message reference to the PLAIN part, next to
  where the brand template puts it in the HTML footer. Previously only
  HTML was guaranteed to carry one: a plain-text reader of a reply got none, and
  a notification got two whenever the template fetch failed and the HTML fell
  back to wrapping a body that already contained it.

## [1.8.2] - 2026-08-18

### Fixed
- `tools/comm.sh` had the same defect as the messaging MCP: it called core-api
  at `$SN_CORE_API_URL` with a `Remote-User` header, which is refused for an
  agent identity, so every command failed with 403 -- the secretary could not
  read the inbox this tool exists to work. It now calls the core-api unix
  socket (`$SN_CORE_API_SOCKET`), and refuses to fall through to the address,
  because a 403 here reads to the agent above as an empty inbox.

## [1.8.1] - 2026-08-16

### Added
- The installer grants the service user `SELECT, INSERT` on
  `sn_core.message_identity`, so the telegram and email adapters can
  stamp one identity verdict per inbound message once the arrival-time stamping
  lands (next stage). INSERT only -- the row is written once, at arrival, and
  never revised.

## [1.8.0] - 2026-08-11

### Added
- **A connected mailbox is no longer an inbox of everything it already holds**.
 Binding a channel instance to an agent takes a watermark  (`agent_channels.history_before`); anything that arrived at or before it is
  recorded as HISTORY -- readable, and never routed to an agent for a reply.

  The incident: the configurator bound a real business inbox with months of
  accumulated mail, the poller treated the whole backlog as fresh inbound, and
  the agent did what it is supposed to do with inbound mail. One reply carrying
  the node's internal status reached an outside party; a second was queued
  behind it.

  No existing guard could have stopped it, and each for its own reason. The
  egress guard allowed it correctly -- they had written to us. The machine-mail
  filter classified it correctly -- a marketing blast is human-sent. Nothing
  anywhere asked "is this from BEFORE this mailbox was connected?" Age is not
  permission: a correspondent who wrote in May is legitimately allowed, and
  their May question still got answered as if it arrived today.

- `adapters/email/transport.py` now reads the message's real arrival time --
  IMAP `INTERNALDATE` first (the server's own record, which the sender cannot
  influence), the `Date` header second. Until now the only timestamp anything
  downstream had was when we happened to poll, which is what made a backlog
  indistinguishable from mail that arrived a minute ago.

- `common/binding.py` -- the watermark lookup and the rule, in one place. The
  decision is made at INGEST and nowhere else: both consumers (the fast-dispatch
  loop and the Runner's wake path) select on `message_state.status = 'new'`, so a
  message that is never 'new' is never routed by either, with no second guard in
  either of them.

### Changed
- `db.record_inbound` takes `received_at` and `history`. The row and its
  terminal state are now written in ONE transaction: a row committed as 'new'
  and corrected a moment later is a row a consumer may already have picked up.
- An undatable message is recorded as history, not as new. Failing that way
  leaves a silent agent and a row that says why; failing the other way leaves
  the node.

### Notes
- Requires the schema change in `bootstrap/B02-sn-core-seed.sql`
  (`agent_channels.history_before`, and `'history'` added to the
  `message_state` status set). **Re-run B02 on an installed node** -- the schema
  is baked, not migrated. Reference copy: core-api migration 022.
- Tests: `tests/test_history_watermark.py` (17 cases). Verified to fail with the
  guard removed.

## [1.7.1] - 2026-08-11

### Removed
- `adapters/email/adapter.py::deliver()`, the second exit. It had had no
  callers now made `send_outbound` the sole transmitter, and carried its
  own DO-NOT-CALL warning naming the incident: a node whose allowlist forbade
  everything mailed seven external addresses through exactly this route, because a
  second exit is not a duplicate of the first, it is a path with no egress guard on
  it. It also sent before it recorded, so a crash between the two statements put
  mail on the wire the registry never heard of. A tombstone comment replaces it.

## [1.7.0] - 2026-08-05

**A whole domain can be granted, and only by saying so.** The last undecided item on the earlier change chain.

A party that writes from four addresses at one company was either four catalog rows to keep in step, or a refusal for whichever address nobody thought of. The open question was never whether to allow it but how, without leaving two matchers that can disagree.

A handle stored as `@customer.com` -- an at-sign and nothing before it -- covers every address at that domain. A normal address never becomes a pattern, so the widening is something an operator states, never something that happens by accident.

### Added

- `handles_matching(channel_type, normalised)` -- every catalog handle that would cover an address, most specific first. One list, used by both `decide()` and `may_wake()`, which is what keeps the outbound and inbound answers identical; the query moved from `= %s` to `= ANY (%s)` and nothing else about it changed.

### Deliberately not widened

- **Subdomains.** `@customer.com` does not cover `@mail.customer.com` -- a different host, often a different party. Grant it separately.
- **A bare domain.** `customer.com` without the at-sign stays a literal handle, so pasting a website into the address book grants nothing.
- **Plus-addressing under an address grant** still does not match, as before. Under a *domain* grant it does -- not the stripping rule reversed, just a wider grant that was chosen on purpose.

## [1.6.1] - 2026-08-05

**Mail that arrived through an alias routes to the box it landed in.** Found within the hour of 1.6.0, by forwarding `info@example.com` to an agent's own mailbox and watching every message dead-letter.

Routing asked one question -- which agent is bound to the address in the `To` header -- and for a forwarded message that address is the alias, which is bound to nobody. So `no agent bound to email recipient 'info@example.com'` was raised for mail sitting in a mailbox this node owns, polls and holds the password for. True of the header, false of the message.

The row already carried the answer: `channel_id` is stamped with the mailbox the poller actually fetched from. Nothing needed to be recorded, only read.

### Fixed

- `_resolve_mailbox(recipient, channel_id)` replaces `_resolve_agent(recipient)`: the header is tried first, so a directly-addressed message still routes by who it was addressed to, and the receiving box is the fallback. It returns the address that resolved, and the gate, the reply identity and the routing decision all use that one value -- three things that must never disagree about which mailbox this is.
- The reply now leaves as the box the mail was fetched from rather than the alias. **Visible consequence:** someone who writes to `info@example.com` is answered from the agent's own address. Answering as the alias would mean authenticating as an address the node may hold no password for.

## [1.6.0] - 2026-08-05

**An agent is woken only by a sender it was granted.** This builds on the gate from 1.5.0.

1.5.0 gated the way out and deliberately left the way in open, so that a business inbox could never miss a new customer. That reasoning is still right for a public front door, and it is still the default. It is the wrong posture for a node whose mailbox was just connected to an address that already existed: binding `info@example.com` replayed that mailbox's entire history as live inbound, the agent worked through it, and two replies carrying internal status reached a marketing sender before anything was stopped. Every one of those messages was legitimately delivered, legitimately routed, and answered by a model the owner paid for.

So the two questions are now separate. **May this message arrive** -- always yes; the poller reads every mailbox and the registry records every message, unchanged. **May it wake an agent** -- only if its sender is a contact granted to the agent that owns the receiving mailbox.

### Added

- **`CONTACT_GATE_INBOUND`**, its own switch, defaulting to off. The outbound gate and the inbound gate answer different questions, and an operator may reasonably want deny-by-default going out while still hearing everyone coming in -- which is exactly the 1.5.0 posture, so it stays what you get by not choosing.
- **`contacts.may_wake()`** -- the inbound verdict, over the same catalog and the same query as `decide()`, with the node's own address and the other party's swapped. That flip is the one subtlety in this release: outbound, the node's address is the sender; inbound, it is the recipient. Calling `decide()` with inbound arguments would ask whether the *stranger's* address owns a mailbox on this node, which is never true, so everything would be refused with a plausible-looking reason. Two entry points exist so that cannot be gotten wrong at a call site.
- The verdict is journalled as `message.blocked` whether or not the switch is on, so a node can be watched declining to wake before it declines anything.

### Changed

- `dispatch` asks the gate at the wake point, after the agent is resolved and before brin is called -- the one place on the fast path where a model is about to be billed. A refused message goes to `blocked`, not `processed`: both mean "not answered", and only one of them says a policy decided it.

### Known gap

Where email is not a fast channel, the agent is woken by core-api `/with-pending-work` instead, which this does not touch. On such a node the inbound gate does nothing. Recorded rather than papered over; it needs the same pass on the core-api side.

## [1.5.0] - 2026-08-04

**A recipient must be a contact granted to the sending agent.**

Deny by default, decided by the owner after a node answered a marketing blast with its own internal status -- the agent address, the voice endpoint, an invitation to continue the business interview. The egress guard permitted it correctly: it could only ask "did they write to us first", and they had. Having written to us is not consent to be answered.

### Added

- `common/contacts.py` -- the decision, and only the decision. Given a channel, a sending address and a recipient, it resolves which agent owns that address (`agent_channels.instance_ref`, the same rule the rest of the code uses, so there is no second notion of who sent a message) and answers whether that agent holds a grant covering the recipient on that channel. Its own module because the design calls for an independent screening pass over the registry; calling it inline today must not mean rewriting the rule when it moves.
- `db.emit_blocked` -- `message.blocked`, its own event type. A policy refusal and a dead SMTP server are different facts with different fixes, and until now both reached the journal as `message.failed{stage: transmit}`, so "what did we decline to send, and to whom" had no answer. Carries `enforced`, so a report-only verdict is distinguishable from one that stopped something.
- `message_state` gains `blocked` (schema, canon side).

### Changed

- `send_outbound` consults the gate before dispatching to any channel sender. Refusal marks the row `blocked` with the reason, journals it, and the sender never runs.
- **`CONTACT_GATE` defaults to OFF, and off is not silent**: the verdict is journalled either way, so a node can be seen refusing before it actually refuses. Switching enforcement on before contacts can be managed leaves a node able to write only to whatever was seeded by hand.
- Matching is per channel. Email pulls the address out of whatever the recipient field holds (`"Name" <a@b>` is the ordinary case) and compares case-insensitively, with both sides normalised inside the query -- so a contact typed `A@B.com` still matches mail for `a@b.com` and there is no write-time cleanup step to forget. Plus-addressing is deliberately NOT stripped: stripping widens what an agent may reach, and for a deny-by-default gate the safe direction is narrow.
- Internal agent-to-agent traffic is exempt by KIND rather than by seeded grant rows, so the working loop does not depend on a catalog nobody has filled in yet.
- **The old egress guard is superseded.** It now stands down whenever the gate enforces, instead of running beside it: two policies answered the same question differently and caused a delivery conflict. It is deleted once every node has the gate on, and status is corrected then. Until then it is what stops a report-only node from having no egress control at all.

### Known before switching on

An agent with no grants goes silent. Seed the contacts for whatever must keep working first; the switch is `CONTACT_GATE=on` in the companion `.env`, which the installer now writes with that warning beside it.

## [1.4.0] - 2026-08-04

**The node has one exit again.**

The accepted outbound rule says an agent does not send: it drops its reply in the outbox, and a separate service delivers it. That single step is what the safety envelope stands on -- "the only thing we control is WHAT lands in the outbox and WHETHER it is released." A shipped node had no such service. `communication.sh` installed the telegram daemon, the dispatch daemon and the email poll timer, and never installed the outbound sender, although this repository shipped both its unit files and code calling it the sole transmitter.

Nothing errored. A telegram or slack reply was composed, recorded, and never sent. Anything core-api queued for `/api/messages/send` or `/reply` returned 200 and stayed queued; field-node row 23 was still sitting there when this was written. Email escaped only because the fast path transmitted inline -- a second exit with no egress guard on it, which is how a node whose allowlist forbade every external address sent seven of them.

The rule was never decided away. the inline-delivery proposal is `Status: Proposed` and bounded itself with "Email is never included"; shipped nodes ran `FAST_CHANNELS=email` with no sender at all. It was written the same day the accepted rule put the guard at a chokepoint that inline delivery removed, and does not reference it once.

### Fixed

- **The outbound sender is installed, enabled and verified on every node.** Not gated on the email channel: a telegram-only node needs its replies to leave just as much. Five verify checks assert it, because the failure it prevents is silent and an install reporting N/N proved nothing about it before.
- **`_deliver_email` no longer transmits.** It records the outbound row and returns, symmetric with `_deliver_telegram`, which has queued rather than sent all along ("send-outbound is the SOLE transmitter. Sending inline here too double-sends"). The reply now leaves through the timer like every other channel.
- **The reply's identity is recorded, not inferred.** The outbound row's `sender` is the mailbox that actually received the message, which is the identity the sender authenticates as, and the same address the multi-mailbox poller stamps on the inbound row. Previously the row carried whatever single mailbox the environment named -- the shape behind mail addressed to one box coming back signed by another.
- **The Message-ID is minted when the row is written** and stored as its `external_id`, so the registry and the wire agree. Minting it mid-send is only possible when the send is inline.

### Changed

- **The egress guard allows replies to known correspondents and configured recipients**, and it now sits on the only path out. Two ways to be allowed: the recipient wrote to us first on this thread (answering a correspondent needs no configuration), or `EMAIL_ALLOWED_DOMAINS` / `EMAIL_ALLOWED_ADDRESSES` covers them (the only way to reach someone who did not write first). This is what an unset allowlist now means, and the question had to be answered: under the old reading -- unset refuses everyone -- routing email through the guarded path would have stopped a shipped node answering anyone at all. The canon had already chosen: "a static whitelist alone would wrongly block real customers, so reply-to-the-known-correspondent is the rule."
- Order is now write-then-wire on every channel. The inline path called `transport.send()` and only then `record_outbound()`; a crash between the two put mail on the wire the registry never heard of.
- `adapters.email.adapter.deliver()` has no callers and is marked do-not-call. It still transmits, which is the hazard this release removes; it is a candidate for deletion once a clean node has proved the new path.

### Not in this release, deliberately

- `FAST_CHANNELS=email` stays. the accepted outbound rule forbids it, but today the fast path is the only thing that makes email work on a shipped node; removing it makes email depend on the wake loop, whose email path is unproven. This release changes the exit and leaves intake alone.
- The outbound guard itself (the `held` state, the content rules) is not built. No `held` state exists anywhere -- the schema allows only `new|routed|processed|failed`. This release creates the one place it can stand.

## [1.3.0] - 2026-08-04

**The message path now writes to the event journal, and stops lying about what it delivered.**

The node's own architecture glossary defines the journal as the way to answer what happened to a message: `message.inbound -> agent.woken -> message.sent -> message.transmitted`. On a shipped node it held none of them. A reader following our documentation would query, find nothing, and conclude no mail had ever arrived. A misrouted turn -- addressed to one agent and answered by another -- cost an evening of matching session ids by eye across two log files, and every fact needed belongs here.

### Fixed

- **A delivered reply no longer sits in the outbound queue forever.** The fast-dispatch path transmits inline (`adapters.email.deliver` sends, then records the row), and only the `send-outbound` timer -- which the installer does not wire -- ever advanced a row past `new`. On field-node six emails that had genuinely been delivered were still queued, and wiring that timer later would have re-sent every one of them. The transmit points now settle their own rows, idempotently, so the queue means what it says and installing the timer becomes safe.

### Added

- `message.sent`, carrying the agent that actually composed the reply. That field is precisely what was missing when that misrouted turn was diagnosed; the dispatch layer knows it and nothing downstream can recover it, so it is recorded at the point of composition or not at all.
- `message.transmitted` from BOTH transmit points. It existed, but only in the timer path that shipped nodes never run.
- `message.failed`, carrying the stage: `route`, `think`, `deliver`, `transmit`. Those fail for unrelated reasons and are fixed in different components; which one it was is the first question anyone asks, and `message_state.error` cannot answer it.
- `message.routing_denied` for the two deliberate non-routings -- no agent bound to the channel instance, and machine mail recorded as evidence but never answered. Without it a decision and a fault look identical from the journal, and both look like nothing happened.

### Changed

- All emitters go through one `_emit` in `common/db.py`. It exists to get right, once, the two things that were got wrong: the `sn_core.actors` self-registration needs SELECT as well as INSERT (`ON CONFLICT` must read the conflicting row, while `has_table_privilege(...,'INSERT')` cheerfully answers true), and a failed emit warns instead of being swallowed. Three hand-rolled copies of that INSERT pair are now one.

## [1.2.0] - 2026-08-02

**Sending identity and loop guards.** Two defects found by the node mail audit, both silent in production.

### Fixed

- **A reply now leaves as the mailbox that received the message.** The dispatch delivery path built its transport from whatever identity the `.env` happened to name, so a node with several mailboxes answered everyone as one of them: mail addressed to a customer-facing mailbox went out signed by an unrelated one (3 rows in `sn_core.messages`, 2026-08-02 14:56). The deliverer contract now carries `recipient` (the node's own channel instance) and `_deliver_email` applies that mailbox's credentials before sending. No password for the box means no send, because the wrong identity is accepted by the server and therefore fails silently.

### Added

- `common/mailcreds.py` -- the per-mailbox environment swap, in one place. It existed twice (poller, outbound sender) and was missing exactly where it mattered.
- `common/mailrules.py` -- when not to answer. Auto-generated mail (Auto-Submitted, Precedence, X-Failed-Recipients, a literal null Return-Path, List-Id, or a system sender local-part) is recorded but never answered; a reply is never addressed to a system mailbox; and a per-thread ceiling (5 in 60 minutes, counted in the registry) catches loops the first two cannot see. mail-node had answered mailer-daemon bounces three times, each bounce reaching the model.
- The ingest stores those headers plus its verdict in the row metadata, so the reply decision survives whichever path picks the row up (fast dispatch or the Runner wake-loop).

## [1.0.0] - 2026-05-27

**Repo extraction.** Previously vendored in the installer repository as `companions/communication/`; now its own repo. State at extraction: live b08dev `/opt/smart-node/communication/`.

### Layout

- `dispatch.py` -- registry → brin → reply loop (long-running daemon)
- `adapters/email/` -- email adapter (oneshot, polled by systemd timer every 30s)
- `adapters/telegram/` -- telegram adapter (long-running daemon)
- `common/` -- shared helpers (DB, message envelopes)
- `systemd/`:
  - `smart-node-communication-dispatch.service`
  - `smart-node-communication-email.service` + `.timer`
  - `smart-node-communication-telegram.service`

### Notes

- The voice adapter previously lived here (`adapters/voice/`) but was extracted to a separate runtime at `/opt/smart-node/voice` (see `smart-node-voice` repo). The voice systemd unit (`smart-node-communication-voice.service`) is still prefixed `communication-*` for historical routing but actually drives `docker compose` at the voice dir -- it will be moved to the voice repo when communication is split.

- The companion installer (`communication.sh` in the installer repository) still generates systemd units inline. `systemd/` in this repo serves as the canonical reference for the P3 host-level migration.

### Known issue at extraction

- Bounce-loop guard: dispatch.py SCAN_SQL needs filter to skip `mailer-daemon@` senders. Confirmed during stress test 2026-05-26. One-line fix. Not yet applied.
