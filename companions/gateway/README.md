# AI Gateway

Universal API for AI model access. Provides unified interface for Claude CLI with persistent sessions, shadow caching, rate limiting, and real-time monitoring via WebSocket.

**URL:** https://ai.example.com
**Local:** http://172.17.0.1:19850
**Access:** VPN (10.0.0.x) + Server IP + Docker networks

## Features

- **CLI Sessions**: Persistent Claude CLI sessions with context preservation
- **Per-session TTL**: Each session has its own timeout (default 1h, configurable)
- **Multi-turn**: Session remembers conversation history
- **Clean context handoff**: Durable Mesh handoff before an opaque CLI session is reset
- **Streaming**: SSE streaming for real-time responses
- **Shadow Cache**: Detects duplicate requests, marks cache hits in logs (no actual caching -- always sends real request)
- **Rate Limiting**: Per-IP rate limiting with configurable limits
- **Request Logging**: All requests logged with session files + WebSocket broadcast
- **API Key Auth**: Optional `X-API-Key` header authentication
- **Python Client**: Simple async client included

## Quick Start

### Python Client

```python
from client import AIGateway

# One-shot (auto creates/closes session)
ai = AIGateway()
response = await ai.chat("What is 2+2?")
print(response)  # "4"

# Multi-turn conversation
async with AIGateway() as ai:
    await ai.send("My name is Alex")
    response = await ai.send("What is my name?")
    print(response)  # "Alex"

# With specific model and service
ai = AIGateway(model="sonnet", service="my-app")
await ai.create_session()
r1 = await ai.send("Hello")
await ai.close()
```

### Direct API (curl)

```bash
# Create session (service is required)
curl -X POST https://ai.example.com/api/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"service": "my-app", "model": "haiku"}'
# Returns: {"session_id": "xxx", "provider": "cli", "model": "haiku", "status": "ready"}

# Create session with short TTL (300s = 5 min)
curl -X POST https://ai.example.com/api/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"service": "my-app", "model": "haiku", "timeout": 300}'

# Chat
curl -X POST https://ai.example.com/api/v1/sessions/xxx/chat \
  -H "Content-Type: application/json" \
  -d '{"content": "What is 2+2?"}'
# Returns: {"session_id": "xxx", "content": "4", "tool_uses": [], "usage": {...}}

# List active sessions
curl https://ai.example.com/api/v1/sessions
# Returns: {"sessions": [{"session_id": "xxx", "model": "haiku", "service": "my-app", "ttl": 300, ...}]}

# Close session
curl -X DELETE https://ai.example.com/api/v1/sessions/xxx

# Health check
curl https://ai.example.com/api/v1/health

# Cache stats
curl https://ai.example.com/api/v1/cache/stats
```

## API Endpoints

### Sessions (CLI)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/sessions` | Create session |
| POST | `/api/v1/sessions/{id}/chat` | Send message |
| POST | `/api/v1/sessions/{id}/chat/stream` | Stream response (SSE) |
| GET | `/api/v1/sessions` | List active sessions |
| DELETE | `/api/v1/sessions/{id}` | Close session |
| GET | `/api/v1/sessions/processes[?agent=]` | The process table: every live agent process, its turn, idle time, last model call |
| POST | `/api/v1/sessions/agent-stop` | End every live process of an agent now (`{agent, requested_by, reason}`); sessions are kept |

### Cloud Providers

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/chat` | One-shot chat (anthropic/gemini/openrouter) |
| POST | `/api/v1/chat/stream` | One-shot streaming chat |

### Monitoring

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/providers` | List providers |
| GET | `/api/v1/cache/stats` | Shadow cache statistics |
| GET | `/api/v1/usage` | Usage stats (if enabled) |
| WS | `/ws/live` | Real-time request log stream |

### Utility

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | README (HTML) |
| GET | `/docs` | Swagger UI (if enabled) |

## Request/Response Examples

### Create Session

```json
// POST /api/v1/sessions
// Request:
{
  "service": "entis",
  "model": "haiku",
  "system_prompt": "You are a helpful assistant",
  "timeout": 300
}

// Response:
{
  "session_id": "8ef61ac2-e823-4070-b4a2-8663b2a4b713",
  "provider": "cli",
  "model": "haiku",
  "created_at": "2026-01-31T06:37:48.116824",
  "status": "ready"
}
```

**Fields:**
- `service` (required): Calling service identifier (e.g. "entis", "rein", "top-monitor")
- `model`: Claude model -- haiku (default), sonnet, opus
- `system_prompt`: Optional system prompt for the session
- `timeout`: Session TTL in seconds (default 3600). Gateway auto-closes idle sessions after this time.

### Chat

