#!/usr/bin/env python3
"""
MCP Server for Brin -- Unified Agent Routing

Exposes specialists, Rein workflows, and memory search as MCP tools.
Any MCP-compatible client (Claude Code, console-agent, Cursor) can connect
and get access to 52 specialists + 23 workflows + Mesh memory.

Usage:
    python mcp_server.py                    # stdio transport
    python mcp_server.py --transport sse    # SSE on port 8102

Environment:
    AI_GATEWAY_URL  -- Gateway URL (default: http://172.17.0.1:19850)
    MESH_API_URL    -- Mesh API URL (default: http://localhost:8000)
"""
import json
import os
import sys
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.specialist_loader import SpecialistLoader
from core.turn_budget import gateway_call_timeout, refresh_turn_ceiling
from core.router import Router
from services.inbox_service import InboxService
from services.media_service import MediaService
from services.intent_service import IntentService
from services.notification_service import NotificationService
from models.inbox import IncomingMessage, ContentKind
from models.media import VisionRequest, TranscribeRequest, GenerateRequest, DownloadRequest
from models.intent import ClassifyRequest, SearchParseRequest, ModelParseRequest
from models.notification import NotificationRequest, NotificationChannel
from models.diagnostics import DiagnosticTarget, DiagnosticRequest, AsyncDiagnosticRequest
from services.diagnostics_service import DiagnosticsService
from services.model_service import ModelService
from services.process_service import ProcessService
from services.memory_service import MemoryService
from models.model_catalog import AddModelRequest, UsageLogEntry
from models.memory import SmartSearchRequest

# ── Configuration ──

GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:19850")
MESH_URL = os.environ.get("MESH_API_URL", "http://localhost:8000").rstrip("/")
REIN_TASKS_DIR = "/srv/agents/tasks"

# ── Shared instances ──

loader = SpecialistLoader()
router = Router(loader)
_inbox = InboxService()
_media = MediaService()
_intent = IntentService()
_notify = NotificationService()
_diag = DiagnosticsService()
_models = ModelService()
_processes = ProcessService()
_memory = MemoryService()

# ── MCP Server ──

mcp = FastMCP(
    "brin",
    instructions="Unified agent routing: 52 specialists, 23 workflows, Mesh memory",
)


# ── Helper functions ──

async def _gateway_post(path: str, body: dict, timeout: float | None = None) -> dict:
    # None: the turn ceiling plus brin's margin. It was 300.0, which
    # ended a turn at five minutes whatever the node's ceiling said.
    if timeout is None:
        await refresh_turn_ceiling()
    async with httpx.AsyncClient(timeout=gateway_call_timeout() if timeout is None else timeout) as client:
        r = await client.post(f"{GATEWAY_URL}{path}", json=body)
        r.raise_for_status()
        return r.json()


