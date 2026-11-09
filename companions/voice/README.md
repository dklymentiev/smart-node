# voice -- Smart Node voice adapter (v1)

The voice channel for a Smart Node: full-duplex browser voice via the Gemini
Native Audio API. It was forked wholesale from the earlier Voice implementation.
This is the minimal v1.

## Scope (v1)

- **Answer-now only.** The assistant talks and remembers. It does NOT yet
  delegate tasks or hand off to a worker -- that is a later stage (it
  depends on the Secretary trigger).
- **Monolith.** Transport + conversational front + memory are fused in
  `backend/main.py` -- inherited from Voice. The clean three-element
  split (`adapters/voice/` as transport only) is a deferred refactor.

## Architecture

```
Browser (mic)  --WebSocket /ws-->  FastAPI (backend/main.py)  --WebSocket-->  Gemini Native Audio
                                          |
              node memory companion (Mesh)  -- per-user memory, extract-after-session
              SQLite (/data/voice.db)       -- identity, sessions, message logs
```

Per-user memory lives in the node memory companion (one Mesh document per
user, deterministic GUID), not in local SQLite. The local SQLite keeps only
identity, sessions and message logs.

## Deploy

```
cp .env.example .env     # fill GOOGLE_API_KEY, ASSISTANT_NAME, access phrase
docker compose up -d --build
```

Container `smart-node-voice`, port `127.0.0.1:8011`. Reached via Traefik at
`https://voice.example.com` (route: `/srv/traefik/config/custom/voice.yml`).
HTTPS is required -- the browser only grants the microphone in a secure context.

## Config (.env)

| var | meaning |
|-----|---------|
| `GOOGLE_API_KEY` | Gemini Native Audio API key |
| `ASSISTANT_NAME` | the node voice identity (rebrandable) |
| `DB_PATH` | SQLite path -- identity/sessions/logs (default `/data/voice.db`) |
| `MESH_API_URL` | node memory companion URL (default `http://localhost:8000`; on a node `http://smart-node-mesh:8000`) |
| `MESH_WORKSPACE` | Mesh workspace for voice memory (default `voice`) |
| `MESH_API_KEY` | Mesh API key -- empty on a node (companion runs `AUTH_REQUIRED=false`) |
| `VOICE_ACCESS_PHRASE` | access passphrase the caller must say |
| `VOICE_ADMIN_TOKEN` | bearer token for `/debug`, `/api/debug` |
| `VOICE_FORWARD_ENABLED` | `0` in v1 -- capability forwarding is off |

## Deferred work

- Add messages-registry provenance (`channel_type='voice'`).
- Replace the retired `voice.<id>.sn.vpn` scheme with `communication.sh`
  wiring based on the node's `--node-fqdn` value.