```json
// POST /api/v1/sessions/{id}/chat
// Request:
{"content": "What is the capital of France?"}

// Response:
{
  "session_id": "8ef61ac2-...",
  "content": "Paris is the capital of France.",
  "tool_uses": [],
  "usage": {"input_tokens": 10, "output_tokens": 8}
}
```

## Session TTL

Each session has a TTL (time-to-live) that controls when idle sessions are automatically closed:

- **Default**: 3600 seconds (1 hour)
- **Short-lived services** (like Entis): use `timeout: 300` (5 minutes)
- **Long-running conversations**: use default or higher values
- **Cleanup runs every 5 minutes**: sessions exceeding their TTL are terminated

This prevents orphaned sessions when clients crash or fail to call DELETE.

## Context Handoff

Long-lived opaque agent sessions use the latest provider response's
`input_tokens + cache_read_tokens + cache_write_tokens` as their current context
size. The node default is 120,000 tokens and an agent-specific policy can
override or disable it.

After a successful user-visible response crosses the boundary, the gateway asks
the same CLI session for a self-contained handoff, writes it to the agent's Mesh
workspace, atomically records `session.context_reset`, and only then completes
and closes the old session. The next session for the same agent and conversation
injects the latest matching handoff on its first turn.

If generation or Mesh persistence fails, the old session stays open and
`session.handoff_failed` records the failure together with the conditions for the
next attempt: a failed session backs off until the cooldown passes or its context
has grown, so a memory outage does not buy a full-context generation on every
turn. Older handoff documents are kept. Provider transcripts are not deleted or
pruned by this feature.

The next session finds its predecessor's handoff by the document id recorded on
the session that was retired, not by searching tags, so no conversation id can
spell itself out of its own memory. Tags are still written (lowercased, the way
Mesh stores them) for people searching later.

### What this does to concurrent turns

A turn holds a per-session claim through its response AND its handoff. A second
user turn QUEUES behind it, exactly as it used to queue on the CLI backend's
per-session send lock: no message is refused for a lifecycle reason. The one case
that cannot be queued is a turn that arrives while the handoff is already
running: by the time it reaches the front, its session is closed. It gets

```json
409 {"detail": {"error": "session_reset", "reason": "...", "session_id": "..."}}
```

which is not a failure but an instruction: send the same text to a new session
(brin does this by itself). The answer to the person is published before the
handoff starts, so a threshold-crossing turn does not spend its synchronous
window on that second request.

On this node Mesh runs with `AUTH_REQUIRED=false` and takes the workspace from
`X-Workspace`, so `MESH_API_KEY` is empty. If memory is ever put behind keys, that
variable has to be set in `/etc/smart-node/mesh.env`, or handoffs fail with 401:
loudly, but every single time.

## Shadow Cache

Gateway tracks hashes of all chat requests. When an identical request arrives (same model + system_prompt + message), it's marked as `cache_hit` in logs but **still sent to Claude** -- no actual caching.

Use cases:
- Dashboard shows yellow markers for cache hits -- potential cost savings
- Identify duplicate calls from services

```bash
# Check cache stats
curl https://ai.example.com/api/v1/cache/stats
# {"entries": 42, "hits": 5, "misses": 100, "active": 40, "max_size": 10000, "ttl": 3600}
```

## WebSocket (Live Monitoring)

Connect to `/ws/live` for real-time request log stream:

```javascript
const ws = new WebSocket("wss://ai.example.com/ws/live");
ws.onmessage = (e) => {
  const entry = JSON.parse(e.data);
  // {ts, session_id, ip, type, model, service, tokens, duration_ms, cache_hit}
};
```

Event types: `create`, `chat`, `close`, `error`

## Models

| Model | CLI Name | Description |
|-------|----------|-------------|
| Claude Haiku | haiku | Fast, cheap (default) |
| Claude Sonnet | sonnet | Balanced |
| Claude Opus | opus | Most capable |

## Configuration

