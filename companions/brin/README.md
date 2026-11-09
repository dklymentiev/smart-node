# Brin

Unified agent routing API. Thin binding layer that routes messages to AI specialists (via AI Gateway) or multi-agent workflows (via Rein), with integrated memory, media, NLU, and process management.

**Version:** 0.2.0
**Port:** 8101 (HTTP API), 8102 (MCP/SSE)
**Auth:** `X-API-Key` header on all `/api/*` endpoints

## Architecture

```
Client (webchat, telegram, API)
    |
    v
POST /api/message { message, model, stream, context }
    |
    v
Router -- decides: DirectAgent or ReinWorkflow
    |                           |
    v                           v
Executor.run_direct()     Executor.run_workflow()
    |                           |
    [memory enrichment]     Create task dir
    |                       for Rein daemon
    v
AI Gateway session
    |
    v
Response + memory metadata
```

## Quick Start

```bash
cd /opt/smart-node/brin
source venv/bin/activate
python main.py  # HTTP API on :8101
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_GATEWAY_URL` | `http://172.17.0.1:19850` | AI Gateway endpoint |
| `MESH_API_URL` | `http://localhost:8000` | Mesh memory API |
| `BRIN_API_KEY` | (from `/etc/smart-node/secrets/brin-api-key`) | API authentication key |
| `MEMORY_SEARCH_TIMEOUT` | `5` | Memory search timeout (seconds) |
| `MODEL_DB_PATH` | `_storage/models.db` | SQLite for model catalog |
| `PROCESS_DB_PATH` | `_storage/processes.db` | SQLite for process tracking |
| `EVENT_DB_PATH` | `_storage/events.db` | SQLite for event queue |
| `REIN_TASKS_DIR` | `/srv/agents/tasks` | Rein tasks directory for watcher |

## API Reference

### Core: Message Routing

#### `POST /api/message`

Main endpoint. Routes message to specialist or workflow.

**Request:**
```json
{
  "message": "analyze this code",
  "model": "sonnet",
  "stream": false,
  "context": {"specialist": "critic-specialist"}
}
```

**Response (DirectAgent):**
```json
{
  "content": "AI response text...",
  "specialist": "critic-specialist",
  "memory": {
    "documents": [
      {
        "guid": "doc_abc123",
        "content": "Related memory document...",
        "tags": ["type:worklog", "topic:code-review"],
        "created_at": "2026-02-21T10:00:00Z",
        "similarity_score": 0.85
      }
    ],
    "total": 3,
    "query": "analyze this code",
    "search_strategy": "smart"
  }
}
```

