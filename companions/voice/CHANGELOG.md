# Changelog

All notable changes to `smart-node-voice` go here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are semver. Tags on `main` correspond to the `VERSION` file.

## [Unreleased]

### Fixed
- A call no longer dials into nothing when the sign-in session has ended:
  forward auth refuses the /ws upgrade silently, so the page asks a plain request first
  (redirect:'manual') and shows "You have been signed out" with Sign in again; a call
  that loses its socket checks the same before retrying twenty times.
- Internal delegations identify their origin with the specified `conversation`
  key, which core-api consumes, instead of the ignored `caller` key.

## [1.0.4] - 2026-08-16

### Removed
- `my_messages`. It read the agent's own mailbox over TCP with a `Remote-User`
  header, which core-api has refused since 0.18.0 (an agent identity is
  established only over the unix socket) -- so the tool returned a 403 and the
  agent told the caller "error reading my email". It was also redundant with
  `messages_search`, which is now the single recall tool: a person's history
  across channels, balanced, with a channel filter for "what about email".

## [1.0.3] - 2026-08-16

Turns reach the registry as they happen, not once at hangup.

### Changed
- Each voice turn is written to the bus the moment it completes
  (`record_voice_turn_live` at both the user and assistant turn points),
  best-effort with one retry, never blocking the call. The end-of-session
  `record_voice_to_bus` becomes an idempotent RECONCILIATION that only fills
  turns a live write missed -- keyed `voice:<session>:<seq>` by the turn's index,
  the same key the live writes use, so nothing is doubled. A crash mid-call now
  keeps everything said so far; every turn carries its real time; and the current
  call is searchable while it is still going.

## [1.0.2] - 2026-08-16

The voice line recalls a person reliably and can start a fresh thread.

### Added
- `new_conversation` tool + `window_clear`: the caller can say "start a new
  conversation" and the agent wipes THIS running thread (memory + the on-disk
  sliding window), keeping durable memory. The escape hatch from a thread the
  agent has talked itself into a corner on.
- `messages_search` (the catalog tool over `/api/messages/search`) is exposed to
  Gemini from the node catalogue; the dispatcher fills `identity` from the
  AUTHENTICATED caller, so the search always looks up the right person rather
  than a name the model guessed.

### Fixed
- The start-of-call cross-channel block no longer poisons itself. It was fed the
  agent's OWN past voice replies -- including denials like "we did not talk this
  morning" -- and repeated them, confirming its own error every call (the owner
  hit exactly this across three morning calls). The block now drops the agent's
  spoken (voice-outbound) turns and its denial lines, keeps what the CALLER said
  and the other channels, and is framed as ground truth to trust over the model's
  own memory.
- `my_messages` reads the caller's cross-channel history (by-contact), not only
  the agent's slug-keyed mailbox, so telegram/email are no longer invisible to it.

## [1.0.1] - 2026-08-16

### Changed
- Cross-channel recall now reads the unified message registry. The voice
  line is the second door to the agent (line.<domain>), and it was still on the
  old static-list mechanism: `fetch_crosschannel_context` read a STATIC list of
  conversation ids (`VOICE_CROSSCHANNEL_CONVERSATIONS`) out of the gateway store,
  which was unconfigured on most nodes, so the voice agent recalled nothing from
  other channels. It now calls core-api `GET /api/messages/by-contact` for the
  authenticated caller (Authum Remote-User), the same read side the office desk
  chat uses -- the person's recent history across telegram, email, web chat and
  voice from one registry, resolved by contact. So foreman recalls the same
  person on the voice line as in the desk chat.

## [1.0.0] - 2026-05-27

**Repo extraction.** First-time repo creation for the voice adapter. State at extraction matches the live node's `/opt/smart-node/voice/`.

### Provenance

Initial implementation forked wholesale from Voice. The clean RFC-011 three-element split (transport / conversation / memory as separate `adapters/voice/`) is a deferred refactor; this v1 keeps the monolithic backend/main.py inherited from Voice.

### Layout

- `backend/main.py` -- FastAPI WebSocket bridge to Gemini Native Audio
- `frontend/` -- minimal in-browser mic UI (served from same container)
- `Dockerfile` + `docker-compose.yml` -- containerized; uses smart-node Docker network so it can reach the mesh memory companion directly
- `systemd/smart-node-voice.service` -- one-shot wrapper that drives `docker compose up/down`

### Scope (v1)

- **Answer-now only.** The assistant talks and remembers. It does NOT yet delegate tasks or hand off to a worker -- that is a later stage (RFC-011 element 2 proper, depends on the Secretary trigger).
- **Monolith.** Transport + conversational front + memory are fused in `backend/main.py` -- inherited from Voice.

### Notes

- The legacy unit name `smart-node-communication-voice.service` (prefixed `communication-*` for historical routing) is replaced by `smart-node-voice.service` (canonical to this repo). Companion installer (`voice.sh` to be added in P3) handles the rename.
