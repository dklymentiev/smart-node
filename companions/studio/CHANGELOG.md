# Changelog

All notable changes to `smart-node-office` go here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- A comment box on the task card, right under the description, with the
  comments newest first below it, so a question and its answer are both in view
  without scrolling. `@name` tells that actor through the planner's mention
  notice; the name is lower-cased before posting, since actor ids are lower case
  and `@Dana` would tell nobody. While the card is open, new comments are fetched
  every 4 s (tab visible only) and put on top without redrawing the card. Each
  comment has Reply, which quotes it and mentions its author. Pinned by
  tests/office-task-comment-box.test.sh; scripts/browser-check.mjs posts one
  (answered in the page, nothing is written) and checks Reply.
- The office installs as an app on a phone or a computer (PWA): a manifest
  (public/manifest.webmanifest, "Office", standalone), icons from the sidebar
  mark (public/icons/), and a service worker (public/sw.js) that keeps only an
  offline page (public/offline.html), shown when the node cannot be reached. It
  keeps nothing else: the shell and every /api answer come from the node each
  time. office.sh routes these files past smart-node-auth (they carry no data;
  the browser fetches the manifest and icons without the session).
  test/pwa.test.mjs checks the files; scripts/pwa-check.mjs checks in Chrome
  that the office is installable and the offline page works.
- Unread counts in the chats (public/js/chat-unread.js, its decisions in
  chat-shell-model.js with tests): a badge on each chat row (an agent's row adds
  up its open chats) and on the chats in an agent's menu, the total on the Chats
  icon and in the page title, "(3) Office", and an Unread filter beside All and
  Working on a desktop. The counts come from core-api's GET /api/desks/unread
  every 8 s while the page is in view, and bring each row's last line and its
  place in the list up to date with them. The chat on screen in a focused window
  is marked read up to its newest message (PATCH /api/desks/{id} {read_upto}), so
  the phone and the computer agree. The office proxies GET /api/desks/unread;
  scripts/browser-check.mjs checks the badges, the filter and the read marks.

### Changed
- The Task statuses editor (wait reasons, close outcomes) moved from Settings to
  the node panel's Planner page: they are planner settings. The task list's
  status filter still reads them. Settings has no card for them. An admin
  (super-admin or admin, from /api/me's new role) gets a Node panel card that
  opens the panel.
- The task list filters by who does it with a row of chips over the list, one
  per assignee (who it is assigned to, not who filed it) with a task count,
  Unassigned last; several can be
  on at once, Anyone clears them, and the choice is kept across visits. It was a
  text field where the name had to be typed exactly.
- Links in written text (chat messages, task descriptions and comments,
  documents) are the theme's slate (--sn-work, the colour of the working mark),
  brighter on hover. They were the text's own grey and merged into it.
- On a desktop the chats work as in a messenger too (public/js/chat-shell.js,
  its decisions in chat-shell-model.js with tests): the sidebar is a rail of
  section icons, the chats are a column on the left with search and All /
  Working filters in place of the row of tabs, the chat has a header (the agent
  or every agent who took part, what they are doing, the agent's other chats,
  Chat/Canvas), and a panel on the right shows the agent's card (now, open tasks,
  chats) or, in a chat of several agents, who is in it. The panel folds away
  with its cross or (i) and stays folded. scripts/browser-check.mjs checks both
  layouts in headless Chrome against a running office.