The `memory` field is present only when memory enrichment is enabled and documents are found. See [Memory Enrichment](#memory-enrichment) below.

**Response (ReinWorkflow):**
```json
{
  "task_id": "task-20260221-143022",
  "flow": "deliberation",
  "status": "pending",
  "message": "Workflow 'deliberation' queued as task-20260221-143022."
}
```

**Routing priority:**
1. `@specialist-id message` -- routes to named specialist
2. Workflow trigger keywords (deliberation, code-review, brainstorm, etc.) -- creates Rein task
3. Default -- routes to `main` specialist (or first available)

#### `GET /api/specialists`

List all available specialists.

```json
[
  {"id": "analyst-specialist", "title": "Analyst Specialist", "description": "..."},
  {"id": "creator-specialist", "title": "Creator Specialist", "description": "..."}
]
```

#### `GET /api/specialists/{id}`

Get specialist details including full system prompt content.

#### `POST /api/specialists/reload`

Force reload all specialist `.md` files from `/srv/agents/specialists/`.

#### `GET /api/workflows`

List available Rein workflows.

#### `GET /api/workflows/{task_id}/status`

Check status of a running Rein workflow task.

---

### Memory

#### `POST /api/memory/smart-search`

Smart memory search with query cleaning and parallel strategies.

**Request:**
```json
{
  "query": "recent worklogs about rein",
  "scope": null,
  "kind": null,
  "platform": null,
  "limit": 10
}
```

**Query processing pipeline:**
1. Skip greetings/short messages
2. Clean stop words (ru/en)
3. Translate keywords (tiktok, youtube, etc.)
4. Detect temporal queries (recent, yesterday, etc.)
5. Detect platform mentions

**Search strategies (run in parallel):**
- Semantic search (always)
- Recent search (if temporal query)
- Platform tag search (if platform detected)
- URL tag search (if link-related query)

Results merged by guid (no duplicates).

#### `POST /api/memory/context`

Same as smart-search but returns formatted context string for prompt injection.

```json
{
  "context": "[MEMORY CONTEXT]\nFound 3 relevant document(s)...\n---\n",
  "document_count": 3
}
```

---

### Memory Enrichment

Configurable via `tools.yaml`. When `mode: auto-inject`, the executor automatically searches Mesh memory before every message and prepends relevant context.

**Configuration (`tools.yaml`):**
```yaml
tools:
  memory:
    enabled: true
    mode: auto-inject    # auto-inject | on-demand | off
    min_score: 0.3       # minimum similarity score
    limit: 5             # max documents to fetch
    max_context_length: 500  # max chars per document
    timeout: 5           # search timeout in seconds
```

**Behavior:**
- `auto-inject`: searches memory on every message, prepends `[MEMORY CONTEXT]` block
- `on-demand`: (reserved for future) client must explicitly request memory
- `off`: no memory enrichment

**Properties:**
- Hot reload: changes to `tools.yaml` take effect without restart (mtime-based)
- Non-fatal: if Mesh is down, message sends normally without memory
- Score filtering: documents below `min_score` are excluded
- Response includes `memory` field with search results for client-side rendering

---

### Inbox

#### `POST /api/inbox`

Save incoming message to Mesh with auto-tagging by content type.

**Request:**
```json
{
  "text": "https://example.com/video",
  "source": "telegram",
  "user_id": 123
}
```

Auto-detects content kind (url, photo, video, text) and platform (tiktok, youtube, etc.).

---

### Media

#### `POST /api/media/vision`

Analyze image using Vision AI (OpenRouter).

```json
{"image_path": "/tmp/screenshot.jpg", "prompt": "Describe this image", "model": "gemini"}
```

#### `POST /api/media/transcribe`

Transcribe audio to text (OpenAI Whisper or Gemini).

```json
{"audio_path": "/tmp/voice.ogg", "model": "whisper"}
```

#### `POST /api/media/generate`

Generate image from text prompt (DALL-E or Flux).

```json
{"prompt": "A sunset over mountains", "model": "dall-e-3", "width": 1024, "height": 1024}
```

#### `POST /api/media/download`

Download video from URL (yt-dlp).

```json
{"url": "https://youtube.com/watch?v=...", "max_duration": 300, "max_filesize_mb": 50}
```

---

### Intent (NLU)

#### `POST /api/intent/classify`

Classify user message intent via Gemini Flash.

```json
{"text": "show cpu usage"}
```

Returns: `{"intent": "diagnostic", "confidence": "high"}`

#### `POST /api/intent/parse-search`

Parse natural language search query into structured parameters.

```json
{"text": "find tiktok links from inbox"}
```

Returns: `{"scope": "inbox", "kind": "url", "platform": "tiktok", "query": "links"}`

#### `POST /api/intent/parse-model`

Parse model settings command.

```json
{"text": "use gemini for vision"}
```

Returns: `{"action": "set", "service": "vision", "model": "gemini-2.5-pro"}`

---

### Notifications

#### `POST /api/notify`

Send notification via Telegram or Email.

```json
{"text": "Deployment complete", "channel": "telegram", "recipient": "123456789"}
```

---

### Diagnostics

#### `POST /api/diagnostics/run`

Run system diagnostic (sync, up to 30s).

```json
{"target": "general"}
```

Targets: `general`, `cpu`, `memory`, `disk`, `network`, `services`.

#### `POST /api/diagnostics/run-async`

Start diagnostic in background, returns `task_id`.

#### `GET /api/diagnostics/task/{task_id}`

Check async diagnostic status.

---

### Model Catalog

#### `GET /api/models?service=chat`

List available models, optionally filtered by service.

#### `POST /api/models`

Add model to catalog.

```json
{"alias": "fast", "model_id": "anthropic/claude-haiku", "service": "chat"}
```

#### `DELETE /api/models/{alias}/{service}`

Remove model from catalog.

#### `GET /api/models/user/{user_id}/{service}`

Get user's preferred model for a service.

#### `PUT /api/models/user/{user_id}/{service}`

Set user's preferred model.

```json
{"alias_or_model": "opus"}
```

#### `GET /api/models/user/{user_id}`

Get all model settings for a user.

#### `DELETE /api/models/user/{user_id}?service=chat`

Clear user model settings (reset to defaults).

#### `POST /api/models/usage`

Log model usage for statistics.

#### `GET /api/models/stats?user_id=42&service=chat&days=30`

Get aggregated usage statistics.

---

### Events (EventBus)

Persistent event queue backed by SQLite. Events survive restarts (unread events are drained on startup). Supports in-process pub/sub, SSE streaming, and TTL-based auto-cleanup.

#### `POST /api/events`

Emit an event manually.

```json
{"event_type": "system.alert", "payload": {"msg": "disk 90%"}, "source": "monitor"}
```

Returns: `{"event_id": 1, "event_type": "system.alert"}`

#### `GET /api/events?event_type=rein.task_done&limit=20`

List recent events, optionally filtered by type.

```json
{
  "events": [
    {"id": 1, "event_type": "rein.task_done", "source": "rein_watcher",
     "payload": {"task_id": "task-20260223-143022", "flow": "deliberation"},
     "created_at": "2026-02-23T14:35:00", "read": false}
  ],
  "total": 1,
  "unread": 1
}
```

#### `GET /api/events/stream` (public, no auth)

Server-Sent Events stream for real-time updates. Each event is a JSON object:

```
data: {"id": 1, "event_type": "rein.task_done", "source": "rein_watcher", "payload": {...}}
```

**Event types and TTL:**

| Event | TTL | Source | Description |
|-------|-----|--------|-------------|
| `process.completed` | 24h | ProcessService | Background process finished |
| `process.failed` | 7d | ProcessService | Background process failed |
| `rein.task_done` | 72h | ReinWatcher | Rein workflow completed |
| `rein.task_failed` | 72h | ReinWatcher | Rein workflow failed |
| `rein.block_done` | 1h | ReinWatcher | Rein workflow block completed |
| `memory.ready` | 1h | ContextEnricher | Deferred memory search finished |
| `system.alert` | 7d | Manual/monitor | System alert |

**Auto-notifications:** `process.failed` and `rein.task_failed` events automatically trigger Telegram notifications.

---

### Process Management

#### `POST /api/processes`

Register a new background process.

```json
{"process_type": "download", "name": "Video download", "input_data": {"url": "..."}}
```

#### `GET /api/processes?active_only=true&limit=20`

List processes.

#### `GET /api/processes/{id}`

Get process status.

#### `POST /api/processes/{id}/stop`
#### `POST /api/processes/{id}/pause`
#### `POST /api/processes/{id}/resume`

Process lifecycle control.

#### `PATCH /api/processes/{id}/progress`

Update progress (0-100).

```json
{"progress": 75}
```

#### `GET /api/processes/{id}/logs?limit=50`

Get process log entries.

#### `POST /api/processes/cleanup?days=7`

Delete old processes.

---

### Health

#### `GET /health` (public, no auth)

```json
{
  "status": "ok",
  "specialists": 53,
  "workflows": 26,
  "dependencies": {
    "ai_gateway": {"ok": true, "latency_ms": 168.4},
    "mesh": {"ok": true, "latency_ms": 311.6}
  }
}
```

---

## MCP Server

Exposes all capabilities as MCP tools for Claude Code, console-agent, Cursor, etc.

```bash
python mcp_server.py                    # stdio transport
python mcp_server.py --transport sse    # SSE on port 8102
```

**Available MCP tools:**
- `ask_specialist` -- Ask a specialist agent
- `list_specialists` -- List all specialists
- `run_workflow` -- Start Rein workflow
- `workflow_status` -- Check workflow status
- `search_memory` / `save_memory` -- Memory operations
- `save_to_inbox` -- Save to inbox
- `recognize_image` / `transcribe_audio` / `generate_image` / `download_video` -- Media
- `classify_intent` / `parse_search` / `parse_model_command` -- NLU
- `send_notification` -- Telegram/Email
- `run_diagnostic` -- System diagnostics
- `list_models` / `set_user_model` / `model_usage_stats` -- Model catalog
- `process_status` / `list_processes` -- Process management
- `emit_event` / `list_events` -- EventBus

---

## Project Structure

```
/opt/smart-node/brin/
|-- main.py                  # FastAPI HTTP API (:8101)
|-- mcp_server.py            # MCP server (stdio/:8102)
|-- tools.yaml               # Tool configuration (hot reload)
|-- requirements.txt
|-- core/
|   |-- executor.py          # Runs specialists via Gateway + memory enrichment
|   |-- router.py            # Message routing (@mention, keywords, default)
|   |-- specialist_loader.py # Loads .md files with mtime cache
|   |-- tools.py             # ToolsConfig loader (tools.yaml)
|   |-- auth.py              # X-API-Key authentication
|   |-- health.py            # Dependency health checks
|   |-- logging_config.py    # Structured JSON logging + correlation IDs
|-- models/
|   |-- actions.py           # DirectAgent, ReinWorkflow
|   |-- memory.py            # SmartSearchRequest, MemoryDocument, etc.
|   |-- inbox.py             # IncomingMessage, ContentKind
|   |-- media.py             # Vision, transcribe, generate, download
|   |-- intent.py            # Classify, search parse, model parse
|   |-- notification.py      # Telegram/Email
|   |-- diagnostics.py       # System diagnostics
|   |-- process.py           # Background processes
|   |-- model_catalog.py     # Model management
|   |-- event.py             # EventBus models
|-- services/
|   |-- event_store.py       # SQLite event storage with TTL
|   |-- event_bus.py         # Async pub/sub + SSE + drain
|   |-- rein_watcher.py      # Polls Rein tasks, emits events
|   |-- memory_service.py    # Smart search with query cleaning
|   |-- inbox_service.py     # Auto-tagging save to Mesh
|   |-- media_service.py     # Vision, audio, image gen, video download
|   |-- intent_service.py    # NLU via Gemini Flash
|   |-- notification_service.py  # Telegram/Email
|   |-- diagnostics_service.py   # System diagnostics
|   |-- model_service.py     # Model catalog (SQLite)
|   |-- process_service.py   # Process tracking (SQLite)
|-- tests/                   # 229 tests
|-- logs/                    # Structured JSON logs
|-- _storage/                # SQLite databases
```

## Logging

Structured JSON to `logs/brin.log` + console. Includes correlation IDs (via `X-Correlation-ID` header).

```json
{
  "timestamp": "2026-02-21T15:30:45",
  "level": "INFO",
  "service": "brin",
  "correlation_id": "abc123",
  "logger": "brin.executor",
  "message": "Gateway chat ok",
  "duration_ms": 1234.5
}
```

## Dependencies

- Python 3.10+
- FastAPI, uvicorn, httpx, pydantic
- MCP SDK (`mcp>=1.26.0`)
- PyYAML (for tools.yaml)
- AI Gateway (for specialist chat)
- Mesh API (for memory search)
