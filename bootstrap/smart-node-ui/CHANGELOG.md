# Changelog -- smart-node-core / frontend (panel)

## Unreleased

- The phone menu shows its items again: the frosted top bar is drawn by a layer behind the bar (a backdrop-filter on the bar made it the frame of the fixed drawer inside it, cutting the drawer to the bar's height), and the dimmer is under the bar instead of over the drawer.

- The panel is an installable app, like the Office: a manifest, icons (the mark dark on light, so it is told apart from the Office icon), a service worker that keeps only an offline page, and the three files a browser fetches before it signs in (manifest, worker, icons, offline page) served without sign-in by their own route.

- First-run page: its styles load before sign-in, and it names the file that holds the setup token (the install report never held it).
- Workflows: each card has the owner's two controls, applied at once. A switch puts the flow on or off (off: no new run starts, a run already going finishes), and the agents who may start it are chips: a cross takes one off, "+ Add an agent" gives it to another. Uses PATCH /api/workflows/<name> and PUT /api/workflows/<name>/agents.
- Settings page (/settings/), back in the top bar after Workflows, with the node's own settings only, read only: the node (name, domain, release, whether the agents run, with the way to Limits), the rules every agent's prompt carries (policies by category, linked to Policies), who can sign in and what each role may do (its holders and permissions), and the departments with their heads. Reads core-api's /api/node/about and /api/node/access.
- Memory page (/memory/), where the top bar said "Memory soon": what the agents keep in memory, read only. Search by meaning or the newest documents, over all workspaces or one (chips with each agent's count), a type and a period; a row opens the whole document beside the list. /memory/?ws=<workspace> opens one agent's memory, and the agent page's Memory tab links there. Reads core-api's /api/memory (memory.read, the owner's). The empty Settings item and the AI subnav's Workflows "soon" leave the top bar.
- Workflows is read only: each workflow with its status, the agents who may start it and its steps as text in the order they run, each with where its outcomes lead; runs and the diagram are in the office. The "Run a test inquiry" form is gone: it ran a sample booking whatever the flow.
- Workflows: the row of step boxes is gone. It drew a branching flow as one line (a review that sends back, the two endings done and cancelled one after the other). Each card links to the flow diagram in the office instead.
- The top bar drops the empty Documents, Mail and Voice items (they said "soon" and led nowhere). Pages are served with Cache-Control: no-cache, as the assets were: a browser kept an edited page, its top bar included, for up to a day.
- Planner page (/planner/), where the top bar said "Planner soon": the task-status dictionaries, wait reasons and close outcomes, edited in place (label, description, legacy stage, effects, on/off as the panel switch, which applies at once; add a row). It reads and writes the planner through core-api's /api/planner proxy, so only the owner can change them. Moved here from the office Settings page.
- One way to say what went wrong across the panel: a refused save names the fields and why, a bare HTTP status says what it means (signed out, not allowed, the node failed and its log is on Systems), and an error page from a proxy no longer hides the status. Was [object Object] or a bare HTTP code in many places.
- Agents page: every card links to its Tools and permissions; the agent page opens on the tab its address names (#tools). The grants modal nothing opened is gone.
- Secrets page: one table for every credential of the node, the gateway's provider keys included, with where each is wired in, which agents may read it, who set it last and who read it last; a Requests section lists refused agent reads with a Grant button. The backend card says postgres.
- Agents page: a Now row on every card shows the agent's live processes from the runtime's process table (working, with the current tool, or idle for how long, and the last model call), repainted every five seconds. A Stop button ends the agent's processes at once and disables it; Enable brings it back.

## 0.6.8 -- 2026-10-03

- Offer only pause and refuse for turn length. Hide ask owner until its HITL flow exists.
- Refresh the Limits editor asset in existing browsers after the update.

## 0.6.6 -- 2026-10-03

- Show an unset agent spending default as an editable blank field, without creating a spending cap during update.

## 0.6.5 -- 2026-10-02

- Add the Limits page, agent Limits tab and node pause control. AI Gateway spending cards are read-only and link to Limits.

## 0.6.4 -- 2026-09-23

### Changed
- Agent page: the system prompt is only ever BUILT from the settings, never edited.
  Tabs are Instructions (first), Tools, Channels, Access, Settings, Memory, Preview.
  Specialist (an editor of the composed CLAUDE.md), Policies (kept in localStorage, never
  reached the server) and Workflow (an empty placeholder) are gone.
- Preview shows the real prompt from `GET /api/agents/<slug>/system-prompt` (byte-identical
  to the agent's CLAUDE.md) with Generate (`POST .../regenerate`); the client-side template
  and the "final prompt" block that called `/prompt` (404) are removed.
- Settings (ex-Manifest): role, description, schedule; system user read-only; OS groups
  are not a form field; Name, Model and On duty live on Instructions only.
- The Knowledge card (agent_knowledge, retired by the 2026-06-23 design) is replaced by
  Documents: list, pin, open, remove, "+ Text note", "+ Upload file", same store as the office.

### Fixed
- Save on Settings/Access sent `specialist_body` from the Specialist tab, which held the
  COMPOSED prompt: any save there overwrote the agent's instructions with the built file.
- "Save configuration" shows only on the tabs it saves (Tools, Access, Settings).
- Signed out is said, not swallowed: every page's same-origin calls use
  redirect:'manual', and a redirect to the login page raises "You have been signed out"
  with Sign in again, instead of a save that silently did nothing.

### Added
- Database: a click on a row opens the whole row in a modal, one column per line, JSON
  indented, null marked; the table cuts each cell to one line.

## 0.6.3 -- 2026-09-23
Shipped hot on example-node first at the owner's request, then carried here.
- Host data reaches the panel only through core-api: Crons reads
  `/api/system/crons`, the runner card and Services read `/api/system/services`;
  no page reads the collector's `/data/*.json` any more.
- Overview: two switches, Agents (the runner) and Gateway (gateway + voice),
  with a confirm step; only a signed-in session can use them.
- Services: Path column (install tree, program or compose dir; unit file or
  mounts on hover); the Emergency egress placeholder is gone.
- Systems is now Health (menu label, title, lead; URL unchanged): one ECharts
  chart of tool calls, failed calls and agent wakes on one time axis; the Cron
  jobs card is Node timers with each timer's last result; a running agent's
  state pulses like the Office's "working now".
- AI page: budget cards with the context threshold, real stat cards, provider
  cards, session timeline (ECharts). Events and Messages: activity/traffic
  charts. Crons: runner card and every host schedule. Database: theme tokens,
  newest 100 of N. Footer version from `/version.txt`; sticky footer.
- Integrations: live status (companion installed and its units running), MCP
  server and Helper tools columns; the dead URI links and New button are gone.
- MCP: live status per server with the reason on hover.
- Runtimes: live list of what runs an agent's turn (Claude Code, Codex, Hermes,
  Brin), read-only.
- Agents: footer pinned to the card bottom, path /srv/agents/<slug>, a click on
  Health opens the grants instead of the agent. Agent page: checkboxes drawn as
  switches, themed channel address fields and save-bar buttons.
- Specialists and Policies: the edit modal's textarea shared its id with the
  table body, so it opened empty and a Specialists save posted an empty body.
- References removed from the menu and the agent tabs (registry empty, no
  resolver).
- Vendored Apache ECharts 5.6.0 (`assets/vendor/`, Apache-2.0).

## 0.6.2 -- 2026-09-18
- The agent card shows the model as the first of its facts (Model, above
  Schedule) instead of a header badge: a `<runtime>/<model>` id squeezed the
  name and id into a column. A long id ends in an ellipsis, the full
  id on hover.
- Every agent Model selector (new agent, agent editor, agent-extra) is filled from
  `/api/models`: one group per runtime, the vendor on each
  option, and the value is the stored `<runtime>/<model>`. The hard-coded
  sonnet/haiku/opus options are gone. A current model the catalog no longer offers
  is kept and marked "(current)".

## 0.6.1 -- 2026-09-16
- Added the global AI context-handoff control and per-agent override in the
  Memory tab. Both write through owner-gated, audited core APIs.
- Session detail shows durable context-reset markers and the associated Mesh
  handoff document identifier.

## 0.6.0 -- 2026-06-20
- **Unified design system**: the panel now
  shares one source of truth with the Office client (the public demo). Added
  `ui/tokens.css` + `ui/base.css` (the `--sn-*` tokens), linked before `shell.css` on all
  pages with a `data-theme` bootstrap; `shell.css` refactored into `@layer app` with panel
  tokens (`--paper`/`--ink`/`--steel`/...) aliased onto `--sn-*`.
- Themes: light + dark, **default dark** (was light-only). Retired the prism gradient ->
  brushed-metal `--sn-edge` as the single accent.
- Brushed-metal edge on cards (stat / provider / agent / table / cap / role / modal);
  hover and selected state light the full `--sn-edge` gradient.
- Added a **theme toggle** button in the topbar (was bootstrap-only, no UI control);
  injected via shared `shell.js`, persists `sn-theme`.
- NOTE: in progress -- per-page content markup pass still pending; not yet baked into the
  panel image (hot-pushed into the running container for preview).

## 0.5.0 -- 2026-06-09
- New **Database** section: browse core DB tables + row contents (admin-only, read-only).
  Backend routes /api/database/tables and /api/database/table/{schema}/{name}.
- Unified the top nav across ALL pages (was drifted: some had Workflows, some marked
  companions "soon", links pointed at a hardcoded host). Now one canonical menu; not-installed
  companions shown as "soon" (no hardcoded host).


## 0.4.0 -- 2026-06-07
- Consolidated into smart-node-core. Panel source now lives here (was the
  standalone smart-node-ui repo, retired).
- Carries the POL_LS fix: policies load from /api/policies (real backend),
  no localStorage mock. The slug pattern also permits escaped dashes.
- Moved panel deploy scaffolding in from smart-node-ui so this dir is a
  complete vendor source for the installer B06 stage:
    host-tools/sn-panel-data.sh
    systemd/sn-panel-data.{service,timer}
    VERSION