async def _gateway_get(path: str, timeout: float = 30.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{GATEWAY_URL}{path}")
        r.raise_for_status()
        return r.json()


async def _gateway_delete(path: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.delete(f"{GATEWAY_URL}{path}")


async def _mesh_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{MESH_URL}{path}", json=body)
        r.raise_for_status()
        return r.json()


async def _mesh_put(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(f"{MESH_URL}{path}", json=body)
        r.raise_for_status()
        return r.json()


async def _mesh_get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{MESH_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()


# Session cache: specialist_id -> gateway session_id
_sessions: dict[str, str] = {}


async def _get_or_create_session(specialist_id: str, system_prompt: str, model: str) -> str:
    """Get existing Gateway session or create new one."""
    if specialist_id in _sessions:
        try:
            await _gateway_get(f"/api/v1/sessions/{_sessions[specialist_id]}")
            return _sessions[specialist_id]
        except Exception:
            del _sessions[specialist_id]

    data = await _gateway_post("/api/v1/sessions", {
        "model": model,
        "system_prompt": system_prompt,
        "service": f"omni-mcp:{specialist_id}",
    }, timeout=60.0)
    sid = data["session_id"]
    _sessions[specialist_id] = sid
    return sid


# ── Tools ──

@mcp.tool()
async def ask_specialist(
    specialist_id: str,
    message: str,
    model: str = "sonnet",
) -> str:
    """Ask a specialist agent a question. The specialist responds based on its expertise defined in a .md file.

    Args:
        specialist_id: Specialist ID (e.g. 'critic-specialist', 'architect-specialist'). Use list_specialists to see all.
        message: Your message/question for the specialist.
        model: AI model to use (haiku, sonnet, opus). Default: sonnet.
    """
    specialist = loader.get(specialist_id)
    if not specialist:
        available = [s.id for s in loader.list()[:10]]
        return f"Specialist '{specialist_id}' not found. Available: {', '.join(available)}..."

    session_id = await _get_or_create_session(specialist_id, specialist.content, model)

    data = await _gateway_post(
        f"/api/v1/sessions/{session_id}/chat",
        {"content": message},
    )
    return data.get("content", "")


@mcp.tool()
async def list_specialists() -> str:
    """List all available specialist agents with their titles and descriptions."""
    specialists = loader.list()
    lines = [f"Available specialists ({len(specialists)}):\n"]
    for s in specialists:
        desc = f" -- {s.description[:80]}" if s.description else ""
        lines.append(f"  {s.id}: {s.title}{desc}")
    return "\n".join(lines)


@mcp.tool()
async def run_workflow(
    flow_name: str,
    question: str,
) -> str:
    """Start a Rein multi-agent workflow (e.g. deliberation, code-review, brainstorm).
    Creates a task that the Rein daemon picks up and executes asynchronously.

    Args:
        flow_name: Workflow name (e.g. 'deliberation', 'code-review'). Use list_workflows to see all.
        question: The question or topic for the workflow to process.
    """
    from datetime import datetime, timezone
    from pathlib import Path

    flows = router.available_flows()
    if flow_name not in flows:
        return f"Workflow '{flow_name}' not found. Available: {', '.join(flows[:10])}..."

    task_id = f"task-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    task_dir = Path(REIN_TASKS_DIR) / task_id

    (task_dir / "input").mkdir(parents=True, exist_ok=True)
    (task_dir / "output").mkdir(exist_ok=True)
    (task_dir / "state").mkdir(exist_ok=True)

    task_yaml = (
        f"id: {task_id}\n"
        f"flow: {flow_name}\n"
        f"output_dir: ./output\n"
        f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
    )
    (task_dir / "task.yaml").write_text(task_yaml)
    (task_dir / "input" / "question.txt").write_text(question)
    (task_dir / "input" / "input.json").write_text(
        json.dumps({"topic": question}, ensure_ascii=False)
    )
    (task_dir / "state" / "status").write_text("pending")

    return (
        f"Workflow '{flow_name}' queued as {task_id}.\n"
        f"Rein daemon will pick it up. Check status with workflow_status('{task_id}')."
    )


@mcp.tool()
async def workflow_status(task_id: str) -> str:
    """Check the status of a running Rein workflow task.
    Shows ASCII progress bar with per-block status.

    Args:
        task_id: Task ID returned by run_workflow (e.g. 'task-20260221-183000').
    """
    import sqlite3
    from pathlib import Path

    task_dir = Path(REIN_TASKS_DIR) / task_id
    if not task_dir.exists():
        return f"Task '{task_id}' not found."

    status_file = task_dir / "state" / "status"
    file_status = status_file.read_text().strip() if status_file.exists() else "unknown"

    # Try reading detailed state from rein.db
    rein_db = task_dir / "state" / "rein.db"
    if rein_db.exists():
        try:
            conn = sqlite3.connect(str(rein_db))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            blocks = []
            for row in cur.execute(
                "SELECT name, status, phase, progress, start_time, updated_at FROM processes"
            ):
                blocks.append(dict(row))
            conn.close()

            total = len(blocks)
            done = sum(1 for b in blocks if b["status"] == "done")
            failed = sum(1 for b in blocks if b["status"] == "failed")
            running = sum(1 for b in blocks if b["status"] == "running")

            # Status
            if total == 0:
                task_status = "pending"
            elif failed > 0:
                task_status = "FAILED"
            elif done == total:
                task_status = "COMPLETED"
            elif running > 0 or done > 0:
                task_status = "RUNNING"
            else:
                task_status = "pending"

            # Progress bar
            pct = int(done / total * 100) if total > 0 else 0
            bar_len = 30
            filled = int(bar_len * done / total) if total > 0 else 0
            bar = "#" * filled + "-" * (bar_len - filled)

            lines = []
            lines.append(f"  Task:     {task_id}")
            lines.append(f"  Status:   {task_status}")
            lines.append(f"  Progress: [{bar}] {pct}% ({done}/{total})")
            lines.append("")
            lines.append("  Blocks:")

            # Status icons (ASCII)
            icons = {"done": "[OK]", "running": "[>>]", "failed": "[!!]", "pending": "[..]"}

            for b in blocks:
                icon = icons.get(b["status"], "[??]")
                name = b["name"]
                phase = b.get("phase") or ""
                dur = ""
                if b.get("start_time") and b.get("updated_at") and b["status"] == "done":
                    d = round(b["updated_at"] - b["start_time"], 1)
                    dur = f" ({d}s)"
                phase_str = f" -- {phase}" if phase else ""
                lines.append(f"    {icon} {name}{phase_str}{dur}")

            # If completed, show output snippet
            if task_status == "COMPLETED":
                lines.append("")
                output_dir = task_dir / "output"
                if output_dir.exists():
                    for f in sorted(output_dir.iterdir()):
                        if f.suffix in (".json", ".md", ".txt"):
                            content = f.read_text()[:1500]
                            lines.append(f"  Output ({f.name}):")
                            lines.append(f"  {content}")
                            break

            return "\n".join(lines)

        except Exception as e:
            pass  # Fall through to simple status

    # Simple status (no rein.db yet)
    result = f"  Task:   {task_id}\n  Status: {file_status}"

    if file_status == "completed":
        output_dir = task_dir / "output"
        if output_dir.exists():
            for f in sorted(output_dir.iterdir()):
                if f.name.endswith(".json"):
                    try:
                        data = json.loads(f.read_text())
                        result += f"\n\n  Output ({f.name}):\n  {json.dumps(data, indent=2, ensure_ascii=False)[:2000]}"
                    except json.JSONDecodeError:
                        result += f"\n\n  Output ({f.name}):\n  {f.read_text()[:2000]}"
                    break

    return result


@mcp.tool()
async def list_workflows() -> str:
    """List all available Rein workflows that can be started with run_workflow."""
    flows = router.available_flows()
    lines = [f"Available workflows ({len(flows)}):\n"]
    for f in flows:
        lines.append(f"  {f}")
    return "\n".join(lines)


@mcp.tool()
async def search_memory(
    query: str,
    limit: int = 5,
) -> str:
    """Semantic search in Mesh memory. Finds documents by meaning, not exact words.

    Args:
        query: What to search for (natural language).
        limit: Max results (default 5).
    """
    data = await _mesh_post("/search", {"query": query, "limit": limit})
    results = data.get("results", [])
    if not results:
        return f"No results for '{query}'."

    lines = [f"Found {len(results)} results for '{query}':\n"]
    for r in results:
        guid = r.get("guid", "?")
        score = r.get("similarity", 0)
        content = r.get("content", "")[:200]
        tags = ", ".join(r.get("tags", []))
        lines.append(f"**{guid}** (score: {score:.3f})")
        lines.append(f"  tags: {tags}")
        lines.append(f"  {content}\n")
    return "\n".join(lines)


@mcp.tool()
async def save_memory(
    content: str,
    tags: Optional[list[str]] = None,
) -> str:
    """Save a document to Mesh memory with optional tags. Auto-tagged with date and source.

    Args:
        content: Document text (min 10 chars).
        tags: Optional tags (e.g. ['type:worklog', 'topic:architecture']).
    """
    body = {"content": content, "source": "omni-mcp"}
    if tags:
        body["tags"] = tags
    data = await _mesh_put("/", body)
    guid = data.get("guid", "?")
    return f"Saved: {guid}"


# ── Inbox & Media tools ──

@mcp.tool()
async def save_to_inbox(
    text: str,
    source: str = "mcp",
    file_path: Optional[str] = None,
    kind: Optional[str] = None,
) -> str:
    """Save an incoming message to Mesh memory with auto-tagging (kind, platform, date).

    Args:
        text: Message text or caption.
        source: Origin of the message (e.g. 'telegram', 'cli', 'web'). Default: 'mcp'.
        file_path: Optional path to attached file.
        kind: Content kind override: text, url, photo, video, audio, voice, document.
    """
    msg = IncomingMessage(
        text=text,
        source=source,
        file_path=file_path,
        kind=ContentKind(kind) if kind else None,
    )
    result = await _inbox.save(msg)
    if result.error:
        return f"Error: {result.error}"
    return (
        f"Saved to inbox: {result.guid}\n"
        f"Kind: {result.kind}\n"
        f"Tags: {', '.join(result.tags)}\n"
        f"Preview: {result.content_preview}"
    )


@mcp.tool()
async def recognize_image(
    image_path: str,
    model: Optional[str] = None,
    prompt: Optional[str] = None,
) -> str:
    """Analyze an image using Vision AI. Returns description and extracted text.

    Args:
        image_path: Absolute path to the image file (JPEG, PNG, GIF, WebP).
        model: Vision model to use (default: Gemini 2.0 via OpenRouter).
        prompt: Custom prompt for analysis.
    """
    req = VisionRequest(image_path=image_path, model=model, prompt=prompt)
    result = await _media.recognize_image(req)
    if result.error:
        return f"Error: {result.error}"
    parts = [f"Description: {result.description}"]
    if result.text:
        parts.append(f"Extracted text: {result.text}")
    parts.append(f"Model: {result.model} | {result.time_sec}s | {result.file_size_kb}KB")
    if result.cost is not None:
        parts.append(f"Cost: ${result.cost}")
    return "\n".join(parts)


@mcp.tool()
async def transcribe_audio(
    audio_path: str,
    model: Optional[str] = None,
) -> str:
    """Transcribe audio to text using Whisper (local) or Gemini (API).

    Args:
        audio_path: Absolute path to the audio file (MP3, OGG, WAV, M4A).
        model: Model: 'faster-whisper:small', 'faster-whisper:medium', or 'google/gemini-2.0-flash-001'.
    """
    req = TranscribeRequest(audio_path=audio_path, model=model)
    result = await _media.transcribe_audio(req)
    if result.error:
        return f"Error: {result.error}"
    return (
        f"Transcription:\n{result.text}\n\n"
        f"Model: {result.model} | {result.time_sec}s | {result.file_size_kb}KB"
    )


@mcp.tool()
async def generate_image(
    prompt: str,
    model: Optional[str] = None,
    width: int = 1024,
    height: int = 1024,
) -> str:
    """Generate an image from a text prompt using Flux (Together AI).

    Args:
        prompt: Text description of the image to generate.
        model: Flux model: 'FLUX.1-schnell' (fast), 'FLUX.1-dev', 'FLUX.1-pro' (best).
        width: Image width in pixels (default: 1024).
        height: Image height in pixels (default: 1024).
    """
    req = GenerateRequest(prompt=prompt, model=model, width=width, height=height)
    result = await _media.generate_image(req)
    if result.error:
        return f"Error: {result.error}"
    parts = [
        f"Image saved: {result.image_path}",
        f"Prompt: {result.prompt}",
        f"Model: {result.model} | {result.width}x{result.height} | {result.steps} steps",
        f"Time: {result.time_sec}s",
    ]
    if result.cost is not None:
        parts.append(f"Cost: ${result.cost}")
    return "\n".join(parts)


@mcp.tool()
async def download_video(
    url: str,
    max_duration: int = 600,
    max_filesize_mb: int = 100,
) -> str:
    """Download a video from TikTok, Instagram, YouTube, Twitter, etc. via yt-dlp.

    Args:
        url: Video URL.
        max_duration: Max video duration in seconds (default: 600 = 10 min).
        max_filesize_mb: Max file size in MB (default: 100).
    """
    req = DownloadRequest(url=url, max_duration=max_duration, max_filesize_mb=max_filesize_mb)
    result = await _media.download_video(req)
    if result.error:
        return f"Error: {result.error}"
    parts = [
        f"Downloaded: {result.file_path}",
        f"Platform: {result.platform}",
    ]
    if result.title:
        parts.append(f"Title: {result.title}")
    if result.duration is not None:
        parts.append(f"Duration: {result.duration}s")
    if result.file_size_mb is not None:
        parts.append(f"Size: {result.file_size_mb}MB")
    return "\n".join(parts)


# ── Intent & Notification tools ──

@mcp.tool()
async def classify_intent(text: str) -> str:
    """Classify user message intent using NLU (Gemini Flash).
    Returns one of: diagnostic, question, file_op, save, search,
    model_settings, image_generation, video_download, video_analyze,
    hint, chat, unknown.

    Args:
        text: The user message to classify.
    """
    result = await _intent.classify(text)
    if result.error:
        return json.dumps({"intent": result.intent, "error": result.error})
    return json.dumps({"intent": result.intent, "confidence": result.confidence})


@mcp.tool()
async def send_notification(
    text: str,
    channel: str = "telegram",
    recipient: Optional[str] = None,
    subject: Optional[str] = None,
) -> str:
    """Send notification via Telegram or Email.

    Args:
        text: Message text to send.
        channel: Delivery channel: 'telegram' or 'email'.
        recipient: Email address or Telegram chat_id override. Defaults to owner.
        subject: Email subject line (email only).
    """
    req = NotificationRequest(
        text=text,
        channel=NotificationChannel(channel),
        recipient=recipient,
        subject=subject,
    )
    result = await _notify.send(req)
    return json.dumps(result.model_dump(), ensure_ascii=False)


@mcp.tool()
async def parse_model_command(text: str) -> str:
    """Parse model settings command into structured action, service, and model.
    Returns JSON with action (list/get/set/stats), service, and model.

    Args:
        text: Natural language model command (e.g. 'use gemini for photos', 'show models', 'stats').
    """
    result = await _intent.parse_model_command(text)
    return json.dumps(result.model_dump(), ensure_ascii=False)


@mcp.tool()
async def parse_search(text: str) -> str:
    """Parse natural language search query into structured parameters.
    Returns scope, kind, platform, and query.

    Args:
        text: Natural language search query (e.g. 'show tiktok links').
    """
    result = await _intent.parse_search_query(text)
    return json.dumps(result.model_dump(), ensure_ascii=False)


# ── Diagnostics tools ──

@mcp.tool()
async def run_diagnostic(target: str = "general") -> str:
    """Run a system diagnostic check (sync, up to 30s timeout).
    Targets: cpu, memory, disk, docker, fail2ban, general, processes, system.

    Args:
        target: Diagnostic target (default: 'general').
    """
    req = DiagnosticRequest(target=DiagnosticTarget(target))
    result = await _diag.run_diagnostic(req)
    if result.error:
        return f"Error ({result.target}): {result.error}"
    return f"[{result.target}] (exit={result.exit_code}, {result.time_sec}s)\n{result.output}"


@mcp.tool()
async def run_diagnostic_async(target: str = "general") -> str:
    """Start a diagnostic check in the background, returns task_id for polling.
    Targets: cpu, memory, disk, docker, fail2ban, general, processes, system.

    Args:
        target: Diagnostic target (default: 'general').
    """
    req = AsyncDiagnosticRequest(target=DiagnosticTarget(target))
    result = await _diag.run_diagnostic_async(req)
    if result.error:
        return f"Error: {result.error}"
    return f"Task started: {result.task_id} (target: {result.target})"


@mcp.tool()
async def diagnostic_task_status(task_id: str) -> str:
    """Check status of an async diagnostic task.

    Args:
        task_id: Task ID returned by run_diagnostic_async.
    """
    result = await _diag.get_task_status(task_id)
    if result.status == "not_found":
        return f"Task '{task_id}' not found."
    if result.status == "running":
        return f"Task '{task_id}' is still running (target: {result.target})."
    if result.error:
        return f"Task '{task_id}' failed: {result.error}"
    return f"[{result.target}] status={result.status} (exit={result.exit_code}, {result.time_sec}s)\n{result.output}"


# ── Model tools ──

@mcp.tool()
async def list_models(service: Optional[str] = None) -> str:
    """List available AI models in the catalog. Optionally filter by service type.

    Args:
        service: Filter by service type: vision, audio, chat, intent, image_gen.
    """
    result = await _models.list_models(service)
    if not result.models:
        return f"No models available{f' for {service}' if service else ''}."
    current_svc = None
    lines = [f"Model catalog ({len(result.models)} models):\n"]
    for m in result.models:
        if m.service != current_svc:
            current_svc = m.service
            lines.append(f"\n[{current_svc.upper()}]")
        price = "FREE" if m.is_free else "paid"
        lines.append(f"  {m.alias}: {m.model_id} [{price}]")
    return "\n".join(lines)


@mcp.tool()
async def get_user_model(user_id: int, service: str) -> str:
    """Get user's preferred AI model for a service.

    Args:
        user_id: User ID (Telegram chat_id or similar).
        service: Service type: vision, audio, chat, intent, image_gen.
    """
    model_id = await _models.get_user_model(user_id, service)
    return f"User {user_id} model for {service}: {model_id}"


@mcp.tool()
async def set_user_model(user_id: int, service: str, alias_or_model: str) -> str:
    """Set user's preferred AI model for a service.

    Args:
        user_id: User ID.
        service: Service type: vision, audio, chat, intent, image_gen.
        alias_or_model: Model alias (e.g. 'gemini-flash') or full model ID (e.g. 'google/gemini-2.5-flash').
    """
    result = await _models.set_user_model(user_id, service, alias_or_model)
    if result.error:
        return f"Error: {result.error}"
    return result.message


@mcp.tool()
async def model_usage_stats(
    user_id: Optional[int] = None,
    service: Optional[str] = None,
    days: int = 30,
) -> str:
    """Get AI model usage statistics.

    Args:
        user_id: Filter by user (optional).
        service: Filter by service type (optional).
        days: Number of days to look back (default: 30).
    """
    stats = await _models.get_usage_stats(user_id, service, days)
    lines = [f"Usage stats (last {days} days):"]
    lines.append(f"  Requests: {stats.total_requests}")
    lines.append(f"  Cost: ${stats.total_cost_usd:.4f}")
    lines.append(f"  Time: {stats.total_time_sec:.1f}s")
    if stats.by_service:
        lines.append("\nBy service:")
        for svc, data in stats.by_service.items():
            lines.append(f"  {svc}: {data.requests} req, ${data.cost_usd:.4f}")
    return "\n".join(lines)


# ── Process tools ──

@mcp.tool()
async def process_status(process_id: str) -> str:
    """Check status of a background process.

    Args:
        process_id: Process ID returned by register.
    """
    info = await _processes.get_status(process_id)
    if not info:
        return f"Process '{process_id}' not found."
    lines = [
        f"Process: {info.name} ({info.id})",
        f"Type: {info.type}",
        f"Status: {info.status.value}",
    ]
    if info.status.value == "running":
        lines.append(f"Progress: {info.progress:.0f}%")
    if info.error:
        lines.append(f"Error: {info.error}")
    if info.created_at:
        lines.append(f"Created: {info.created_at[:19]}")
    return "\n".join(lines)


@mcp.tool()
async def list_processes(active_only: bool = False, limit: int = 20) -> str:
    """List background processes.

    Args:
        active_only: If true, show only running/paused/pending processes.
        limit: Max number of processes to return (default: 20).
    """
    if active_only:
        result = await _processes.list_active()
    else:
        result = await _processes.list_recent(limit)

    if not result.processes:
        return "No processes found."

    lines = [f"Processes ({result.total}):"]
    for p in result.processes:
        progress = f" {p.progress:.0f}%" if p.status.value == "running" else ""
        lines.append(f"  [{p.status.value.upper()}]{progress} {p.id}: {p.name}")
    return "\n".join(lines)


@mcp.tool()
async def stop_process(process_id: str) -> str:
    """Stop a running background process.

    Args:
        process_id: Process ID to stop.
    """
    result = await _processes.stop(process_id)
    if not result.success:
        return f"Failed to stop {process_id}: {result.error}"
    return f"Process {process_id} stopped."


# ── Memory tools ──

@mcp.tool()
async def smart_memory_search(
    query: str,
    scope: Optional[str] = None,
    kind: Optional[str] = None,
    platform: Optional[str] = None,
    limit: int = 10,
) -> str:
    """Smart memory search with query cleaning, keyword translation, and parallel strategies.

    Args:
        query: Search query (natural language, Russian or English).
        scope: Where to search: inbox, notes, worklog, or None for all.
        kind: Content type filter: url, photo, video, etc.
        platform: Platform filter: tiktok, youtube, instagram, etc.
        limit: Max results (default: 10).
    """
    req = SmartSearchRequest(query=query, scope=scope, kind=kind, platform=platform, limit=limit)
    result = await _memory.search(req)

    if result.error:
        return f"Search error: {result.error}"
    if not result.documents:
        return f"No results for '{query}'."

    lines = [f"Found {result.total} results (strategy: {result.search_strategy}):\n"]
    for doc in result.documents:
        score = f" (score: {doc.similarity_score:.3f})" if doc.similarity_score else ""
        tags = ", ".join(doc.tags[:5])
        content = doc.content[:200]
        lines.append(f"**{doc.guid}**{score}")
        if tags:
            lines.append(f"  tags: {tags}")
        lines.append(f"  {content}\n")
    return "\n".join(lines)


# ── Resources ──

@mcp.resource("omni://health")
async def health_resource() -> str:
    """System health: specialist count, workflow count."""
    specialists = loader.list()
    workflows = router.available_flows()
    return json.dumps({
        "status": "ok",
        "specialists": len(specialists),
        "workflows": len(workflows),
    }, indent=2)


@mcp.resource("omni://specialists")
async def specialists_resource() -> str:
    """List of all available specialists."""
    specialists = loader.list()
    data = [{"id": s.id, "title": s.title, "description": s.description} for s in specialists]
    return json.dumps(data, indent=2, ensure_ascii=False)


@mcp.resource("omni://workflows")
async def workflows_resource() -> str:
    """List of all available Rein workflows."""
    flows = router.available_flows()
    return json.dumps([{"name": f} for f in flows], indent=2)


# ── Entry point ──

if __name__ == "__main__":
    # SSE heartbeat -- prevent proxy/NAT timeout disconnects
    try:
        sys.path.insert(0, "/srv/scripts")
        from shared.mcp_heartbeat import patch_sse_heartbeat
        patch_sse_heartbeat()
    except ImportError:
        pass

    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    if transport == "sse":
        mcp.settings.port = 8102
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