All settings via environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| HOST | 0.0.0.0 | Listen address |
| PORT | 8000 | Listen port |
| CLI_PATH | claude | Path to Claude CLI binary |
| CLI_SESSION_TIMEOUT | 3600 | Default session TTL (seconds) |
| CLI_MAX_SESSIONS | 10 | Max concurrent sessions |
| SKIP_PERMISSIONS | false | Skip Claude CLI permission prompts |
| GATEWAY_API_KEY | (none) | API key for X-API-Key auth (optional) |
| ALLOWED_IPS | 127.0.0.1 | Comma-separated IP whitelist |
| TRUSTED_PROXIES | 127.0.0.1 | Trusted proxy IPs (for X-Forwarded-For) |
| LOG_CONTENT | false | Log request/response content |
| LOGS_DIR | ./logs | Directory for request logs |
| ENABLE_DOCS | false | Enable /docs Swagger UI |
| ENABLE_USAGE_ENDPOINT | false | Enable /api/v1/usage |
| CORS_ORIGINS | http://localhost:3000 | CORS allowed origins |
| ANTHROPIC_API_KEY | (none) | For Anthropic cloud provider |
| GEMINI_API_KEY | (none) | For Gemini cloud provider |
| OPENROUTER_API_KEY | (none) | For OpenRouter cloud provider |
| CONTEXT_HANDOFF_THRESHOLD | 120000 | Upgrade-overlap fallback until the database policy is readable |
| MESH_API_URL | http://127.0.0.1:8000 | Mesh API used for durable handoffs |
| MESH_API_KEY | (none) | Optional Mesh API key |
| MESH_HANDOFF_TIMEOUT | 15 | Mesh write timeout in seconds |
| MESH_HANDOFF_READ_TIMEOUT | 5 | Mesh read timeout; sits on the session-creation path |
| CONTEXT_HANDOFF_RETRY_COOLDOWN | 600 | After a failed handoff, seconds before another attempt |
| CONTEXT_HANDOFF_RETRY_GROWTH | 1.15 | Or this much context growth, whichever comes first |

## Files

```
ai-gateway/
├── main.py              # the door: FastAPI application + endpoints
├── runtime_main.py      # the runtime: owns the agent CLI processes (own unit)
├── runtime_proxy.py     # the door forwards /api/v1/sessions/* and CLI chats
├── model_tokens.py      # agent process tokens, stored as digests
├── model_door.py        # the one door to the model: caller, forward, record
├── anthropic_relay.py   # /anthropic/*: the agents' own model calls
├── openai_relay.py      # /openai/v1/*: the same for agents in the OpenAI format (Flint)
├── config.py            # Configuration from env vars
├── models.py            # Pydantic request/response models
├── session_manager.py   # CLI session lifecycle + TTL cleanup
├── rate_limiter.py      # Per-IP rate limiting
├── request_logger.py    # Request logging + WebSocket broadcast
├── shadow_cache.py      # Shadow cache for duplicate detection
├── client.py            # Python async client
├── ai-gateway.service   # Systemd service file
├── .env.example         # Environment variables reference
├── LICENSE              # MIT License
├── tests/
│   └── test_integration.py  # Integration tests
└── README.md
```

## Service Management

```bash
# Status
systemctl status ai-gateway

# Restart
sudo systemctl restart ai-gateway

# Logs
journalctl -u ai-gateway -f

# Integration tests
cd /opt/smart-node/ai-gateway && venv/bin/pytest tests/ -v
```

## Two processes: the door and the runtime

The gateway companion runs as two systemd units of one install, one
service account and one `.env`:

- `smart-node-gateway.service`, the door (`main.py`, `127.0.0.1:8000`).
  The one address callers and agent processes know. Cloud providers
  (`/api/v1/chat` for a cloud model, `/api/v1/chat/stream`), the agents'
  own model calls (`/anthropic/*`), budgets, usage, health. It spawns
  nothing and runs with `NoNewPrivileges=true`.
- `smart-node-runtime.service`, the runtime (`runtime_main.py`,
  `127.0.0.1:8002`, loopback only). Owns the agent CLI processes:
  `/api/v1/sessions/*` (the CLI sessions, the session-first v2 API,
  activity, interrupt, steer) and a chat routed to the local CLI. This is
  the process that uses sudo to run an agent as its own OS user.

The door forwards everything under `/api/v1/sessions` and every CLI chat
to `RUNTIME_URL` as it arrived and streams the answer back
(`runtime_proxy.py`). Restarting the door costs a running agent one
retried model call; restarting the runtime ends every agent process, as
a gateway restart always did. `/api/v1/health` reports the `cli` provider
from the runtime's own health; a runtime that does not answer is reported
as `unavailable` with its address.

## Model relay for CLI agents

Claude Code in an agent's process calls the Anthropic Messages API itself.
The gateway puts itself on that path (`anthropic_relay.py`):

- at spawn, the runtime mints a token for the process and hands it, with
  `ANTHROPIC_BASE_URL` pointing at the door, over the wrapper's private
  stdin frame; only the token's SHA-256 digest is stored
  (`sn_core.model_tokens`, `model_tokens.py`); the node credential
  (`cli_providers.env_vars`, resolved from the secret store) stays in the
  door;
- `POST /anthropic/v1/messages` (and any other path under `/anthropic/`) is
  forwarded to `api.anthropic.com` with the token swapped for the credential,
  streaming kept; the model must be of the family the session was created
  with, or haiku; a bearer the gateway did not issue is refused (401);