- A message's actions are its menu: the right button on a computer opens
  Reply, Copy as text and Copy as Markdown at the pointer, with Quote selection
  and Copy selection first when part of the message is selected (Shift and a
  link keep the browser's menu); a phone keeps the long press. The Copy and
  Reply row under each message is gone: under a long message it sat by the
  composer, far from the text.
- On a phone the chats work as in a messenger (public/js/phone-chats.js, loaded
  after app.js): "/" is a full-screen list of the chat tabs (avatar, name, last
  message, time) and a tap opens one chat full-width with a back arrow to the
  list; the system back returns to the list too. New chat is a screen of its
  own with a close cross, and any popover closes on a tap outside it. Before,
  the chats were a row of tabs at the top, scrolled sideways, and the agent
  picker could only be closed through a thin strip under it. The desktop is
  unchanged.
- A reply in flight is the agent's text with ONE line under it saying what the
  agent is doing now. That line is the bottom edge of the reply and never wraps.
  Once the turn has run a command the line carries a chevron at its left end and
  the whole line opens the console downward out of the reply: a dark panel of
  fixed height that scrolls inside itself. The choice is remembered across turns
  and chats. Before, the list of commands was always open inside the reply and
  the line sat above the text, out of sight on a long reply.
- Runs of blank lines between the agent's phrases, left where its tool calls were
  taken out of the text, are collapsed to one.

### Added
- @name in a chat message tells that agent: every name outside quotes and code
  that is an agent other than the chat's own (five at most) gets a message on the
  bus, and each answer appears in the chat as "Answer from <agent>". The chat's
  own agent still receives the message as before. The response carries
  X-SN-Mentioned. A message that starts with @colleague goes to the named
  colleagues only, and the chat's own agent does not receive it
  (POST /api/desks/:id/reply, which takes `agents`).
- Who speaks in a chat (public/js/chat-authors.js, loaded after app.js): the
  agent's avatar at the bottom left of its run of messages and its name over the
  first; a colleague's relayed answer shown as that colleague; a silent
  NO_REPLY not drawn, a colleague's one shown as a short centred line.
- Reply to one message: Copy and Reply under a message on hover, a long press on
  a touch screen. A reply to the chat's own agent carries the quote; a reply to
  a colleague goes to that colleague only. Selecting part of a message offers
  Quote, which quotes only that part.
- Typing @ in the composer lists the agents to pick from.
- A colleague asked from the chat shows as a pending reply in the chat, with what
  it is doing (GET /api/desks/:id/waiting).
- Every new message rests in the middle of the view, as a sent one does; the
  person's messages sent in a row stack as one group with a small "queued" mark
  instead of a queued reply bubble each.
- On a phone a long agent message is folded to ten lines with Show more, and an
  agent's message runs to the right edge.
- Copy a message. A control beside each message, shown on hover, offers "Copy as
  text" (what is on screen) and "Copy as Markdown" (the message as written).

### Fixed
- A list whose items are set apart by blank lines is one list. A blank line ended
  the list, so every item became a list of its own and a numbered list read
  1. 1. 1.; the list now ends at the first line that is not another item of
  it. A list that starts at 3 starts at 3.
- Opening a chat lands on its last message and stays there while images and
  frames below it finish loading; it lets go the moment the reader scrolls or
  presses a key. On a phone the chat is hidden while the list shows and loses
  its position, so a chat reopened from the list now pins to the end when it
  appears.
- On a phone an agent's message with an avatar was capped at no width at all, and
  the open console, whose lines do not wrap, stretched it past the screen. It is
  held to the thread minus the avatar margin.
- The Quote chip under a selection shows only on a touch screen; with a mouse the
  right-click menu already has Quote selection. Copy in that menu closes it at
  once instead of printing "Copied" before the copy had finished.
- While a turn runs the last three tools show in plain words under the status
  line (the model's own description when the gateway sends one), and the chat asks
  for progress every 400 ms, one request at a time, and at once when the tab
  comes back to the front. The command stays in the console.
- Tasks on a phone: the task table (ten columns on a 390 px screen, titles
  cut to a word, the filters lying over the first rows) is a list of cards there:
  the title on two lines with the time it changed beside it, then its number,
  project / goal and flow when set, status, priority, assignee and due. The
  filters fill one row, and a task page shows its fields two to a row without
  the empty ones. CSS only; a computer keeps the table.
- A task opened by its link (/tasks/<id>) on a desktop showed the chat
  column's empty header (Chats, search, All / Unread / Working) over the task:
  chat-shell.js remembered the chats view, and the task view opens before it
  loads. It now reads the view from the page.
- Opening another chat from the list left the view near the top of its
  thread: chat-authors.js took every message of the newly built thread for one
  that had just arrived and glided to the first of them. A thread built anew for
  another chat is now the baseline, and only what comes after it moves the view.
- "Reconnecting" was shown for the whole of a long tool call. It appeared whenever
  the chat request came back without an answer, which is the ordinary end of the
  sync window on a long turn. It now appears only when the node does not report
  the turn as in progress.
- Commands in the console were listed two or three times. The steps poll runs
  every second and one answer can take longer than that; a late answer for an old
  cursor appended the same steps again. A stale answer is now dropped.

## [0.5.9] - 2026-09-26

Includes entries that shipped with node 1.12.46 without a version heading of their own.

### Changed
- Chat tabs at the top are grouped by agent, one tab per agent with a uniform
  width and a meta column; the chevron opens that agent's recent chats, the time
  sits on the right of each dropdown row, and the left-right order of tabs stays
  put when a tab is activated.

### Fixed
- On a phone a document takes the full width, and the /docs page keeps tight
  padding instead of inheriting the desktop override.

### Added
- Signed out is said, not swallowed. The login page is on another origin, so a
  fetch that followed forward auth's redirect failed CORS with a bare TypeError: a chat
  message went nowhere and only a reload showed the login page. Every same-origin call
  now uses redirect:'manual', the bounce arrives as 'opaqueredirect', and a wall says
  "You have been signed out" with Sign in again. Send checks the session first and
  leaves the text in the composer; the chat's draft is stored before the wall, so it is
  back after signing in. A tab coming back into view checks too.
- A Console block on the agent card: Show/Hide, remembered per browser; while
  open it polls `/api/agents/:slug/console` every 2 s and lists each turn in flight,
  whatever started it, with its steps in its own scrolling box that follows new lines
  only while the reader is at the bottom; an idle agent shows its last turn. The card
  used to say "working now" and nothing about what.
- A running turn shows what the agent is doing, step by step, inside its bubble
  ("Bash: <command>", what it says between tools), in a fixed-height box that follows
  the newest line. A turn the page did not start (reload, second window, a relay
  woke the agent) gets the same bubble. Before this the chat showed "Thinking 301s"
  while the agent ran forty commands, and the owner could not tell work from a hang
  (2026-09-20 and again 2026-09-23 after the 1.12.45 update removed the hot
  copy).

### Fixed
- Planner overview (/tasks/dashboard): every ring and count was computed from the planner's
  default page of 100 tasks, the last four days of 4774, so every older goal read 0/0. The
  page now reads all tasks through /api/tasks/overview (paged from the planner, descriptions
  dropped, 30 s cache). It also opened on six screens of objective cards before the first
  task. Now: three collapsible sections, remembered per browser -- Strategies (equal-width
  chips, freshest first, goals under no strategy gathered as "No strategy"), Projects (same
  chips, two rows or all; filter goals and tasks; replaces the donut chart) and Goals (compact
  grid in whole rows, ring with done/total and age, a count select and "show more", title
  filter); "show closed" is the panel's switch; activity full width; the task table freshest
  first in pages of 50. A task opens in a side drawer with the same card and gates as the
  task page (loadDetail renders into any host) instead of leaving the overview.
- Task tables on the overview and the Tasks page fit the page: columns are shares of the
  width, a long title is cut with the full text on hover; a "Created by" column (requester)
  in every task table, the agent page pane included.
- Dashboard: "What your office did" avatars have no coloured tile (it showed through the
  transparent custom pictures), letters and channel icons are larger and each row is centred
  on its avatar; "Needs your call" shows the agent who brought the task (it read a
  non-existent created_by, so every row said "Office", an O that read as 0) and its text no
  longer runs under the avatar.
- Documents list shows the owner's attachments only (the same rule as the prompt and the
  panel); it listed the whole memory workspace, 24 of alex's 26 tiles were handoffs.
- Adding, uploading, linking or deleting a document regenerates the agent at once; before,
  the prompt did not change until some unrelated settings edit.

### Fixed
- The office unit reads MESH_API_URL from /etc/smart-node/mesh.env. It had none
  and fell back to https://mesh.local, which resolves nowhere, so the agent Documents
  page never loaded on a node.

## [0.5.8] - 2026-09-23

### Fixed
- HTML, SVG and every other non-Markdown document are rendered in a sandboxed
  iframe with scripts disabled. They no longer enter the Office page through
  `innerHTML`; Markdown keeps its existing escaped renderer.
- A task in pending_review with a reviewer named on it now shows Close-to-done
  and Send-back buttons on its card in the office panel, for the reviewer only.
  The card used to render no action buttons at all in this state, so the owner
  could not close his own reviews from the UI and had to shell a curl at the
  planner; the 1.12.45 close-out was blocked on that missing surface.
  The routing is one pure function (tdReviewGate), unit-tested on the pre-fix
  baseline (RED) and after (GREEN) per Close-to-done and Send-back
  PATCH the planner directly (status=done|in_progress + a required note), and
  the planner's own actor==reviewer check refuses a wrong caller and
  surfaces the message back into the page.
- A dead node no longer leaves a chat saying an agent is working forever. The
  liveness poll now changes the turn to a non-animated node-down notice, recovery
  resolves the stored answer or an orphaned turn, and Stop ends the browser wait
  before its best-effort server abort. Failed requests are not blindly resent.
- Custom agent avatars are served from durable node state instead of the Office
  package. Uploads are proxied to the principal-gated core-api writer, and the
  installer migrates legacy files before package replacement without changing
  their public URLs.

### Added
- The chat never blocks: turns are kept per chat, the composer stays usable,
  and a message sent while its chat is working says it is queued and gets its
  own answer. Stop is its own button and keeps the partial reply, marked. Chat
  tabs and an agent's chat rows show which chats are being worked on.
- Active chats show their canonical provider-context percentage above the
  composer and in tabs; agent pages and cards surface the highest active chat.
  Warning bands match the 60/80 monitor policy.
- “Start clean” closes the provider session after an explicit preservation/loss
  confirmation. It is deliberately separate from Reload, which resumes context.

### Fixed
- Each chat keeps its own draft. The composer was one field for every
  chat: text typed in one followed you into the next, and a file attached in one
  sat in another's tray, where Send would have sent it. Switching chats now stashes
  the text and the tray and restores the other chat's; the text also survives a
  reload. Sending clears its own chat's draft before anything else.
- Stop sits on the reply it stops, not by the composer: the running reply has
  its own Stop, a queued one has none (it is already waiting in the gateway).
- A chat tab shows its context as a 1px line along its bottom edge, as thin as
  its border, instead of a percentage in its title: the length is how full it
  is, and it runs through a green-amber-red gradient on the meter's 60/80
  bands, so the colour at its end says how close to full; the figures are in
  the tab's tooltip.
- The context meter and its clean action moved from above the composer into
  the chat card (the tab's icon), next to the other session actions; the
  composer carries only the message. "Start clean" is now an eraser icon and
  "Clean", with a one-line tooltip; the confirmation is unchanged.
- Stop stopped nothing (it looked the session up by a key that is no longer
  written). A turn past the sync window is waited for while the gateway reports
  it running instead of failing after two minutes. The thread no longer draws
  twice when two loads overlap, and a message sent before the chat list loaded
  waits for it instead of going out with no agent.

### Changed
- The agent card shows the whole model id, runtime included; it used to
  keep only the last segment, so `openrouter/openai/gpt-5` read as `gpt-5`.

## [0.5.7] - 2026-08-24

### Changed
- The panel says WHO wrote, instead of calling everyone "A customer".
  Every value holding an '@' was masked to a channel-generic word, so a client,
  the owner's own second address, a supplier and a bounce all rendered
  identically and the feed could not be read. The mask was never a decision of
  this project -- it arrived with the vendored snapshot in 0.5.0 and was carried.
  Office is served behind smart-node-auth, so its only viewer is someone already
  signed in: inside the product the mask hid the owner's correspondence from the
  owner. The generic words are right on the public landing panel, where the
  viewer really is a stranger, and stay there.
  - A new `/api/directory` flattens the contacts registry (core-api
    `/api/contacts`) to handle -> display name, cached 60s. Resolution is
    server-side because contacts live in core-api's database and the browser
    cannot ask.
  - A known contact renders by name, on any channel. An unknown one renders as
    the address it actually is -- still the truth, and two strangers no longer
    read alike. "You" and this node's own agents keep precedence over the
    registry, so a contact row naming the owner cannot turn him into a third
    person.
  - A MISS IS NOT AN ERROR, and neither is core-api being down: the panel then
    shows addresses instead of names and loses nothing else.
  - The generic labels remain for the one case where they are true -- an arrival
    carrying no address at all.
  - `test/dash-labels.test.mjs` carries the new contract, including the assertion
    the old code fails: two unknown correspondents must be distinguishable.

## [0.5.4] - 2026-08-22

### Fixed
- The activity feed recognised a successful READ by matching a hardcoded set of
  planner op names, so planner knowledge lived in the feed and a new read
  operation would render as work until someone updated a list in a UI file. It
  now decides on the request METHOD, which core-api began putting on the terminal
  tool event; the name list remains only as a fallback for rows written before
  that, and an event with neither is shown rather than hidden.

## [0.5.3] - 2026-08-21

### Fixed
- The Dashboard "What your office did" feed showed polls and repetition rather
  than work. Three defects, all in what `/api/activity` forwarded:
  - A successful READ rendered as an action. The journal no longer records reads that
    found nothing; a read that found rows still reached the feed. Measured on
    archive-node over 48h, 8965 of 8986 `tool.invoked` rows were GETs against 21 for
    every write. Tool events are now classified by `payload.op`, listing the
    READS so an op the endpoint has not been taught about is shown, not hidden.
  - The verb came from `entity_id`, which for any planner call is the literal
    string `planner` -- so a comment, an update and a task creation all read
    "<agent> created planner". The action lives in `payload.op`, which the
    endpoint now forwards and the client renders.
  - A held message was news once and reported hundreds of times. Over 7 days
    archive-node recorded 413 `message.routing_denied`, 337 of them the same verdict: a
    voice call on a channel instance with no agent bound. These are now collapsed
    by (reason, channel) with a count, NOT dropped: the verdict is a real
    configuration gap, and hiding it would leave the owner unable to see why
    calls go unanswered.
  - System doorbells (`message.inbound` on the `system` channel) no longer appear.
    They are how the node wakes an agent, and rendered as "A customer contacted
    the office".

- The same card could be EMPTY on a busy node, and the fix above is what made
  that visible. `/api/activity` fetched the newest 400 raw events and filtered
  afterwards, so its depth measured how noisy the node was rather than what the
  office did. Reported from a live node within hours: an owner's OPEN dashboard
  polls the planner about twelve times a minute, so the newest 400 events covered
  the last half hour and were 379/400 `planner.list` reads. That node had done 34
  owner-facing things across the working day and not one was inside the window.
  The act of watching the feed generated the noise that evicted its contents.
  It now fans out one query per event type (core-api's `event_type` filter takes
  one value) and merges newest-first, so each type gets its own depth and message
  events are immune to read-poll volume.

  Measured on 7 days of real archive-node events replayed through this code: before, 40
  rows carrying 16 held and 9 inbound; after, 38 rows carrying 3 held and 22
  inbound. The inbound figure is the result: the repetition was consuming the
  40-row budget and pushing genuine messages out of the feed entirely.

## [0.5.2] - 2026-08-16

### Fixed
- When the Authum session expires, the chat no longer hangs as "the agent did not
  respond in time" with the user left to guess they must refresh. A central fetch
  interceptor detects the auth bounce (a 401, or a response whose final URL landed
  on the auth host) and sends the page to login.

## [0.5.1] - 2026-08-14

Absorbed from the component repository, where it has been since 2026-08-03 and
live on two nodes, while every node built from the installer went without it.
The installer half of the same fix DID reach canon on the day -- `office.sh` has
been writing `SN_MAIL_DOMAIN` into the unit for eleven days, feeding a variable
nothing read.

### Fixed
- Dashboard mail labels no longer name a particular operator or a particular
  domain. `dshMailName()` recognised an agent by a regex pinned to one mail
  domain, so on any other node every agent mailbox fell through to "A customer".
  The owner now comes from `/api/me` (whoever is signed in) and an agent is
  recognised by matching the local part against the node's OWN agent roster.
- The activity feed decided its wording from the literal `alex`, so on any other
  node a colleague read as an outsider. It now asks the roster.

### Added
- `SN_MAIL_DOMAIN` (optional): the domain the node's own mailboxes live on, the
  same value the dispatcher writes as `SN_MSGID_DOMAIN`. Exposed through
  `/api/me` as `mailDomain`, where it guards the agent match so a stranger who
  happens to share a slug stays a customer. Unset is valid and simply labels
  fewer addresses as agents.

### Note on how this landed
Ported as patches, hunk by hunk, NOT as a file copy. The two copies had diverged
in both directions: canon carried `relayHeaders` and the move of Office writes
onto `/api/planner` which the component repository does not have,
and the repository carried this fix. Copying either file wholesale would have
reverted the other side. Verified afterwards that both survive.

## [0.5.0] - 2026-06-26

### Added
- Knowledge: per-document **Pin** toggle. A pinned doc is pre-loaded into the
  agent prompt as a snapshot; unpinned docs are retrieved on demand. The tile
  shows a filled pin glyph; toggle is optimistic with rollback on server error.

- Knowledge: **Sync** button on pinned docs. Re-pulls the latest content into the
  prompt. The spinner is driven by the Web Animations API instead of a CSS
  keyframes class, so it stays visible under the OS "reduce motion" setting
  (base.css disables all CSS animations under `prefers-reduced-motion`).
- Rules: rule chips are now **clickable to activate/deactivate** a policy for the
  agent (`PUT /api/agents/:slug/policies/:slug` with `include`/`exclude`).
  Optimistic toggle with rollback; keyboard-accessible (Enter/Space).

### Added (BFF)
- Proxy route `PUT /api/agents/:slug/documents/:guid/pin` -> core-api.

## [0.3.3] - 2026-06-15

### Changed
- Chat error recovery now polls desk history instead of blindly resending. When
  a turn errors or the connection drops (Cloudflare 524 on a long turn), the
  client first polls `GET /api/desks/:id/history` for up to 2 min: if a new
  assistant turn landed server-side it is rendered (the answer was never lost,
  just the connection). Only if nothing landed does it retry the message once
  (covers a turn that truly never ran, e.g. 502 EOF). A baseline history length
  is captured in parallel with the turn so recovery can detect the new reply.
  Stops the duplicate-turn risk of the old unconditional resend.

## [0.3.2] - 2026-06-15

### Added
- Chat pending bubble now shows a calm "working N s" indicator with a live
  elapsed-seconds counter while a turn is in flight (seconds, not ms -- ms reads
  as nervous/broken). During the silent retry it switches to "reconnecting N s"
  without losing the timer. Reassures the user that work is ongoing during long
  turns instead of a static set of dots, so the activity display shows the actual turn state.

## [0.3.1] - 2026-06-15

### Fixed
- Chat no longer auto-restarts the gateway session when a turn errors. The old
  recovery path called `/api/desks/:id/reload` (-> gateway `/restart`) on every
  chat error, which terminated the in-flight turn and produced the next error --
  a 502 "EOF before result" death-spiral that surfaced raw `Gateway returned
  502` / `Session ID is already in use` text to the user. Recovery is now a
  single plain retry of the same message with no restart; the gateway respawns
  its own process and `--resume` keeps context. The explicit **Reload** button
  still restarts the session on demand, so a clean restart remains an explicit action.

## [0.3.0] - 2026-06-15

### Added
- Studio views (planner-backed, rendered inside the Office shell): **Tasks**
  list, **Task detail**, and **Dashboard**. New sidebar nav group (Dashboard /
  Chats / Tasks) + a small SPA view-router with deep-links (`/tasks`,
  `/tasks/:id`, `/dashboard`) and browser back/forward; views never overlap.
- BFF proxy to the planner companion: `PLANNER_URL` env + `/api/tasks`,
  `/api/tasks/:id`, `/api/tasks/:id/comments`. planner runs in dev auth mode;
  Authum gates at the edge. Task data is read from planner, never from core.
- Task detail shows breadcrumb, status/priority chips, meta + a lifecycle
  timeline (created -> assessed -> approved -> started -> completed), description
  and comments. Dashboard derives metrics (status tiles, recent, attention)
  from the task list. All styled with the Office design system (sn-* tokens).

## [0.2.2] - 2026-06-15

### Fixed
- Empty messages no longer render an empty bubble (a hollow circle in the
  thread). `bubble()` skips blank content; a blank (non-error) chat response
  drops its pending bubble instead of leaving it empty.

## [0.2.1] - 2026-06-15

### Added
- `formatter.js`: GFM table support (header + `|---|` separator + body rows,
  per-column alignment via `:`), with inline markdown inside cells. Tables were
  previously rendered as raw `| ... |` text.

## [0.2.0] - 2026-06-14

### Added
- `public/js/formatter.js`: extensible, registry-based message formatter
  (per-specialist / per-model), default = markdown. Agent replies now render
  markdown (headings, lists, code, links) instead of raw escaped text.

### Changed
- Chat send is self-healing: on a gateway error it shows "reconnecting...",
  resumes the desk session (`/reload`), and retries the same message once on the
  same desk before surfacing an error -- recovery is invisible to the user.
