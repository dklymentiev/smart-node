"""
CLI Session Manager - Manages persistent Claude CLI sessions
"""
import asyncio
import os
import uuid
import json
import logging
import time
from datetime import datetime
from typing import Dict, Optional, AsyncIterator
from dataclasses import dataclass, field

from config import (CLI_PATH, CLI_SESSION_TIMEOUT, CLI_MAX_SESSIONS, CLI_QUEUE_TIMEOUT,
                    SKIP_PERMISSIONS, CLI_DEFAULT_CWD, spawn_env, STREAM_LIMIT)
from cli_prompt import prompt_file
from turn_limits import node_turn_bound_seconds

logger = logging.getLogger(__name__)


# V1 sessions have no agent id. Their whole turn uses the largest registered
# ceiling; v2 agent sessions use their own effective profile.

# STREAM_LIMIT (the per-line ceiling) now comes from config, with its
# reasoning. It was declared here, and the OTHER spawn path never got it: see
# the spawn environment and the note beside SPAWN_ENV_KEYS, which these same two files already
# drifted on once.

_AGENTS_ROOT = "/srv/agents"


def _inject_agent_slug(env: dict, proc_cwd: Optional[str]) -> None:
    """Set env['SN_AGENT_SLUG'] when the session runs in an agent's own dir.

    Node tools read SN_AGENT_SLUG as the caller identity (e.g. the planner CLI
    sends it as the Remote-User header). The slug is the agent directory name
    under /srv/agents/<slug> -- sourced from the runtime, not the model prompt,
    so it cannot be forgotten or spoofed. Sessions with any other cwd get no
    slug (the tools then fail closed, which is the intended guard).
    """
    if not proc_cwd:
        return
    norm = proc_cwd.rstrip("/")
    if os.path.dirname(norm) == _AGENTS_ROOT:
        env["SN_AGENT_SLUG"] = os.path.basename(norm)


@dataclass
class Session:
    """Represents an active CLI session"""
    session_id: str
    model: str
    process: asyncio.subprocess.Process
    created_at: datetime
    last_activity: datetime
    message_count: int = 0
    system_prompt: Optional[str] = None
    service: Optional[str] = None
    ttl: int = CLI_SESSION_TIMEOUT
    cwd: Optional[str] = None
    _chat_lock: asyncio.Lock = field(default=None, repr=False)
    _stderr_lines: list = field(default_factory=list, repr=False)

    def __post_init__(self):
        if self._chat_lock is None:
            self._chat_lock = asyncio.Lock()