- each call is one row in `sn_core.model_calls`: session, agent, model, usage,
  stop reason, tool_use names, outcome, duration.

The token is revoked when the process ends (reclaim or exit) and, all of
them, when the runtime starts or stops. `MODEL_RELAY=false` in the runtime's
environment turns the relay off; processes then carry the credential and
call the provider directly, as before.

Both entrances are thin: `model_door.py` holds what every model call passes
(the token looked up, the call forwarded, `model_calls` and `model_calls_live`
written); an entrance reads its format's model name and usage and knows its
provider's route. A new format is a new entrance on the same door.

### The rules on every call

A turn is checked when it starts; a long turn is many model calls, so the
door checks every one too (`model_door.check`): an agent that is stopped
(`sn_config.agents.enabled` false) gets 403, a spent hard budget
(`budget.check_budget` for the session's tenant, agent and metadata) 402.
The same check holds for the agents' calls (`/anthropic`, `/openai/v1`) and
the gateway's own (`Transport`); the gateway's HTTP callers get the shapes a
turn refused at its start already has, 409 `agent_stopped` and 402
`budget_exhausted`. A decision holds 10 seconds per caller. Rules that cannot
be read let the call go, with a warning. Spend is still counted per turn.

### The gateway's own model calls

`/api/v1/chat` and the HTTP session backends (OpenRouter, Gemini) call their
providers themselves. Their HTTP client sends through `model_door.Transport`,
so each model call is kept in `model_calls_live` while it runs and recorded in
`sn_core.model_calls` like an agent's (`auth` = `internal`, `provider` the
provider's name, `user_agent` = `gateway:<service>`). The code that starts a
call names whom it is for: `/api/v1/chat` the request's `agent`, `session_id`
and `service`, a session turn its agent and session. Usage per turn and
budgets are still kept as before (`ai_sessions`, `budgets`).

### Flint and the OpenAI format

Flint gets the same: a token in place of its provider key and
`FLINT_BASE_URL` pointing at `/openai/v1` (OpenRouter, OpenAI) or
`/anthropic/v1`. `openai_relay.py` serves `POST /openai/v1/chat/completions`
and `GET /openai/v1/models`, swaps the token for the provider row's key and
records each chat call in `sn_core.model_calls`; a bearer the gateway did not
issue is refused, not forwarded. See FLINT.md.

### A second upstream: a CLI provider row

A row of `sn_config.cli_providers` whose `binary_path` is the Claude Code
binary is a backend under its own slug (`backends.claude_cli_upstreams`):
the runtime runs the same binary with the row's `env_vars`, and the door
routes the process's model calls by the provider its token names. The row's
`ANTHROPIC_BASE_URL` is the upstream (as the CLI itself would read it; unset
or the door's own address means `api.anthropic.com`), its
`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` the credential (a
`secret:<ref>` from the store). An upstream of the row's own needs no
credential; then nothing is sent as one, and the token never is.

A local model server on a test node, for example, Ollama speaking the
Messages API on `127.0.0.1:11434`, with the smallest model it has:

```sql
INSERT INTO sn_config.cli_providers (slug, binary_path, invocation_args, supported_models, env_vars)
VALUES ('local-cli', '/usr/local/bin/claude', '[]', ARRAY['qwen2.5:0.5b'],
        '{"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434",
          "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen2.5:0.5b",
          "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}');
```

Then `POST /api/models` with `provider: local-cli` (core-api validates the
slug against `GET /api/v1/backends`, which lists the row), and an agent on
`local-cli/qwen2.5:0.5b`. Its turns run the real Claude Code process through
the real door and runtime; only the upstream is local. `sn_core.model_calls`
and `sn_core.model_tokens` carry the provider.

## Access Control

IP whitelist configured via `ALLOWED_IPS` env var. Default production setup:
- `10.0.0.0/24` -- WireGuard VPN
- `<server-ip>` -- Server
- `172.17.0.0/24`, `172.18.0.0/24`, `172.20.0.0/24` -- Docker networks

Requests from other IPs get `403 Forbidden`.

Optional API key auth: set `GATEWAY_API_KEY` env var, then pass `X-API-Key` header.

## Migration from claude-wrapper

```python
# Old (claude-wrapper)
sys.path.insert(0, '/opt/smart-node/claude-wrapper')
from claude_wrapper import ClaudeWrapper

wrapper = ClaudeWrapper(model="haiku")
await wrapper.initialize()
response = await wrapper.call(prompt)
await wrapper.shutdown()

# New (ai-gateway)
sys.path.insert(0, '/opt/smart-node/ai-gateway')
from client import AIGateway

ai = AIGateway(model="haiku", service="my-app")
response = await ai.chat(prompt)
```