class SessionManager:
    """
    Manages persistent Claude CLI sessions.

    Each session maintains a long-running Claude process that communicates
    via stdin/stdout using JSON streaming format.
    """

    def __init__(self):
        self.sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(CLI_MAX_SESSIONS)

    async def create_session(
        self,
        model: str = "haiku",
        system_prompt: Optional[str] = None,
        service: Optional[str] = None,
        ttl: int = CLI_SESSION_TIMEOUT,
        cwd: Optional[str] = None
    ) -> Session:
        """
        Create new CLI session with persistent process

        Args:
            model: Claude model (haiku, sonnet, opus)
            system_prompt: Optional system prompt

        Returns:
            Session object

        Raises:
            RuntimeError: If max sessions reached
        """
        # Wait for a free slot (queue instead of reject)
        if self._semaphore.locked():
            logger.info(f"All {CLI_MAX_SESSIONS} slots busy, queuing session request...")
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=CLI_QUEUE_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Queue timeout ({CLI_QUEUE_TIMEOUT}s): all {CLI_MAX_SESSIONS} slots busy")

        session_id = str(uuid.uuid4())

        try:
            # Build command
            cmd = [
                str(CLI_PATH),
                "--print",
                "--verbose",
                "--model", model,
                "--session-id", session_id,
                "--input-format", "stream-json",
                "--output-format", "stream-json",
            ]

            if SKIP_PERMISSIONS:
                cmd.append("--dangerously-skip-permissions")

            logger.info(f"Creating session {session_id} with model {model}")

            # Allowlist + credential lookup live in config, shared with
            # backends/cli_claude.py. They used to be two copies here and there,
            # and the copies drifted.
            safe_env = spawn_env()

            # Start process
            proc_cwd = cwd if cwd and os.path.isdir(cwd) else (CLI_DEFAULT_CWD if os.path.isdir(CLI_DEFAULT_CWD) else None)
            if proc_cwd:
                logger.info(f"Session {session_id} cwd: {proc_cwd}")

            # Agent identity for tool attribution: node tools (e.g. the planner
            # CLI) read $SN_AGENT_SLUG to set their caller identity. Derive it
            # from the agent's own directory (/srv/agents/<slug>) -- the runtime,
            # not the prompt -- so the model can neither forget nor spoof who it
            # acts as. Non-agent sessions (other cwd) get no slug.
            _inject_agent_slug(safe_env, proc_cwd)

            with prompt_file(system_prompt) as (options, fds):
                process = await asyncio.create_subprocess_exec(
                    *cmd, *options,
                    pass_fds=fds,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=safe_env,
                    cwd=proc_cwd,
                    limit=STREAM_LIMIT,
                )

            now = datetime.utcnow()
            session = Session(
                session_id=session_id,
                model=model,
                process=process,
                created_at=now,
                last_activity=now,
                system_prompt=system_prompt,
                service=service,
                ttl=ttl,
                cwd=proc_cwd,
            )

            # Monitor stderr in background for crash diagnostics
            asyncio.create_task(self._read_stderr(session))

            async with self._lock:
                self.sessions[session_id] = session

            logger.info(f"Session {session_id} created successfully")
            return session

        except Exception:
            self._semaphore.release()
            raise

    async def _read_stderr(self, session: Session):
        """Read stderr in background and store for diagnostics."""
        try:
            while True:
                line = await session.process.stderr.readline()
                if not line:
                    break
                text = line.decode().strip()
                if text:
                    session._stderr_lines.append(text)
                    # Keep last 50 lines
                    if len(session._stderr_lines) > 50:
                        session._stderr_lines = session._stderr_lines[-50:]
        except Exception:
            pass

        # Process exited -- log stderr if there was a crash
        rc = session.process.returncode
        if rc and rc != 0 and session._stderr_lines:
            stderr_tail = "\n".join(session._stderr_lines[-10:])
            logger.error(f"Session {session.session_id} CLI crashed (exit={rc}):\n{stderr_tail}")

    async def _respawn_session(self, session: Session, resume: bool = False) -> Session:
        """Respawn a dead CLI process, keeping the same session_id and slot.

        resume=True passes --resume <session_id> so Claude Code restores the
        session's own on-disk transcript and the conversation survives the
        restart. Only works for sessions created with --session-id (transcript
        exists) and from the same cwd; the caller handles the no-transcript
        fallback. The fresh process re-reads .claude config (CLAUDE.md,
        .mcp.json, settings) from cwd either way.
        """
        logger.info(f"Respawning session {session.session_id} (was exit={session.process.returncode}, resume={resume})")

        cmd = [
            str(CLI_PATH),
            "--print",
            "--verbose",
            "--model", session.model,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]
        if resume:
            cmd.extend(["--resume", session.session_id])
        if SKIP_PERMISSIONS:
            cmd.append("--dangerously-skip-permissions")
        safe_env = spawn_env()

        proc_cwd = session.cwd
        _inject_agent_slug(safe_env, proc_cwd)
        with prompt_file(session.system_prompt) as (options, fds):
            process = await asyncio.create_subprocess_exec(
                *cmd, *options,
                pass_fds=fds,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=safe_env,
                cwd=proc_cwd,
                limit=STREAM_LIMIT,
            )

        session.process = process
        session.last_activity = datetime.utcnow()
        session._stderr_lines = []
        asyncio.create_task(self._read_stderr(session))

        logger.info(f"Session {session.session_id} respawned (pid={process.pid})")
        return session

    async def restart_session(self, session_id: str) -> dict:
        """Restart a session's CLI process on demand (e.g. a UI 'reload' button).

        Kills the live process and respawns with --resume so the conversation
        survives; the fresh process re-reads its .claude config (CLAUDE.md,
        .mcp.json, settings) and system prompt. Falls back to a fresh process if
        the session has no on-disk transcript yet (created before --session-id).
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        # Stop the live process first
        if session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                session.process.kill()
                await session.process.wait()

        # Respawn resuming the transcript; fall back to fresh if --resume fails
        await self._respawn_session(session, resume=True)
        await asyncio.sleep(0.5)
        resumed = session.process.returncode is None
        if not resumed:
            tail = "; ".join(session._stderr_lines[-3:]) if session._stderr_lines else "no stderr"
            logger.warning(f"Session {session_id} --resume failed ({tail}); respawning fresh")
            await self._respawn_session(session, resume=False)
            await asyncio.sleep(0.5)

        return {
            "session_id": session_id,
            "status": "restarted",
            "pid": session.process.pid,
            "resumed": resumed,
        }

    async def chat(self, session_id: str, content: str) -> dict:
        """
        Send message to session and get response.
        Auto-respawns CLI process once if it crashed before chat.

        Args:
            session_id: Session UUID
            content: User message content

        Returns:
            dict with session_id, content, tool_uses, usage

        Raises:
            ValueError: If session not found
            RuntimeError: If process terminated after respawn
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        # Auto-respawn if process died before first chat
        if session.process.returncode is not None:
            if session.message_count == 0:
                stderr_info = "; ".join(session._stderr_lines[-3:]) if session._stderr_lines else "no stderr"
                logger.warning(
                    f"Session {session_id} process died before first message "
                    f"(exit={session.process.returncode}, stderr={stderr_info}). Respawning..."
                )
                await self._respawn_session(session)
                # Brief wait for process init
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError(f"Session {session_id} process has terminated")

        async with session._chat_lock:
            # Send message as JSON (Claude CLI format)
            message = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": content
                }
            }

            input_data = json.dumps(message) + "\n"
            session.process.stdin.write(input_data.encode())
            await session.process.stdin.drain()

            # Read response
            response_text = ""
            tool_uses = []
            usage = {}
            cost_usd = None
            duration_ms = None
            content_length = None
            ceiling = node_turn_bound_seconds()
            deadline = time.monotonic() + ceiling

            while True:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    line = await asyncio.wait_for(
                        session.process.stdout.readline(),
                        timeout=remaining
                    )
                except asyncio.TimeoutError:
                    await self.close_session(session_id)
                    raise RuntimeError(f"turn_deadline_exceeded: session={session_id} limit={ceiling:g}s")
                except ValueError as exc:
                    # An output line past STREAM_LIMIT. asyncio raises a bare
                    # ValueError here (it catches LimitOverrunError itself), and
                    # unhandled it read as "the session crashed" with a message
                    # about separators and chunks -- true, and useless to whoever
                    # was reading a document at the time. Say what happened.
                    raise RuntimeError(
                        f"Session {session_id}: one output line exceeded "
                        f"{STREAM_LIMIT} bytes and could not be read ({exc}). "
                        f"The turn produced more on a single line than the "
                        f"gateway will buffer."
                    ) from exc
                if not line:
                    break

                try:
                    data = json.loads(line.decode().strip())
                    msg_type = data.get("type")

                    if msg_type == "assistant":
                        # Extract text from assistant message
                        msg = data.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "text":
                                response_text += block.get("text", "")
                            elif block.get("type") == "tool_use":
                                tool_uses.append(f"{block.get('name')}: {block.get('input', '')}")

                    elif msg_type == "result":
                        # Final result with usage stats
                        usage = data.get("usage", {})
                        cost_usd = data.get("cost_usd")
                        duration_ms = data.get("duration_ms")
                        content_length = data.get("content_length")
                        if not response_text:
                            response_text = data.get("result", "")
                        break

                    elif msg_type == "error":
                        raise RuntimeError(data.get("message", "Unknown error"))

                except json.JSONDecodeError:
                    continue

            # Update session stats
            session.last_activity = datetime.utcnow()
            session.message_count += 1

            return {
                "session_id": session_id,
                "content": response_text,
                "tool_uses": tool_uses,
                "usage": usage,
                "cost_usd": cost_usd,
                "duration_ms": duration_ms,
                "content_length": content_length,
            }

    async def chat_stream(
        self,
        session_id: str,
        content: str
    ) -> AsyncIterator[dict]:
        """
        Stream response from session

        Args:
            session_id: Session UUID
            content: User message content

        Yields:
            Response chunks as dicts
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        async with session._chat_lock:
            # Send message (Claude CLI format)
            message = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": content
                }
            }
            input_data = json.dumps(message) + "\n"
            session.process.stdin.write(input_data.encode())
            await session.process.stdin.drain()
            ceiling = node_turn_bound_seconds()
            deadline = time.monotonic() + ceiling

            # Stream response
            while True:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    line = await asyncio.wait_for(
                        session.process.stdout.readline(),
                        timeout=remaining
                    )
                except asyncio.TimeoutError:
                    await self.close_session(session_id)
                    raise RuntimeError(f"turn_deadline_exceeded: session={session_id} limit={ceiling:g}s")
                except ValueError as exc:
                    # Same as the non-streaming path above: an oversize line
                    # reports itself instead of ending the session obscurely.
                    raise RuntimeError(
                        f"Session {session_id}: one output line exceeded "
                        f"{STREAM_LIMIT} bytes and could not be read ({exc}). "
                        f"The turn produced more on a single line than the "
                        f"gateway will buffer."
                    ) from exc
                if not line:
                    break

                try:
                    data = json.loads(line.decode().strip())
                    yield data

                    if data.get("type") == "result":
                        break

                except json.JSONDecodeError:
                    continue

            session.last_activity = datetime.utcnow()
            session.message_count += 1

    async def close_session(self, session_id: str) -> dict:
        """
        Close session and terminate process

        Args:
            session_id: Session UUID

        Returns:
            dict with session_id, status, total_messages, duration_seconds

        Raises:
            ValueError: If session not found
        """
        async with self._lock:
            session = self.sessions.pop(session_id, None)

        if not session:
            raise ValueError(f"Session {session_id} not found")

        # Terminate process
        if session.process.returncode is None:
            session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                session.process.kill()
                await session.process.wait()

        # Release slot for queued requests
        self._semaphore.release()

        duration = (datetime.utcnow() - session.created_at).total_seconds()

        logger.info(f"Session {session_id} closed after {duration:.0f}s")

        return {
            "session_id": session_id,
            "status": "closed",
            "total_messages": session.message_count,
            "duration_seconds": int(duration)
        }

    def list_sessions(self) -> list:
        """List all active sessions"""
        return [
            {
                "session_id": s.session_id,
                "model": s.model,
                "service": s.service or '',
                "status": "active" if s.process.returncode is None else "terminated",
                "created_at": s.created_at,
                "last_activity": s.last_activity,
                "message_count": s.message_count,
                "ttl": s.ttl
            }
            for s in self.sessions.values()
        ]

    async def cleanup_stale_sessions(self):
        """Close sessions that exceeded their TTL (measured from last activity)."""
        now = datetime.utcnow()
        stale = []

        for session_id, session in self.sessions.items():
            idle_time = (now - session.last_activity).total_seconds()
            if idle_time > session.ttl:
                stale.append(session_id)

        for session_id in stale:
            logger.info(f"Closing stale session {session_id}")
            await self.close_session(session_id)

    async def shutdown(self):
        """Close all sessions"""
        session_ids = list(self.sessions.keys())
        for session_id in session_ids:
            try:
                await self.close_session(session_id)
            except Exception as e:
                logger.error(f"Error closing session {session_id}: {e}")
