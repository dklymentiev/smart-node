"""ClaudeCliBackend -- Claude Code subprocess provider (opaque).

Stage 2b-2. Wraps `claude` CLI as an asyncio subprocess and
adapts it to SessionBackend ABC from Stage 2b-1.

Subprocess pipeline ported from the v1 SessionManager (session_manager.py).
Old SessionManager continues serving /api/v1/sessions; this backend serves the
v2 session-first path via SessionService + sessions_v2_routes.

Opacity: gateway cannot observe CLI-internal tool calls or its own Anthropic
API token counts. Usage is reported when CLI emits a `result` envelope with
usage; otherwise estimated via tiktoken.

State: SessionService creates a NEW backend instance per resolve_backend()
call (see session_service.get_backend docstring -- per-session caching is
Stage 2c). Process handles must therefore live at MODULE level, not instance.
"""
import asyncio
import glob
import json
import logging
import os
import re
import pwd
import signal
import time
import uuid
from collections import deque
from typing import Dict, List, Optional, Any

import anthropic_relay
import db
import model_tokens
import openai_relay
import secrets_store
from core_api_proof import service_headers
from cli_prompt import PROMPT_STDIN_FLAG, prompt_file, write_prompt_frame
from backends import SessionBackend, BackendResponse
from config import (
    CLI_PATH,
    FLINT_PATH,
    FLINT_ENABLED,
    FLINT_SKIP_PERMISSIONS,
    FLINT_SKIP_PERMISSIONS_USERS,
    flint_stdio_safe,
    spawn_env,
    CLI_DEFAULT_CWD,
    SKIP_PERMISSIONS,
    CLI_RUNAS_ENABLED,
    SUDO_PATH,
    KILL_PATH,
    STREAM_LIMIT,
    WRAPPER_PATH,
)
from turn_limits import turn_limit_seconds
from config import MODEL_RELAY

# Names a runtime reads its instructions from (core-api agent_fs.INSTRUCTION_FILES).
AGENT_INSTRUCTION_FILES = ("CLAUDE.md", "FLINT.md", "AGENTS.md")

logger = logging.getLogger(__name__)


# A quiet tool may use the whole registered turn; stdout silence has no
# second, shorter deadline. The effective profile is loaded at send() start.


class TurnLimitExceeded(RuntimeError):
    def __init__(self, session_id: str, seconds: float):
        self.session_id = session_id
        self.seconds = seconds
        super().__init__(f"turn_deadline_exceeded: session={session_id} ran for {seconds:g}s")


class SpawnCapReached(RuntimeError):
    """The node is at its process cap. A momentary condition of the node, not a
    fault of the session: the turn is refused, the session stays usable and the
    same message can be sent again when a slot frees (sessions_v2_routes answers
    503 with Retry-After and does not fail the session)."""


_TURN_LIMITS: Dict[str, float] = {}
SPAWN_GRACE_SEC = 0.5    # give CLI a moment to init before first stdin write
CLOSE_WAIT_SEC = 5       # graceful terminate timeout before SIGKILL

# Module-level shared state: backend instances are short-lived (one per
# resolve_backend call), but the subprocess MUST persist across HTTP requests.
_PROCESSES: Dict[str, asyncio.subprocess.Process] = {}
_STDERR_TAILS: Dict[str, list] = {}
_SPAWN_LOCK: Optional[asyncio.Lock] = None

# Per-session chat lock. A CLI session is a single stdin/stdout pipe pair, so a
# turn (write user message -> read until result) MUST NOT overlap with another
# turn on the same session: two coroutines calling proc.stdout.readline() raise
# "readuntil() called while another coroutine is already waiting for incoming
# data" and corrupt the stream. v1 SessionManager guarded this with a per-session
# _chat_lock; this restores that guarantee for the v2 backend.
_SEND_LOCKS: Dict[str, asyncio.Lock] = {}

# When each session last had a turn. A live `claude` process is kept
# after its turn ON PURPOSE, so --resume can restore the conversation. Until now
# the only thing that ever reclaimed one was a NEW spawn hitting SN_CLI_MAX_PROCS
# and evicting an idle session -- so on a node where no fifth session ever
# arrives, the first four processes live forever. That is not a hypothetical:
# archive-node held exactly four, all children of one gateway, four days old, ninety
# minutes of CPU each, burning a steady 3-4% while doing nothing.
#
# An idle process is now reclaimed on a clock instead of on cap pressure.
_LAST_USED: Dict[str, float] = {}

# When the current in-flight turn started, keyed by session_id. Set when the
# send lock is acquired; cleared in the send() finally block and in every
# cleanup path (close, idle sweep, shutdown_all, cap eviction). The restart
# guard uses this to distinguish a live turn (age < its registered ceiling) from a
# stale lock (age >= its registered ceiling) so it can break the stale lock rather
# than refusing indefinitely.
_TURN_STARTED: Dict[str, float] = {}

# Live tool call name per session. Written each time an assistant message
# carries a tool_use block; cleared in send() finally and every cleanup path that
# pops _TURN_STARTED. Lets the desk-activity endpoint show the UI what the agent
# is doing right now instead of a content-free spinner.
_TURN_STATUS: Dict[str, dict] = {}

# Conversation key -> session_id mapping. Written once in create() from
# metadata["conversation"]. Never removed (stale entries are harmless -- any lookup
# checks _TURN_STARTED, which IS cleaned up, so dead entries just return idle).
# Lets get_desk_activity() find the right session without knowing its ID.
_SESSION_CONVERSATION: Dict[str, str] = {}
_SESSION_RUNTIME: Dict[str, str] = {}

# Which agent each session serves, and the last finished turn of each agent
# The Studio card shows an agent's console: every turn in flight on
# any channel (runner, desk, ...) and, when idle, the turn it finished last.
# The ring of a session is reset when its NEXT turn starts, so the finished
# turn is copied here at the end or it is gone. One entry per agent.
_SESSION_AGENT: Dict[str, str] = {}
_AGENT_LAST_TURN: Dict[str, dict] = {}

# Which OS user each live session's CLI is running as, or None for "the gateway
# account itself". This has to survive the spawn, not just guide it: a
# process owned by another user CANNOT be signalled by the gateway account at
# all. Measured on test-node -- a direct kill returns "Operation not permitted" and
# the child goes on living -- so every reclaim path (idle sweep, turn timeout,
# cap eviction, shutdown) must know to route the signal through sudo. Without
# this dict the privilege drop would quietly reintroduce the orphan leak of
# the gateway believing it had killed things it had not.
_RUNAS: Dict[str, Optional[str]] = {}

# per-session classification so the dual-pool cap check can count
# background vs person-facing slots separately. Values: "person" | "background".
# Set at spawn time; cleared alongside _RUNAS at every cleanup point.
_SESSION_TYPES: Dict[str, str] = {}
# A chat wake's messages (session metadata "answer_ids"). Once each has a
# successful inbox_reply the turn is over: the answers are in the chat, and the
# model's closing call after them ("answered, message closed") read the whole
# context again for nothing while the agent stayed busy, 5 to 9 s a chat
# (2026-10-29). The turn is ended with the CLI's own interrupt.
_ANSWER_IDS: Dict[str, set] = {}
# The chat a colleague's chat wake works for (session metadata "wake_desk"):
# the colleague was @mentioned in that chat and answers there, in a session of
# its own, so the chat's live line and its Stop reach this session too
#.
_WAKE_DESK: Dict[str, str] = {}


def _answered_id(block: Dict[str, Any], reply_calls: Dict[str, int]) -> Optional[int]:
    """The message a tool_result block answered, when it is the successful result
    of an inbox_reply call in reply_calls; None otherwise. A refusal comes back
    as {"error": ...} (the messaging tool reports HTTP errors that way)."""
    mid = reply_calls.get(block.get("tool_use_id"))
    if mid is None or block.get("is_error"):
        return None
    body = block.get("content")
    if isinstance(body, list):
        body = "".join(p.get("text", "") for p in body if isinstance(p, dict))
    try:
        parsed = json.loads(body) if isinstance(body, str) else body
    except ValueError:
        return None
    return mid if isinstance(parsed, dict) and parsed.get("ok") is True else None

# When each live process was spawned (wall clock), for the process table.
_SPAWNED_AT: Dict[str, float] = {}


# Messages that were sent by the person but died before reaching the agent's
# transcript. If both retry attempts in _send_locked fail, the user's text ends
# up in ai_session_messages but NOT in the CLI transcript, and every subsequent
# turn is a conversation where the two sides hold different histories.
# On the next send to the same session we pop the stored message and prepend it
# as a context note so the agent is not silently missing a turn. Cleared on
# close() so stale entries do not outlive the session.
_UNDELIVERED: Dict[str, str] = {}

# Who asked to stop the turn in flight, keyed by session_id. Set by
# interrupt(); read by _send_locked when the turn ends, so a turn the CLI ended
# because it was told to reads as STOPPED rather than as a crash to retry or a
# failure to report. Cleared with _TURN_STARTED.
_INTERRUPTS: Dict[str, dict] = {}
# Sessions whose in-flight turn has been written to the CLI's stdin. Before
# that, a stop only has to keep the message from being sent; after it, the CLI
# has to be told.
_TURN_WRITTEN: set = set()

IDLE_TIMEOUT_SEC = float(os.environ.get("SN_CLI_IDLE_TIMEOUT", "900"))   # 15 min
IDLE_SWEEP_SEC = float(os.environ.get("SN_CLI_IDLE_SWEEP", "60"))
_IDLE_TASK: Optional[asyncio.Task] = None


def live_process_count() -> int:
    """How many CLI children are alive right now. Exposed so the count is visible
    on the health endpoint instead of being discovered by `ps` on a bad day."""
    return sum(1 for p in _PROCESSES.values() if p.returncode is None)


def _current_user() -> str:
    """The account this process is running as. Empty string if it has no passwd
    entry, which is not fatal -- it only means we never skip the sudo hop."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return ""


def _runas_user(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """The OS user this session's CLI must run as, or None to stay in-process.

    session_service resolves `agents.os_user` and puts it in session metadata
    (session_service.py:106). Previously the spawn ignored it, so every agent
    woken through the gateway arrived as smart-node-gateway and its per-agent
    group membership -- the entire access mechanism of a project work area --
    applied to nobody. This is the value being picked back up.

    A session with no os_user (ad-hoc, non-agent) is unchanged and runs as the
    gateway, which is what it has always done.
    """
    if not CLI_RUNAS_ENABLED or not metadata:
        return None
    user = (metadata.get("os_user") or "").strip()
    if not user or user == _current_user():
        return None
    return user


async def _signal_group(pid: int, sig: str, os_user: Optional[str]) -> None:
    """Signal the whole process group of `pid`, as whoever owns it.

    The group and not the pid, because `claude` has children of its own and
    terminating only the parent is how they end up orphaned and spinning.
   Through sudo when the child belongs to an agent, because the gateway account cannot signal another user's process -- measured, not
    assumed: a direct kill answers "Operation not permitted" and the child
    survives it.
    """
    signum = signal.SIGTERM if sig == "TERM" else signal.SIGKILL
    if os_user:
        try:
            p = await asyncio.create_subprocess_exec(
                SUDO_PATH, "-n", "-u", os_user, KILL_PATH, f"-{sig}", "--", f"-{pid}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await p.communicate()
            if p.returncode != 0:
                # Loud on purpose. A signal that silently does not arrive is the
                # orphan leak wearing a clean log.
                logger.error("claude-cli sudo kill -%s pgid=%s as %s FAILED rc=%s: %s",
                             sig, pid, os_user, p.returncode,
                             (err or b"").decode(errors="replace").strip())
        except Exception as e:
            logger.error("claude-cli sudo kill -%s pgid=%s as %s errored: %s",
                         sig, pid, os_user, e)
        return
    try:
        os.killpg(os.getpgid(pid), signum)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _touch_session(session_id: str) -> None:
    """Stamp the session row as active now. Never raises.

    The session reaper (gateway-ops/session-reaper.py) is a second killer this
    module does not control: a root timer that judges idleness by
    ai_sessions.last_activity_at alone. That column moves only when a message
    row is written, and a CLI turn writes its one assistant row at the END, so
    a turn longer than the reaper's window read as idle from its first second
    and was killed mid-work. Seen on a field node: three developer runs in one
    night closed as `reaped` fifteen to nineteen minutes in, work uncommitted.
    The sweep stamps every turn in flight so both killers mean the same thing
    by "working". The turn's registered ceiling stays the one bound on a turn.
    """
    try:
        db.query(
            "UPDATE sn_core.ai_sessions SET last_activity_at = now() WHERE id = %s",
            (session_id,),
        )
    except Exception as e:
        logger.warning("claude-cli session=%s: activity stamp failed: %s", session_id, e)


def _journal_reclaim(session_id: str, pid: int, reason: str) -> None:
    """Write a reclaim into the node's journal. Never raises.

    Every path that ends a CLI process comes through _kill_tree, and until now
    the only trace was a line in this service's log. The node's own journal did
    not know an agent's process had been ended, so "why did the run stop" could
    be answered only by someone with a shell. Called while the session's
    bookkeeping is still in place, which is why it runs before the signal.
    """
    model_tokens.revoke_session(session_id)
    try:
        now = time.monotonic()
        started = _TURN_STARTED.get(session_id)
        last = _LAST_USED.get(session_id)
        payload = {
            "agent": _agent_of(session_id) or None,
            "reason": reason,
            "pid": pid,
            "turn_in_flight": started is not None,
            "turn_seconds": round(now - started) if started is not None else None,
            "idle_seconds": round(now - last) if started is None and last is not None else None,
        }
        # Written here with db rather than through context_handoff.emit_event:
        # that module brings httpx and the budget code into this one's imports.
        db.query(
            "INSERT INTO sn_core.actors (id, display_name, type, layer) "
            "VALUES ('component:gateway', 'component:gateway', 'component', 'system') "
            "ON CONFLICT (id) DO NOTHING"
        )
        db.query(
            "INSERT INTO sn_core.events (actor, event_type, entity_type, entity_id, payload) "
            "VALUES ('component:gateway', 'session.reclaimed', 'ai_session', %s, %s::jsonb)",
            (session_id, json.dumps(payload)),
        )
    except Exception as e:
        # The reclaim itself must still happen: a process left running because
        # its obituary could not be written is the leak this module exists to stop.
        logger.error("claude-cli session=%s: reclaim not journaled (%s): %s",
                     session_id, reason, e)


async def _kill_tree(session_id: str, proc, reason: str,
                     os_user: Optional[str] = None) -> None:
    """SIGTERM the process GROUP, then SIGKILL what is left."""
    if proc is None or proc.returncode is not None:
        return
    pid = proc.pid
    if os_user is None:
        os_user = _RUNAS.get(session_id)
    logger.warning("claude-cli reclaiming session=%s pid=%s user=%s: %s",
                   session_id, pid, os_user or _current_user(), reason)
    _journal_reclaim(session_id, pid, reason)
    await _signal_group(pid, "TERM", os_user)
    if not os_user:
        # Same last resort as before for a gateway-owned child. Never for an
        # agent-owned one: proc.terminate() raises PermissionError there, and
        # catching it would report a kill that did not happen.
        try:
            await asyncio.wait_for(proc.wait(), timeout=CLOSE_WAIT_SEC)
            return
        except (asyncio.TimeoutError, Exception):
            await _signal_group(pid, "KILL", os_user)
            try:
                proc.kill()
            except Exception:
                pass
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=CLOSE_WAIT_SEC)
    except (asyncio.TimeoutError, Exception):
        await _signal_group(pid, "KILL", os_user)


async def _idle_sweep_once() -> int:
    """Reclaim sessions idle past the timeout. Returns how many were reclaimed.

    Never touches a session with a turn in flight: its send lock is held, and a
    long turn is work, not a leak. That is the same test the cap eviction uses.
    Such a session is stamped active instead, for the reaper (_touch_session)."""
    now = time.monotonic()
    reclaimed = 0
    for sid, proc in list(_PROCESSES.items()):
        # The lock first: a send holding it owns the process entry, dead or
        # alive. A respawn that exits at once ("Session ID is already in use")
        # is still being read by that send, which retries the other way; dropping
        # its bookkeeping here ended the turn in KeyError(session_id) -> 502.
        lock = _SEND_LOCKS.get(sid)
        if lock is not None and lock.locked():
            _touch_session(sid)
            continue
        if proc.returncode is not None:
            _forget_process(sid)
            continue
        last = _LAST_USED.get(sid)
        if last is None:
            _LAST_USED[sid] = now       # first sighting: give it a full window
            continue
        idle = now - last
        if idle < IDLE_TIMEOUT_SEC:
            continue
        await _kill_tree(sid, proc, "idle %.0fs > %.0fs" % (idle, IDLE_TIMEOUT_SEC))
        _forget_process(sid)
        reclaimed += 1
    return reclaimed


async def _idle_sweeper() -> None:
    while True:
        try:
            await asyncio.sleep(IDLE_SWEEP_SEC)
            n = await _idle_sweep_once()
            if n:
                logger.info("claude-cli idle sweep reclaimed %d process(es), %d live",
                            n, live_process_count())
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # a sweep must never die quietly
            logger.warning("claude-cli idle sweep error: %s", e)


def start_idle_sweeper() -> None:
    """Called once from the app lifespan. Idempotent."""
    global _IDLE_TASK
    if _IDLE_TASK is None or _IDLE_TASK.done():
        _IDLE_TASK = asyncio.ensure_future(_idle_sweeper())


async def shutdown_all(reason: str = "gateway shutting down") -> int:
    """Kill every CLI child we know of. The gateway owning the lifetime of what
    it spawns means owning the END of it too: a restart must not leave children
    behind, which is exactly what was found on a live node -- orphans whose parents were
    three DIFFERENT gateway pids."""
    global _IDLE_TASK
    if _IDLE_TASK is not None:
        _IDLE_TASK.cancel()
        _IDLE_TASK = None
    killed = 0
    for sid, proc in list(_PROCESSES.items()):
        if proc.returncode is None:
            await _kill_tree(sid, proc, reason)
            killed += 1
        _PROCESSES.pop(sid, None)
        _LAST_USED.pop(sid, None)
        _SEND_LOCKS.pop(sid, None)
        _TURN_STARTED.pop(sid, None)
        _TURN_STATUS.pop(sid, None)
        _INTERRUPTS.pop(sid, None)
        _RUNAS.pop(sid, None)
        _SESSION_TYPES.pop(sid, None)
        _TURN_TEXT.pop(sid, None)
        _TURN_STEERS.pop(sid, None)
        _SESSION_RUNTIME.pop(sid, None)
    if killed:
        logger.info("claude-cli shutdown reclaimed %d process(es)", killed)
    return killed


# One short line saying WHAT the live tool is touching. The
# name alone ("Read", "Bash") leaves the person unable to tell a turn doing the
# right thing from one lost in the wrong file. Only named arguments are taken,
# and the result is cut hard: a tool input can carry a whole file, and this ends
# up in a browser.
_DETAIL_KEYS = ("command", "file_path", "path", "pattern", "query", "url",
                "notebook_path", "ref", "slug", "title")


# The steps of the turn in flight, per session. A ring: the chat shows
# the last few, nobody scrolls a running turn's history. Reset when a turn starts
# and dropped with the rest of the turn state when it ends.
_TURN_STEPS: Dict[str, "deque"] = {}
_TURN_STEP_SEQ: Dict[str, int] = {}
_STEPS_MAX = 40
_TURN_TEXT: Dict[str, str] = {}

# The CLI injects task-tool reminders into a session and tells the model never to
# mention them. Models answer the reminder anyway, in the text of the turn, with a
# sentence such as "Ignoring the reminder, planner is my task system." Every text
# block of a turn is delivered to the person, so that sentence reaches them: it
# opened one reply in four on a desk where it was counted, and people quoted it
# back asking what it meant. The sentence is the model talking to the harness, not
# to the person, so it is dropped at the one place the turn's text is gathered.
# It starts a line, names a reminder within the first hundred characters and ends
# at the first full stop; anything else is left alone.
_REMINDER_ACK = re.compile(
    r"(?im)^[ \t]*Ignoring[ \t]+(?:the|that|this|those)\b[^\n]{0,100}?\breminders?\b"
    r"[^\n.]*(?:\.+|$)[ \t]*\n?")


def _strip_reminder_ack(text: str) -> str:
    """Drop the model's answer to a runtime reminder from a block of reply text.
    '' when the block held nothing else."""
    if not text:
        return text
    return _REMINDER_ACK.sub("", text).lstrip("\n")


_TURN_STEERS: Dict[str, dict] = {}
_STEER_ACKS: Dict[str, asyncio.Future] = {}


# Who watches a chat's turn live. Each watcher holds a queue; any change
# to a turn's live state (a step, more text, the turn starting or ending) drops
# the session id into every queue, and each watcher sends its chat's state if it
# changed. The chat used to poll this once a second, and a hidden or occluded
# tab stopped polling, so a person saw "thinking" until the whole answer landed.
# Nothing here is stored: it is the live view, the answer itself is the history.
_LIVE_WATCHERS: set = set()


def _live_changed(session_id: str) -> None:
    for q in list(_LIVE_WATCHERS):
        try:
            q.put_nowait(session_id)
        except asyncio.QueueFull:
            pass    # the watcher has a change pending already; it reads the latest state


async def desk_live(desk_id: str, ping_seconds: float = 15.0):
    """Server-sent events for one chat: its activity (as get_desk_activity) on
    every change, a comment line while nothing happens so proxies keep the
    connection. A burst of text arrives as one frame per tenth of a second."""
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _LIVE_WATCHERS.add(q)
    try:
        last = None
        while True:
            body = json.dumps(get_desk_activity(desk_id), ensure_ascii=False)
            if body != last:
                last = body
                yield f"data: {body}\n\n"
            try:
                await asyncio.wait_for(q.get(), timeout=ping_seconds)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            await asyncio.sleep(0.1)
            while not q.empty():
                q.get_nowait()
    finally:
        _LIVE_WATCHERS.discard(q)


def _push_step(session_id: str, kind: str, text: str, desc: str = "") -> None:
    """Record one visible step. Never raises: the turn matters, the log does not."""
    try:
        s = " ".join(str(text or "").split())
        if not s:
            return
        if len(s) > 160:
            s = s[:160] + "..."
        seq = _TURN_STEP_SEQ.get(session_id, 0) + 1
        _TURN_STEP_SEQ[session_id] = seq
        buf = _TURN_STEPS.get(session_id)
        if buf is None:
            buf = deque(maxlen=_STEPS_MAX)
            _TURN_STEPS[session_id] = buf
        step = {"seq": seq, "kind": kind, "text": s, "t": int(time.time())}
        if desc:
            step["desc"] = desc
        buf.append(step)
        _live_changed(session_id)
    except Exception:
        pass


def _tool_detail(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in _DETAIL_KEYS:
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            s = " ".join(v.split())
            return s[:80] + ("..." if len(s) > 80 else "")
    return ""


def _tool_desc(inp) -> str:
    """The model's own one-line description of a tool call, or "".

    The model writes a `description` for each Bash call. It is the plain-words
    line the chat shows; the command (which may name a path or a secret) stays
    in the step text for the console. No description gives "", never the command.
    """
    if not isinstance(inp, dict):
        return ""
    v = inp.get("description")
    if not isinstance(v, str) or not v.strip():
        return ""
    s = " ".join(v.split())
    return s[:120] + ("..." if len(s) > 120 else "")


def get_turn_activity(session_id: str) -> dict:
    """Return live turn status for a session.

    Called by the desk-activity endpoint so the UI can show what the agent is
    doing instead of a content-free spinner.

    Returns {"in_progress": False} if no turn is currently running.
    Returns {"in_progress": True, "elapsed": N, "action": "<tool>",
             "action_elapsed": N} while a turn is in flight and a tool call has
    been seen.
    Returns {"in_progress": True, "elapsed": N, "action": None} if a turn is
    running but no tool call has been seen yet (the agent is thinking).
    """
    started = _TURN_STARTED.get(session_id)
    if started is None:
        return {"in_progress": False}
    now = time.monotonic()
    elapsed = int(now - started)
    status = _TURN_STATUS.get(session_id)
    if status:
        return {
            "in_progress": True,
            "elapsed": elapsed,
            "can_steer": _SESSION_RUNTIME.get(session_id) == "flint-cli",
            "action": status["name"],
            "detail": status.get("detail") or "",
            "action_elapsed": int(now - status["ts"]),
        }
    return {"in_progress": True, "elapsed": elapsed, "action": None,
            "can_steer": _SESSION_RUNTIME.get(session_id) == "flint-cli"}


def get_desk_activity(desk_id: str, since: int = 0) -> dict:
    """Return live turn status for any session serving a desk.

    The gateway's conversation key for a desk is "<agent>:desk:<uuid>" (the
    executor uses the agent-prefixed form). Match on suffix so both the prefixed
    and bare forms are caught.

    Scans _SESSION_CONVERSATION for a session whose conversation key ends with
    "desk:<desk_id>" and whose turn is currently in flight, then returns its
    activity. First match wins (there should be at most one live desk turn).
    Returns {"in_progress": False} if no matching session is active.
    """
    needle = f"desk:{desk_id}"
    # The chat's own turn first, then a colleague's chat wake working for it.
    own = [sid for sid, conv in list(_SESSION_CONVERSATION.items())
           if conv == needle or conv.endswith(f":{needle}")]
    wakes = [sid for sid, d in list(_WAKE_DESK.items()) if d == desk_id and sid not in own]
    for sid in own + wakes:
        activity = get_turn_activity(sid)
        if not activity.get("in_progress"):
            continue
        conv = _SESSION_CONVERSATION.get(sid, "")
        # whose turn it is: the chat's agent, or the colleague answering in it
        activity["agent"] = _SESSION_AGENT.get(sid) or conv.split(":", 1)[0] or None
        activity["colleague"] = sid in wakes
        # Everything after the cursor the caller already has.
        # `seq` is the whole turn's counter, not the ring's, so a client
        # that fell behind more than the ring holds sees a jump rather
        # than silently repeated steps.
        buf = _TURN_STEPS.get(sid) or ()
        activity["steps"] = [s for s in buf if s["seq"] > int(since or 0)]
        activity["seq"] = _TURN_STEP_SEQ.get(sid, 0)
        activity["text"] = _TURN_TEXT.get(sid, "")
        activity["steers"] = list(_TURN_STEERS.get(sid, {}).values())
        # a message typed in the chat is for the chat's agent, never steered
        # into a colleague's turn
        activity["can_steer"] = sid in own and _SESSION_RUNTIME.get(sid) == "flint-cli"
        return activity
    for sid, conv in list(_SESSION_CONVERSATION.items()):
        if (conv == needle or conv.endswith(f":{needle}")) and _TURN_STEERS.get(sid):
            return {"in_progress": False, "steers": list(_TURN_STEERS[sid].values())}
    return {"in_progress": False}


def desk_session_in_flight(desk_id: str, agent: Optional[str] = None,
                           colleagues: bool = False) -> Optional[str]:
    """The session whose turn for this desk is running right now, or None.

    Answered from the process table, the same place the activity comes from, so
    a Stop reaches the turn the person is watching. The Studio used to
    look the session up in core-api by a conversation key brin stopped writing,
    found nothing, and printed "Stopped." over a turn that carried on.
    """
    needle = f"desk:{desk_id}"
    # A Stop for a colleague answering in this chat (its chat wake, _WAKE_DESK)
    # reaches that colleague's turn; the agent named is how the page says so.
    if colleagues and agent:
        for sid, d in list(_WAKE_DESK.items()):
            who = _SESSION_AGENT.get(sid) or _SESSION_CONVERSATION.get(sid, "").split(":", 1)[0]
            if d == desk_id and sid in _TURN_STARTED and who == agent:
                return sid
    # Otherwise the chat's own turn, as before (its agent is not checked: a Stop
    # of the chat stops the chat's turn).
    for sid, conv in list(_SESSION_CONVERSATION.items()):
        if (conv == needle or conv.endswith(f":{needle}")) and sid in _TURN_STARTED:
            return sid
    return None


def desks_in_progress() -> list:
    """Every desk with a turn in flight, one entry each.

    For Studio's chat tabs: which conversations are being worked on now,
    polled once for all of them. This is the process table, not the agent lease:
    a lease says an agent is claimed, this says a turn is actually running, and
    for a chat the second is what the person asks about.
    """
    out = []
    for sid, conv in list(_SESSION_CONVERSATION.items()):
        if ":desk:" not in f":{conv}":
            continue
        activity = get_turn_activity(sid)
        if not activity.get("in_progress"):
            continue
        agent, _, desk_id = conv.rpartition("desk:")
        out.append({
            "desk_id": desk_id,
            "agent": agent.rstrip(":") or None,
            "session_id": sid,
            "stopping": sid in _INTERRUPTS,
            **activity,
        })
    return out


def _agent_of(session_id: str) -> str:
    """The agent a session serves: its metadata slug, else the conversation's
    prefix ("agent-b:runner-agent-b-..." / "agent-b:desk:...")."""
    slug = _SESSION_AGENT.get(session_id)
    if slug:
        return slug
    conv = _SESSION_CONVERSATION.get(session_id, "")
    return conv.split(":", 1)[0] if ":" in conv else ""


def _remember_last_turn(session_id: str) -> None:
    """Copy a finishing turn's steps for the agent console. Never raises."""
    try:
        agent = _agent_of(session_id)
        started = _TURN_STARTED.get(session_id)
        if not agent or started is None:
            return
        _AGENT_LAST_TURN[agent] = {
            "session_id": session_id,
            "conversation": _SESSION_CONVERSATION.get(session_id, ""),
            "elapsed": int(time.monotonic() - started),
            "ended_at": int(time.time()),
            "steps": list(_TURN_STEPS.get(session_id) or ()),
        }
    except Exception:
        pass


def agent_activity(agent: str) -> dict:
    """An agent's console: every turn of it in flight, whatever started
    it, each with its whole ring of steps, plus the turn it finished last.

    The whole ring and not a cursor: an agent can run several turns at once and
    a ring is at most _STEPS_MAX short lines, so the client keys steps by
    (session_id, seq) instead of keeping one cursor per session. Tool RESULTS
    are not here because they are never recorded (see _push_step).
    """
    turns = []
    for sid in list(_SESSION_CONVERSATION.keys()):
        if _agent_of(sid) != agent:
            continue
        activity = get_turn_activity(sid)
        if not activity.get("in_progress"):
            continue
        turns.append({
            "session_id": sid,
            "conversation": _SESSION_CONVERSATION.get(sid, ""),
            "stopping": sid in _INTERRUPTS,
            **activity,
            "steps": list(_TURN_STEPS.get(sid) or ()),
            "seq": _TURN_STEP_SEQ.get(sid, 0),
        })
    return {"agent": agent, "turns": turns, "last": _AGENT_LAST_TURN.get(agent)}


def _forget_process(session_id: str) -> None:
    """Drop the per-process bookkeeping of a session whose process is gone:
    reclaimed, evicted, stopped or found dead. The conversation, agent and
    undelivered-message entries stay, so the next message respawns with
    --resume and the console still knows whose session it was."""
    for table in (_PROCESSES, _LAST_USED, _SPAWNED_AT, _SEND_LOCKS, _TURN_STARTED,
                  _TURN_STATUS, _RUNAS, _SESSION_TYPES, _TURN_TEXT, _TURN_STEERS,
                  _SESSION_RUNTIME):
        table.pop(session_id, None)


def processes(agent: Optional[str] = None) -> list:
    """The process table: every live CLI process, whose it is and what it does.

    This is the ground truth the panel shows and a stop acts on. A process
    here is alive now; an agent absent from here runs nothing, whatever a
    lease or a status row says. In-process, safe to poll every few seconds.
    """
    now = time.monotonic()
    out = []
    for sid, proc in list(_PROCESSES.items()):
        if proc.returncode is not None:
            continue
        slug = _agent_of(sid)
        if agent and slug != agent:
            continue
        turn = get_turn_activity(sid)
        last = _LAST_USED.get(sid)
        out.append({
            "session_id": sid,
            "agent": slug or None,
            "os_user": _RUNAS.get(sid),
            "runtime": _SESSION_RUNTIME.get(sid),
            "pid": proc.pid,
            "session_type": _SESSION_TYPES.get(sid),
            "conversation": _SESSION_CONVERSATION.get(sid, ""),
            "spawned_at": _SPAWNED_AT.get(sid),
            "idle_seconds": (None if turn.get("in_progress") or last is None
                             else int(now - last)),
            "stopping": sid in _INTERRUPTS,
            "turn": turn,
        })
    out.sort(key=lambda p: (p["agent"] or "", p["session_id"]))
    return out


async def stop_agent(agent: str, requested_by: Optional[str] = None,
                     reason: str = "stopped by owner") -> dict:
    """End every live process of an agent now.

    A turn in flight is marked interrupted first, so its send loop reads the
    end of the process as STOPPED and not as a crash to respawn and retry
    (the reason interrupt() exists); that loop then clears its own turn
    bookkeeping. An idle process is killed and forgotten as the idle sweep
    does. Sessions are kept: once the agent is enabled again, the next message
    respawns with --resume.
    """
    interrupted, closed = [], []
    for sid, proc in list(_PROCESSES.items()):
        if proc.returncode is not None or _agent_of(sid) != agent:
            continue
        in_turn = sid in _TURN_STARTED
        if in_turn:
            _INTERRUPTS.setdefault(sid, {"by": requested_by, "at": time.time()})
            interrupted.append(sid)
        await _kill_tree(sid, proc, "%s (by %s)" % (reason, requested_by or "unknown"),
                         os_user=_RUNAS.get(sid))
        if not in_turn:
            _forget_process(sid)
        closed.append(sid)
    return {"agent": agent, "interrupted": interrupted, "closed": closed}


def _get_lock() -> asyncio.Lock:
    """Lazy-init module lock so we bind to the running event loop, not import-time."""
    global _SPAWN_LOCK
    if _SPAWN_LOCK is None:
        _SPAWN_LOCK = asyncio.Lock()
    return _SPAWN_LOCK


def _send_lock(session_id: str) -> asyncio.Lock:
    """Per-session lock, lazily created on the running event loop."""
    lock = _SEND_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SEND_LOCKS[session_id] = lock
    return lock


# Session ids already warned about an unreadable transcript directory, so the
# diagnosis is printed once and not once per spawn.
_UNREADABLE_WARNED: set = set()
# The group the agent CLI state must be readable by; the gateway user is in it.
_AGENT_GROUP = "smart-node-agents"


def _transcript_exists(session_id: str, os_user: Optional[str] = None) -> bool:
    """True if Claude CLI already wrote a transcript for this session id.

    A respawn of an existing session MUST use --resume: a second
    `--session-id <id>` over an existing transcript makes the CLI exit with
    "Session ID is already in use" (-> EOF before result -> 502). The first send
    of a brand-new session has no transcript yet, so it still uses --session-id.

    LOOK IN THE RIGHT HOME. Until the privilege drop, every CLI ran as
    the gateway and every transcript pooled in the gateway's home, so reading
    $HOME was right by accident. Running the CLI as the agent moves the file to
    the AGENT's home, and a lookup still pointed at $HOME would answer "no
    transcript" for a session that has one -- which is not a cosmetic miss: the
    caller then passes --session-id instead of --resume and the CLI refuses,
    giving a 502 on the second message of every panel chat. It is invisible
    until a session is resumed, which is not the first thing anyone tests.
    """
    if os_user:
        try:
            home = pwd.getpwnam(os_user).pw_dir
        except KeyError:
            logger.warning("claude-cli unknown os_user %r for transcript lookup; "
                           "falling back to own HOME", os_user)
            home = os.environ.get("HOME") or os.path.expanduser("~")
    else:
        home = os.environ.get("HOME") or os.path.expanduser("~")
    projects = os.path.join(home, ".claude", "projects")
    # glob() swallows a permission error and returns [], which reads exactly
    # like "no transcript". Log it at WARNING: _session_has_turns() is now
    # the authoritative resume oracle, so an unreadable directory is
    # no longer fatal -- it only means the supplementary filesystem check is
    # skipped and the DB answer stands.
    if os.path.isdir(projects) and not os.access(projects, os.R_OK | os.X_OK):
        # SAY WHAT FIXES IT, once per session.
        #
        # The answer below is still the filesystem's, and the respawn-the-other-
        # way fallback still covers a wrong guess. But when the DB holds a
        # session id the CLI never wrote a transcript for -- brin writes the row
        # before the CLI writes anything -- BOTH ways fail, the turn crashes
        # exit=1, and the wake retries. On the live node after 1.12.23 that was
        # 316 spawns in eight minutes, zero completed turns, zero spend, while
        # validate reported 195/195 and the release marker looked right.
        #
        # The remedy is one line and nobody could see it from here, so it is now
        # in the message. Logged once per session id: the old code logged on
        # every call, which is how a real diagnosis ends up buried under itself.
        if session_id not in _UNREADABLE_WARNED:
            _UNREADABLE_WARNED.add(session_id)
            logger.error(
                "claude-cli cannot read %s (running as %s): a resumable session "
                "will be misread as new and the CLI will refuse it. Fix the mode: "
                "the directory must be 0750 and group %s, which is what the "
                "gateway user is a member of.",
                projects, _current_user(), _AGENT_GROUP)
    pattern = os.path.join(projects, "*", f"{session_id}.jsonl")
    return bool(glob.glob(pattern))


# The two things the CLI says when the resume flag is on the wrong side. Kept
# together so a reader can see they are one decision failing in either
# direction, not two unrelated crashes.
_RESUME_MISMATCH = re.compile(
    r"No conversation found with session ID|Session ID .* is already in use",
    re.IGNORECASE)


def _session_has_turns(session_id: str) -> bool:
    """True if this session already exchanged a turn, according to the node's
    own records rather than the filesystem.

    _transcript_exists() answers the same question by looking for the CLI's
    transcript file, and after the privilege drop it CANNOT: the transcript
    lives in the agent's home, mode 0700, and the gateway runs as itself. The
    lookup does not fail loudly either -- an unreadable directory is not a
    directory, so it returns "no transcript", the respawn passes --session-id
    over a session that exists, and the CLI exits "Session ID is already in
    use" -> EOF -> 502. Seen live on 2026-08-30: the owner wrote to an agent
    that was still thinking about his previous message, and the second message
    was lost. From his side the chat simply went quiet.

    The database knows without needing anyone's home directory. The question is
    WHICH row counts. Any row does not: the caller records the person's message
    before the CLI is spawned, so a brand-new session already has a row by the
    time this is asked, the first spawn is handed --resume, and the CLI exits
    "No conversation found with session ID". That is not a rare race -- it is
    every first turn, and on a node with no prior transcripts it is every turn,
    each one answered with an empty string. Found on stand test-node while running
    the 1.12.24 acceptance suite: three consecutive turns, HTTP 200, len=0.

    Only a completed assistant turn proves the CLI wrote a transcript for this
    id. Wrong only in one harmless direction -- a session whose first turn died
    before recording an answer is treated as new, which is exactly what it is.
    """
    try:
        rows = db.query(
            "SELECT 1 FROM sn_core.ai_session_messages "
            "WHERE session_id = %s AND role = 'assistant' "
            "AND content IS NOT NULL AND content <> '' LIMIT 1",
            (session_id,), fetch=True,
        )
        return bool(rows)
    except Exception as exc:  # never let bookkeeping stop a respawn
        logger.warning("claude-cli session=%s could not check history in DB (%s); "
                       "falling back to the transcript lookup alone", session_id, exc)
        return False


def _parse_model(full_model_id: str) -> str:
    """'claude-cli/sonnet' -> 'sonnet'. Strip provider prefix."""
    if "/" not in full_model_id:
        raise ValueError(
            f"model_id {full_model_id!r} missing provider prefix"
        )
    return full_model_id.split("/", 1)[1]


_AGENTS_ROOT = os.environ.get("SN_AGENTS_ROOT", "/srv/agents")


def _inject_agent_slug(env: Dict[str, str], cwd: Optional[str]) -> None:
    """Set env['SN_AGENT_SLUG'] when the session runs in an agent's own dir.

    Node tools read SN_AGENT_SLUG as the caller identity (e.g. the planner CLI
    sends it as the Remote-User header). The slug is the agent directory name
    under /srv/agents/<slug> -- sourced from the runtime, not the model prompt,
    so it cannot be forgotten or spoofed. Sessions with any other cwd get no
    slug (identity-requiring tools then fail closed, which is the guard).
    """
    if not cwd:
        return
    norm = cwd.rstrip("/")
    if os.path.dirname(norm) == _AGENTS_ROOT:
        env["SN_AGENT_SLUG"] = os.path.basename(norm)


def _safe_env() -> Dict[str, str]:
    """What a spawned CLI inherits. The list and the credential lookup live in
    config, shared with session_manager: this was two copies that drifted, and
    the drift caused the failed cancellation."""
    return spawn_env()


def _resolve_cwd(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """metadata.cwd if valid dir, else CLI_DEFAULT_CWD if exists, else None."""
    if metadata and metadata.get("cwd"):
        cwd = metadata["cwd"]
        if os.path.isdir(cwd):
            return cwd
        logger.warning("metadata.cwd %r not a directory, falling back", cwd)
    if CLI_DEFAULT_CWD and os.path.isdir(CLI_DEFAULT_CWD):
        return CLI_DEFAULT_CWD
    return None


def _provider_env(provider_slug: str) -> Dict[str, str]:
    """Resolved env to inject into the CLI: cli_providers.env_vars with secret:
    refs decrypted from gateway_secrets. {} if the provider declares none."""
    rows = db.query(
        "SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s",
        (provider_slug,), fetch=True, as_dict=True,
    )
    if not rows:
        return {}
    return secrets_store.resolve_env(rows[0].get("env_vars") or {})


def _person_tz_env(conversation: Optional[str]) -> Dict[str, str]:
    """TZ of the person whose chat this session serves, so the runtime's own clock
    (Flint's "[Local time: ...]" on every message) is their clock, not the node's.
    An agent told the owner "01:42 UTC" for a reminder at 20:42 his time. Empty
    when there is no chat or no zone set; never raises."""
    if not (conversation or "").startswith("desk:"):
        return {}
    try:
        rows = db.query("SELECT u.timezone FROM sn_core.desks d JOIN sn_config.users u "
                        "ON u.id = d.owner_user_id WHERE d.id::text = %s",
                        (conversation[len("desk:"):],), fetch=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("time zone of %s unreadable: %s", conversation, e)
        return {}
    tz = rows[0][0] if rows else None
    return {"TZ": tz} if tz else {}


def _message_origin_env(metadata: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Origin inherited by the messaging MCP inside an opaque CLI process.

    Brin can add ``origin`` itself only when it owns the model's tool loop. A
    Claude CLI owns its loop and spawns the stdio MCP as a child, so the only
    common boundary is the process environment. The gateway conversation key
    is ``<agent>:desk:<id>`` while core-api's real internal destination is
    ``desk:<id>``; keep only that destination here.

    Explicit relay calls carry a channel and the MCP deliberately does not use
    this inherited origin for them, preventing a relay from starting another
    relay chain.
    """
    conversation = str((metadata or {}).get("conversation") or "")
    # A chat wake has no conversation of the chat's own, only the chat it works for:
    # what the agent passes on from there stays tied to that chat.
    wake_desk = (metadata or {}).get("wake_desk")
    if "desk:" not in conversation and isinstance(wake_desk, str) and wake_desk:
        conversation = f"desk:{wake_desk}"
    marker = conversation.find("desk:")
    if marker == -1:
        return {}
    destination = conversation[marker:].strip()
    if destination == "desk:":
        return {}
    return {
        "SN_MESSAGE_ORIGIN_CHANNEL": "internal",
        "SN_MESSAGE_ORIGIN_CONVERSATION": destination,
    }


# The tools every agent has for its own work, in Claude Code's names (Flint
# reads them the same way; Claude Code skips LS, which Glob covers there).
# Everything else an agent can do comes from what it was granted, which
# core-api renders into its .mcp.json. Without the list each runtime offered
# all of its own tools (tasks, sub-agents, model switching, its own memory) on
# every call, next to the granted ones. Only agent folders carry .mcp.json, so
# a session without one keeps its runtime's defaults.
AGENT_BASE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "LS", "Bash",
                    "WebSearch", "WebFetch")


def _agent_tools(cwd: str) -> str:
    """The agent's tool list for --tools: the base set and its granted servers."""
    try:
        with open(os.path.join(cwd, ".mcp.json")) as fh:
            servers = sorted(json.load(fh).get("mcpServers") or {})
    except (OSError, ValueError, AttributeError):
        servers = []
    return ",".join(list(AGENT_BASE_TOOLS) + [f"mcp__{s}" for s in servers])


def _agent_withheld(cwd: Optional[str]) -> List[str]:
    """What the owner took off this agent on its card: core-api writes
    withheld-tools.json beside .mcp.json. A server (mcp__<server>) or one of its
    tools (mcp__<server>__<tool>); the CLI then never describes it to the model,
    so every request is smaller by its schema (2026-11-06: hiding one server of
    Dana's took 6.6k tokens off each call). It can only take away."""
    if not cwd:
        return []
    try:
        with open(os.path.join(cwd, "withheld-tools.json")) as fh:
            names = json.load(fh).get("tools") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [n for n in names if isinstance(n, str) and n.startswith("mcp__")]


def _withheld_tools(metadata) -> str:
    """--disallowedTools for one session, from its metadata "withhold_tools".
    A chat wake withholds the inbox: with inbox_read the agent went on to answer
    other chats in the same turn, and inbox_dismiss was a wasted call after each
    reply (2026-10-29). A list that only takes away cannot widen what core-api
    granted, whoever wrote it."""
    names = (metadata or {}).get("withhold_tools") or []
    return ",".join(n for n in names if isinstance(n, str) and n.startswith("mcp__"))


# Flint's provider -> the door it calls and the variable it reads its key from.
_FLINT_DOORS = {
    "openrouter": ("openai", "OPENROUTER_API_KEY"),
    "openai": ("openai", "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
}
_FLINT_KEYS = tuple(k for _, k in _FLINT_DOORS.values())


def _relay_flint(env: Dict[str, str], session_id: Optional[str], os_user: Optional[str],
                 model: str, provider: str) -> None:
    """Flint calls the model through the door, like Claude Code: the provider
    key leaves its environment, a token of the gateway takes its place under
    the name Flint reads, and FLINT_BASE_URL sends its calls here. The door
    swaps the key back in (openai_relay, anthropic_relay). A FLINT_BASE_URL
    the provider row sets is where the door sends them on, so it is replaced
    here, not passed."""
    flint_provider = env.get("FLINT_PROVIDER") or "openrouter"
    if flint_provider not in _FLINT_DOORS:
        raise RuntimeError("Flint provider %s has no door in the gateway" % flint_provider)
    door, key_name = _FLINT_DOORS[flint_provider]
    for key in _FLINT_KEYS:
        env.pop(key, None)
    env[key_name] = model_tokens.issue(session_id, env.get("SN_AGENT_SLUG"), os_user, model,
                                       provider=provider)
    env["FLINT_BASE_URL"] = (openai_relay.relay_base_url() if door == "openai"
                             else anthropic_relay.relay_base_url() + "/v1")


async def _spawn(model_suffix: str,
                 system_prompt: Optional[str],
                 cwd: Optional[str],
                 extra_env: Optional[Dict[str, str]] = None,
                 session_id: Optional[str] = None,
                 resume: bool = False,
                 os_user: Optional[str] = None,
                 runtime: str = "claude-cli",
                 provider: Optional[str] = None,
                 withhold: str = "") -> asyncio.subprocess.Process:
    # An agent waiting for its model (the seed's "none"): the runtime would answer
    # "Run --model", which speaks to a developer.
    if model_suffix == "none":
        raise RuntimeError("This agent has no AI model yet. Choose one in Admin, under Agents.")
    is_flint = runtime == "flint-cli"
    # The agent's identity is the instructions file in its own folder (CLAUDE.md,
    # FLINT.md or AGENTS.md, written by core-api for the runtime), which the
    # runtime reads by itself. Passing the same text by flag as well delivered it
    # twice: 62k characters, the role in full two times (2026-10-29). Only a
    # session with no such file (no agent folder) still gets its prompt here.
    if system_prompt and cwd and any(
            os.path.isfile(os.path.join(cwd, n)) for n in AGENT_INSTRUCTION_FILES):
        system_prompt = None
    cmd = [
        str(FLINT_PATH if is_flint else CLI_PATH),
        "--print",
        "--verbose",
        "--model", model_suffix,
        "--input-format", "stream-json",
        "--output-format", "stream-json",
    ]
    # Tie the CLI's own session to the gateway session id: --session-id persists
    # the transcript (~/.claude/projects/<project>/<id>.jsonl) so a later
    # --resume restart restores the conversation. Both are valid UUIDs.
    if session_id and resume:
        cmd.extend(["--resume", session_id])
    elif session_id:
        cmd.extend(["--session-id", session_id])
    if ((FLINT_SKIP_PERMISSIONS or os_user in FLINT_SKIP_PERMISSIONS_USERS)
            if is_flint else SKIP_PERMISSIONS):
        cmd.append("--dangerously-skip-permissions")
    if cwd and os.path.isfile(os.path.join(cwd, ".mcp.json")):
        cmd.extend(["--tools", _agent_tools(cwd)])
    hidden = [n for n in withhold.split(",") if n] + _agent_withheld(cwd)
    if hidden:
        cmd.extend(["--disallowedTools", ",".join(dict.fromkeys(hidden))])
    if system_prompt:
        # Say how big it is, every spawn. Not a limit -- a gauge. The
        # CLI echoes this prompt back on its FIRST output line, escaped, so its
        # size is the size of the first line we must read. Nobody knew ours were
        # 27-35 KiB against a 64 KiB default until another node's went over and
        # its wakes started dying. WARN at a quarter of the ceiling so the next
        # approach is visible long before it is a failure.
        _pl = len(system_prompt.encode("utf-8", errors="replace"))
        if _pl * 4 >= STREAM_LIMIT:
            logger.warning(
                "claude-cli system prompt is %d bytes, within 4x the %d-byte "
                "line ceiling; the CLI echoes it on its first line",
                _pl, STREAM_LIMIT,
            )
        else:
            logger.info("claude-cli system prompt %d bytes (ceiling %d)",
                        _pl, STREAM_LIMIT)

    env = _safe_env()
    if is_flint:
        # Do not lend Flint the node's Claude credential by accident.
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY"):
            env.pop(key, None)
        env["FLINT_UPDATE_CHECK"] = "0"
    if extra_env:
        env.update(extra_env)
    # Agent identity for tool attribution (planner CLI Remote-User etc.),
    # derived from the agent's own dir /srv/agents/<slug> -- runtime, not prompt.
    _inject_agent_slug(env, cwd)
    if is_flint and MODEL_RELAY:
        _relay_flint(env, session_id, os_user, model_suffix, provider or runtime)

    if os_user:
        # Credential wrapper approach. sudo 1.9.15p5 on this node logs
        # the full environment (log_env on, no !log_env directive), so any token
        # value in the env dict we hand to create_subprocess_exec appears
        # verbatim in the journal on every agent wake.
        #
        # FIX: pop credentials from env entirely before sudo sees it. The agent
        # wrapper (WRAPPER_PATH, installed by gateway.sh) runs as the target
        # agent user via sudo, reads the credential directly from
        # /etc/smart-node/agent-runtime.env (root:smart-node-agents, mode 0640
        # -- agent users are members of that group), and execs the real CLI
        # binary with the credential in its environment. The token never travels
        # through sudo or appears in the journal.
        #
        # -H so HOME becomes the agent's own: without it every agent's
        # transcripts keep pooling in the gateway's home.
        # --preserve-env passes only non-credential vars by NAME. Provider vars
        # from cli_providers are dynamic; the list is derived from env so they
        # are not silently dropped. Putting them on the command line would
        # publish them in `ps`.
        framed_env = None
        if is_flint:
            # sudo logs its environment on this node. Transfer Flint's provider
            # key and the other per-session variables through the already private
            # stdin pipe, before the stream-json protocol starts.
            base_keys = {"PATH", "HOME", "USER", "LANG", "TERM", "SHELL"}
            framed_env = {k: env.pop(k) for k in list(env) if k not in base_keys}
            env["FLINT_PATH"] = str(FLINT_PATH)
        else:
            for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
                env.pop(k, None)
            if MODEL_RELAY:
                # The process gets a token of the gateway instead of the node
                # credential, and the door as its API endpoint, so every
                # model call of the agent passes the one door and is recorded.
                # The token is stored as a digest (sn_core.model_tokens): the
                # door that checks it is another process.
                # Both travel over the private stdin frame, like Flint's key:
                # sudo logs its environment, the pipe is not logged.
                env.pop("ANTHROPIC_BASE_URL", None)
                framed_env = {
                    "ANTHROPIC_AUTH_TOKEN": model_tokens.issue(
                        session_id, env.get("SN_AGENT_SLUG"), os_user, model_suffix,
                        provider=provider or runtime),
                    "ANTHROPIC_BASE_URL": anthropic_relay.relay_base_url(),
                }
        preserve = sorted(k for k in env
                          if k not in ("HOME", "USER", "LOGNAME", "SHELL"))
        # Replace CLI_PATH with WRAPPER_PATH as the sudoed binary. The wrapper
        # receives the same CLI args (cmd[1:]) and execs CLI_PATH with them.
        wrapper_args = (["--sn-runtime-flint", "--sn-env-stdin"] if is_flint
                        else ["--sn-env-stdin"] if framed_env is not None else [])
        cmd = [SUDO_PATH, "-n", "-H", "-u", os_user,
               "--preserve-env=" + ",".join(preserve), "--",
               str(WRAPPER_PATH)] + wrapper_args + cmd[1:]
    else:
        framed_env = None

    # sudo logs argv and env and closes inherited file descriptors. Send the
    # prompt over stdin for its wrapper to expose as a file after dropping
    # privileges. A direct CLI can inherit the anonymous file from us.
    if system_prompt and os_user:
        cmd.append(PROMPT_STDIN_FLAG)
    with prompt_file(system_prompt if not os_user else None) as (options, fds):
        process = await asyncio.create_subprocess_exec(
            *cmd, *options,
            pass_fds=fds,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            # The per-line ceiling. Without it this reader gets asyncio's
            # 64 KiB default, and the CLI's FIRST line -- which echoes the assembled
            # system prompt, escaped -- already runs 27-35 KiB on our nodes and was
            # over the default on another. Every wake of such an agent died in ~80ms
            # and the Runner marked it broken, while the node reported healthy. The
            # ceiling itself is defined once in config, beside SPAWN_ENV_KEYS, for
            # the reason recorded there: it lived in session_manager only, and this
            # path was left behind.
            limit=STREAM_LIMIT,
            # Its OWN process group, so we can kill the whole tree rather than just
            # the pid we happen to hold. `claude` spawns children of its own,
            # and terminating only the parent leaves them orphaned and spinning --
            # which is the shape found on a live node: 3-4% CPU each, for weeks, with the
            # gateway unaware they existed. Cgroup membership is inherited and is not
            # changed by this, so systemd still sweeps the group on stop.
            start_new_session=True,
        )
    if framed_env is not None:
        try:
            await write_prompt_frame(process, json.dumps(framed_env))
        except BaseException:
            await _signal_group(process.pid, "KILL", os_user)
            await process.wait()
            raise
    if system_prompt and os_user:
        try:
            await write_prompt_frame(process, system_prompt)
        except BaseException:
            # A cancelled spawn has not entered _PROCESSES yet. Reap it here
            # so the wrapper cannot outlive its caller waiting for a frame.
            await _signal_group(process.pid, "KILL", os_user)
            await process.wait()
            raise
    return process


async def _read_stderr_bg(session_id: str, proc: asyncio.subprocess.Process) -> None:
    """Background task: keep last 50 stderr lines for crash diagnostics."""
    tail: list = []
    _STDERR_TAILS[session_id] = tail
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                tail.append(text)
                if len(tail) > 50:
                    del tail[:-50]
    except Exception:
        pass
    rc = proc.returncode
    # The process is gone, however it went; its relay token goes with it.
    model_tokens.revoke_session(session_id)
    if rc and rc != 0 and tail:
        logger.error("claude-cli session=%s crashed exit=%s\n%s",
                     session_id, rc, "\n".join(tail[-10:]))


def _resolve_agent_prompt(agent_slug: str) -> Optional[str]:
    """The agent's prompt as it stands NOW, from the same resolver a session is
    created with (session_service). None on any failure, so the caller keeps the
    copy it already has."""
    try:
        import httpx
        r = httpx.get(
            os.environ.get("CORE_API_URL", "") + f"/api/agents/{agent_slug}/system-prompt",
            headers=service_headers(),
            timeout=5.0,
        )
        r.raise_for_status()
        return r.text or None
    except Exception as e:
        logger.warning("prompt refresh failed for agent=%s (%s); keeping the stored one",
                       agent_slug, e)
        return None


def _load_session_runtime(session_id: str) -> Dict[str, Any]:
    """Pull model / system_prompt / metadata from DB for autorespawn.

    The stored system_prompt is the one captured when the session was OPENED, and
    for an agent-backed session that is the wrong thing to respawn on. A prompt is
    recomposed whenever the agent's role, policies, tools or documents change, and
    POST /api/agents/<slug>/regenerate restarts every live session precisely so
    they pick the new one up. They did not: it answered `sessions_recycled: 81`
    while every process came back on the old text, because this function handed
    back the stored copy and nothing ever re-asked. A count that reads as
    confirmation while nothing changed is worse than no count at all.

    So re-resolve here and write the fresh text back, so the row stops lying too.
    Fail-soft: a core-api hiccup must never stop a respawn, so on any failure the
    stored prompt is used and the reason is logged."""
    rows = db.query(
        """SELECT s.model, s.system_prompt, s.metadata, a.slug AS agent_slug
             FROM sn_core.ai_sessions s
             LEFT JOIN sn_config.agents a ON a.id = s.agent_id
            WHERE s.id = %s""",
        (session_id,),
        fetch=True, as_dict=True,
    )
    if not rows:
        raise ValueError(f"session {session_id} not found in DB")
    runtime = dict(rows[0])
    slug = runtime.pop("agent_slug", None)
    if slug:
        fresh = _resolve_agent_prompt(slug)
        stored = runtime.get("system_prompt") or ""
        if fresh and fresh != stored:
            db.query("UPDATE sn_core.ai_sessions SET system_prompt = %s WHERE id = %s",
                     (fresh, session_id))
            logger.info("claude-cli session=%s agent=%s prompt refreshed (%d -> %d chars)",
                        session_id, slug, len(stored), len(fresh))
            runtime["system_prompt"] = fresh
    return runtime


class ClaudeCliBackend(SessionBackend):
    is_opaque = True
    provider_name = "claude-cli"
    runtime_name = "claude-cli"

    def __init__(self, provider_name: Optional[str] = None):
        # A cli_providers row that runs the Claude Code binary under its own
        # slug (backends.claude_cli_upstreams): the same runtime, the row's
        # env_vars for the process, the row's upstream for its model calls
        # (anthropic_relay routes by the provider the token names).
        if provider_name:
            self.provider_name = provider_name

    # ----------------------------------------------------------------- create

    async def create(self, session_id, model, system_prompt=None, metadata=None, resume=False):
        if self.runtime_name == "flint-cli" and not FLINT_ENABLED:
            raise RuntimeError("Flint runtime is disabled (FLINT_ENABLED=false)")
        if self.runtime_name == "flint-cli" and not flint_stdio_safe():
            raise RuntimeError("Flint binary lacks the ephemeral-key stdio safety check")
        model_suffix = _parse_model(model)
        cwd = _resolve_cwd(metadata)
        os_user = _runas_user(metadata)

        async with _get_lock():
            existing = _PROCESSES.get(session_id)
            if existing and existing.returncode is None:
                # never kill a process that is mid-turn. A turn holds this
                # session's _send_lock while it streams stdout; terminating the
                # process under it gives the reader EOF -> "EOF before result" ->
                # 502 -- exactly the death-spiral a concurrent /restart used to
                # trigger. Refuse instead. The autorespawn path never reaches here
                # (it only respawns a DEAD process), so this only blocks an
                # explicit restart/resume racing a live turn; the caller can retry
                # once the turn finishes.
                lock = _SEND_LOCKS.get(session_id)
                if lock is not None and lock.locked():
                    # distinguish a live turn from a stale lock.
                    # A live turn (started within its registered ceiling) is protected
                    # by the death-spiral guard. A stale lock (started
                    # beyond its registered ceiling) means the deadline has passed
                    # and the process is wedged -- break it so the person can
                    # recover without operator shell access.
                    _ts = _TURN_STARTED.get(session_id)
                    if _ts is not None:
                        _age = time.monotonic() - _ts
                        _ceiling = _TURN_LIMITS.get(session_id, float("inf"))
                        if _age >= _ceiling:
                            logger.warning(
                                "claude-cli session=%s: breaking stale lock "
                                "held for %.0fs (>= %.0fs ceiling); proceeding "
                                "with respawn",
                                session_id, _age, _ceiling,
                            )
                            # Fall through to the kill-and-respawn path below.
                        else:
                            raise RuntimeError(
                                f"session {session_id} has a turn in flight; "
                                f"refusing to restart over it"
                            )
                    else:
                        raise RuntimeError(
                            f"session {session_id} has a turn in flight; "
                            f"refusing to restart over it"
                        )
                logger.warning(
                    "claude-cli session=%s already has live process pid=%s; killing before respawn",
                    session_id, existing.pid,
                )
                # Through _kill_tree rather than proc.terminate(): once the CLI
                # runs as the agent, terminate() raises PermissionError and the
                # old process would survive its own replacement.
                await _kill_tree(session_id, existing, "replaced by respawn")

            # [incident 2026-06-16] Temporary hard cap on concurrent CLI
            # processes. Each new session spawns a `claude` subprocess; an
            # unbounded wake burst spawned ~19 -> CPU 100%. Refuse (not block) a
            # NEW-session spawn past the cap; the caller (watcher) retries later.
            # A live process for THIS session was already killed above, so a
            # same-session respawn is not falsely blocked.
            #
            # two pools instead of one flat cap.
            #   Person pool: SN_CLI_PERSON_SLOTS slots that background work
            #                cannot consume. Person-facing sessions block only
            #                on the total cap (SN_CLI_MAX_PROCS).
            #   Background pool: SN_CLI_MAX_PROCS - SN_CLI_PERSON_SLOTS slots.
            #                Background sessions are refused when this ceiling
            #                is reached, even if person slots are free.
            _MAX_PROCS = int(os.environ.get("SN_CLI_MAX_PROCS", "6"))
            _PERSON_SLOTS = int(os.environ.get("SN_CLI_PERSON_SLOTS", "1"))
            _BG_CEILING = _MAX_PROCS - _PERSON_SLOTS
            _sess_type = (metadata or {}).get("session_type", "background")

            def _proc_counts():
                live = sum(1 for _p in _PROCESSES.values() if _p.returncode is None)
                bg = sum(
                    1 for _s, _p in _PROCESSES.items()
                    if _p.returncode is None and _SESSION_TYPES.get(_s) != "person"
                )
                return live, bg

            _live, _live_bg = _proc_counts()
            _over_cap = (
                _live >= _MAX_PROCS if _sess_type == "person"
                else _live_bg >= _BG_CEILING
            )
            if _over_cap:
                # Before refusing: reclaim slots held by IDLE sessions. A live
                # claude proc is kept alive for resume after its turn finishes; an
                # idle session (no turn in flight -> its _send_lock not held) then
                # hogs a cap slot doing nothing (the session-leak that jammed the
                # cap on 2026-06-16). Evict the oldest idle sessions (dict is in
                # insertion order) until under cap; a killed idle session respawns
                # with --resume on its next message, so no context is lost. Refuse
                # only if every live slot is genuinely mid-turn.
                # Background spawns only evict other idle background sessions
                # (never person-facing ones) to avoid displacing a person's turn.
                for _sid, _p in list(_PROCESSES.items()):
                    _live, _live_bg = _proc_counts()
                    if _sess_type == "person" and _live < _MAX_PROCS:
                        break
                    if _sess_type != "person" and _live_bg < _BG_CEILING:
                        break
                    if _sid == session_id or _p.returncode is not None:
                        continue
                    _l = _SEND_LOCKS.get(_sid)
                    if _l is not None and _l.locked():
                        continue  # mid-turn: never evict
                    # Background sessions must not evict person-facing sessions.
                    if _sess_type != "person" and _SESSION_TYPES.get(_sid) == "person":
                        continue
                    logger.info(
                        "claude-cli evicting idle session=%s pid=%s to free a cap slot",
                        _sid, _p.pid,
                    )
                    await _kill_tree(_sid, _p, "evicted to free a cap slot")
                    _forget_process(_sid)
                _live, _live_bg = _proc_counts()
                _over_cap = (
                    _live >= _MAX_PROCS if _sess_type == "person"
                    else _live_bg >= _BG_CEILING
                )
                if _over_cap:
                    raise SpawnCapReached(
                        "agent spawn cap reached (%d/%d live, %d/%d background); "
                        "refusing new %s session %s"
                        % (_live, _MAX_PROCS, _live_bg, _BG_CEILING, _sess_type, session_id)
                    )

            extra_env = _provider_env(self.provider_name)
            if self.runtime_name == "flint-cli":
                key_name = {
                    "openrouter": "OPENROUTER_API_KEY",
                    "openai": "OPENAI_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                }.get(extra_env.get("FLINT_PROVIDER"))
                if not key_name or not extra_env.get(key_name):
                    raise RuntimeError(
                        "Flint requires FLINT_PROVIDER and its provider key "
                        "in sn_config.cli_providers"
                    )
            # The CLI's MCP children inherit this. Without it a desk question
            # sent to a colleague loses the only route by which the answer can
            # return to the browser.
            extra_env.update(_message_origin_env(metadata))
            extra_env.update(_person_tz_env(extra_env.get("SN_MESSAGE_ORIGIN_CONVERSATION")))
            proc = await _spawn(model_suffix, system_prompt, cwd, extra_env,
                                session_id=session_id, resume=resume,
                                os_user=os_user, runtime=self.runtime_name,
                                provider=self.provider_name,
                                withhold=_withheld_tools(metadata))
            _PROCESSES[session_id] = proc
            _SESSION_RUNTIME[session_id] = self.runtime_name
            _RUNAS[session_id] = os_user
            _SESSION_TYPES[session_id] = _sess_type
            _ANSWER_IDS[session_id] = {i for i in ((metadata or {}).get("answer_ids") or [])
                                       if isinstance(i, int)}
            _wd = (metadata or {}).get("wake_desk")
            if isinstance(_wd, str) and _wd:
                _WAKE_DESK[session_id] = _wd
            _LAST_USED[session_id] = time.monotonic()
            _SPAWNED_AT[session_id] = time.time()
            # Remember which conversation this session serves. Written
            # at spawn so the desk-activity endpoint can resolve session_id from
            # the desk UUID without a DB round-trip.
            _conv = (metadata or {}).get("conversation", "")
            if _conv:
                _SESSION_CONVERSATION[session_id] = _conv
            _slug = (metadata or {}).get("agent_slug")
            if _slug:
                _SESSION_AGENT[session_id] = str(_slug)
            asyncio.create_task(_read_stderr_bg(session_id, proc))
            # The sweeper starts with the first spawn, so a gateway that never
            # runs a CLI session never carries the task.
            start_idle_sweeper()

        logger.info(
            "claude-cli.create session=%s model=%s pid=%s cwd=%s user=%s",
            session_id, model_suffix, proc.pid, cwd, os_user or _current_user(),
        )
        await asyncio.sleep(SPAWN_GRACE_SEC)

    # ------------------------------------------------------------------ send

    async def send(self, session_id, history, message):
        # Serialize turns per session: concurrent send() on one CLI pipe pair
        # would have two coroutines awaiting proc.stdout.readline() and trip
        # asyncio's "readuntil() called while another coroutine is already
        # waiting" error (the 502 race). The lock makes a second concurrent
        # turn wait for the first to finish instead of corrupting the stream.
        async with _send_lock(session_id):
            # Stamped on BOTH sides of the turn. Before, so a turn that
            # begins after a long silence is not reclaimed out from under itself
            # in the moment between the lock and the first write; after, so the
            # idle window is measured from when the work ENDED rather than when
            # it started -- otherwise a four-minute turn is already a quarter
            # idle by the time it finishes.
            _now = time.monotonic()
            _LAST_USED[session_id] = _now
            # Record turn start for stale-lock detection. The restart
            # guard compares this age to the registered ceiling so it can distinguish
            # a live turn from one that has exceeded the deadline.
            ceiling = turn_limit_seconds(session_id)
            _TURN_STARTED[session_id] = _now
            _TURN_LIMITS[session_id] = ceiling
            # A new turn starts an empty log: what the chat shows must be THIS
            # answer's steps, never the tail of the previous one.
            _TURN_STEPS.pop(session_id, None)
            _TURN_STEP_SEQ.pop(session_id, None)
            _TURN_TEXT[session_id] = ""
            _TURN_STEERS[session_id] = {}
            _live_changed(session_id)
            try:
                return await self._send_locked(session_id, history, message)
            finally:
                _remember_last_turn(session_id)
                _TURN_STARTED.pop(session_id, None)
                _TURN_LIMITS.pop(session_id, None)
                _TURN_STATUS.pop(session_id, None)
                _TURN_WRITTEN.discard(session_id)
                _INTERRUPTS.pop(session_id, None)
                _TURN_TEXT.pop(session_id, None)
                _LAST_USED[session_id] = time.monotonic()
                _live_changed(session_id)

    async def steer(self, session_id: str, text: str) -> dict:
        """Write a direction to Flint while send() remains the sole stdout reader."""
        if self.runtime_name != "flint-cli":
            return {"status": "unsupported"}
        if not text or not text.strip() or len(text) > 8000:
            return {"status": "invalid_text"}
        if session_id not in _TURN_STARTED or session_id in _INTERRUPTS:
            return {"status": "too_late"}
        # A turn can be registered just before its user envelope is written.
        for _ in range(50):
            if session_id in _TURN_WRITTEN:
                break
            if session_id not in _TURN_STARTED:
                return {"status": "too_late"}
            await asyncio.sleep(0.1)
        if session_id not in _TURN_WRITTEN:
            return {"status": "too_late"}
        proc = _PROCESSES.get(session_id)
        if proc is None or proc.returncode is not None:
            return {"status": "too_late"}
        request_id = f"sn-steer-{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        _STEER_ACKS[request_id] = future
        _TURN_STEERS.setdefault(session_id, {})[request_id] = {"request_id": request_id, "status": "submitted"}
        request = {"type": "control_request", "request_id": request_id,
                   "request": {"subtype": "steer", "text": text}}
        try:
            proc.stdin.write((json.dumps(request) + "\n").encode())
            await proc.stdin.drain()
            response = await asyncio.wait_for(future, timeout=5)
        except asyncio.TimeoutError:
            return {"request_id": request_id, "status": "pending"}
        except (BrokenPipeError, ConnectionResetError):
            _TURN_STEERS.get(session_id, {}).pop(request_id, None)
            return {"status": "too_late"}
        finally:
            _STEER_ACKS.pop(request_id, None)
        status = "accepted" if response.get("subtype") == "success" else response.get("error", "rejected")
        if request_id in _TURN_STEERS.get(session_id, {}) and _TURN_STEERS[session_id][request_id]["status"] == "submitted":
            _TURN_STEERS[session_id][request_id]["status"] = status
        return {"request_id": request_id, "status": status}

    # ------------------------------------------------------------- interrupt

    async def interrupt(self, session_id, requested_by=None):
        """Stop the turn in flight and keep the session.

        Sends the CLI its own interrupt over the stream-json control channel.
        Measured on claude 2.1.143: the running tool is killed, the turn ends at
        once with a `result` of subtype error_during_execution, the process stays
        up, and the next message in it still has the whole conversation. That is
        why this does not go through close(): killing the process made the send
        loop read a crash, respawn with --resume and send the stopped message
        again, and a closed session starts the next message from nothing.

        A stop that arrives before the message has been written to the CLI is
        recorded and honoured by _send_locked, which then never sends it. The
        check there and the mark here have no await between them, so the two
        cannot interleave.
        """
        if session_id not in _TURN_STARTED:
            return {"interrupted": False, "reason": "no turn in flight"}
        if session_id in _INTERRUPTS:
            return {"interrupted": True, "already": True,
                    "stopped_by": _INTERRUPTS[session_id].get("by")}
        _INTERRUPTS[session_id] = {"by": requested_by, "at": time.time()}
        if session_id not in _TURN_WRITTEN:
            return {"interrupted": True, "before_start": True}
        proc = _PROCESSES.get(session_id)
        if proc is None or proc.returncode is not None:
            # The process is already gone; the send loop sees the EOF and, with
            # the mark set, ends the turn as stopped instead of retrying it.
            return {"interrupted": True, "process": "gone"}
        request = {
            "type": "control_request",
            "request_id": f"sn-stop-{uuid.uuid4().hex[:12]}",
            "request": {"subtype": "interrupt"},
        }
        try:
            proc.stdin.write((json.dumps(request) + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            logger.warning("claude-cli session=%s: interrupt write failed: %s", session_id, e)
            return {"interrupted": True, "process": "gone"}
        logger.info("claude-cli session=%s: interrupt sent (by %s)", session_id, requested_by or "unknown")
        return {"interrupted": True}

    async def _send_locked(self, session_id, history, message):
        proc = _PROCESSES.get(session_id)

        # Autorespawn if process gone (gateway restart) or dead
        if proc is None or proc.returncode is not None:
            tail = _STDERR_TAILS.get(session_id, [])
            stderr_hint = " | ".join(tail[-3:]) if tail else "no stderr"
            logger.warning(
                "claude-cli session=%s no live process (rc=%s, stderr=%s); respawning",
                session_id,
                proc.returncode if proc else "missing",
                stderr_hint,
            )
            runtime = _load_session_runtime(session_id)
            # resume only if a transcript already exists on disk. The first
            # send of a session has none, so it spawns fresh with --session-id; a
            # respawn after the process died (gateway restart, crash, or a prior
            # turn that exited) finds the transcript and uses --resume. The old
            # code always used resume=False, so the second send after a death
            # re-ran `--session-id <id>` over an existing transcript and the CLI
            # aborted with "Session ID is already in use" -> EOF -> 502.
            #
            # GUESSING THIS WRONG IS SILENT, BOTH WAYS, so do not rely on the
            # guess. Resume a session the CLI does not know and it exits "No
            # conversation found"; do not resume one it does know and it exits
            # "Session ID is already in use". Both end as EOF, and the caller
            # gets an empty answer or a 502 while the person sees nothing at
            # all. Both were seen on this node on 2026-08-30.
            #
            # PRIMARY: the node's own session records. The filesystem
            # check cannot be trusted after the privilege drop: the transcript
            # lives in the agent's home at mode 0700, the gateway runs as itself
            # and cannot read it, glob() returns [] (same as "no transcript"),
            # and every respawn guesses resume=False -- which makes the CLI exit
            # "Session ID is already in use" on every turn after the first,
            # producing the spawn storm seen on the live node at 52/min.
            #
            # _session_has_turns() uses sn_core.ai_session_messages and needs no
            # access to any home directory. It is wrong only in one direction: a
            # session whose first turn died before a message was committed is
            # treated as new -- which it is. The _RESUME_MISMATCH two-way retry
            # below corrects any remaining wrong guess in either direction.
            #
            # SECONDARY: if the DB says False, still try the filesystem. This
            # catches the narrow window where a transcript was written but the
            # DB message was not yet committed (e.g. the first turn was in
            # flight). When the directory is unreadable it returns False silently
            # and the DB answer stands; the retry loop corrects if needed.
            resume = _session_has_turns(session_id)
            if not resume:
                resume = _transcript_exists(
                    session_id, _runas_user(runtime.get("metadata")))
            await self.create(
                session_id,
                runtime["model"],
                runtime.get("system_prompt"),
                runtime.get("metadata") or {},
                resume=resume,
            )
            proc = _PROCESSES[session_id]
            if proc.returncode is not None:
                tail = " | ".join(_STDERR_TAILS.get(session_id, [])[-3:])
                if _RESUME_MISMATCH.search(tail or ""):
                    logger.warning(
                        "claude-cli session=%s refused resume=%s (%s); "
                        "respawning the other way",
                        session_id, resume, tail[:120])
                    await self.create(
                        session_id,
                        runtime["model"],
                        runtime.get("system_prompt"),
                        runtime.get("metadata") or {},
                        resume=not resume,
                    )
                    proc = _PROCESSES[session_id]

        # If a prior turn was lost before reaching the transcript, re-present
        # the person's undelivered message so the agent does not answer with a
        # silently incomplete view of the history. The stored text is
        # the raw message content; we keep `message` (the current turn)
        # unchanged so the failure path can store it cleanly if this turn also
        # dies.
        _prior_lost = _UNDELIVERED.pop(session_id, None)
        if _prior_lost:
            logger.warning(
                "claude-cli session=%s: relaying message lost in prior "
                "failed turn so the agent's transcript is not missing a turn",
                session_id,
            )
            _envelope_content = (
                "[Context: your previous turn was interrupted before it "
                "reached you. The undelivered message was:\n"
                + _prior_lost
                + "\nPlease address it alongside the message below.]\n\n"
                + message
            )
        else:
            _envelope_content = message

        # Write user message in Claude CLI stream-json format
        envelope = {
            "type": "user",
            "message": {"role": "user", "content": _envelope_content},
        }

        # Send the turn and read the result, retrying once if the process exits
        # before producing a result. _RESUME_MISMATCH still governs HOW to
        # respawn (--resume vs. --session-id); it no longer governs WHETHER to
        # retry -- an exit without a result is the condition worth retrying,
        # whatever the CLI said on the way out.
        content = ""
        tool_calls: list = []
        usage_raw: Dict[str, Any] = {}
        request_usages: list[Dict[str, Any]] = []
        cost_usd = 0.0
        _retry_runtime: Optional[Dict[str, Any]] = None  # loaded lazily on retry

        for _attempt in range(2):
            proc = _PROCESSES.get(session_id)
            if proc is None or proc.returncode is not None:
                tail = _STDERR_TAILS.get(session_id, [])
                raise RuntimeError(
                    f"claude-cli session={session_id} process not live at "
                    f"attempt {_attempt + 1}; "
                    f"stderr: {' | '.join(tail[-5:]) or 'none'}"
                )

            if session_id in _INTERRUPTS:
                # Stopped before the CLI was given the message: nothing ran,
                # nothing to keep, and the message is not sent.
                return BackendResponse(
                    content="", usage={}, stopped=True,
                    stopped_by=_INTERRUPTS[session_id].get("by"),
                )
            try:
                proc.stdin.write((json.dumps(envelope) + "\n").encode())
                _TURN_WRITTEN.add(session_id)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                raise RuntimeError(f"claude-cli stdin closed: {e}")

            # Reset per-attempt accumulators so a retry starts clean.
            content = ""
            final_answer = ""
            tool_calls = []
            usage_raw = {}
            request_usages = []
            request_row_index = {}
            cost_usd = 0.0
            streamed_reply = ""
            reply_count = 0
            _got_eof = False
            _eof_error = ""
            _result_subtype = ""
            _stopped = False
            _answer_left = set(_ANSWER_IDS.get(session_id, ()))
            _reply_calls: Dict[str, int] = {}    # tool_use id -> message it answers
            _done_early = False
            stopped_by = None
            _failed = None

            # A hard wall clock for the whole turn. Each read waits only for
            # the time remaining, including when a child emits no output.
            ceiling = _TURN_LIMITS[session_id]
            turn_deadline = time.monotonic() + ceiling

            while True:
                remaining = turn_deadline - time.monotonic()
                if remaining <= 0:
                    await _kill_tree(session_id, proc,
                                     "turn exceeded %.0fs" % ceiling)
                    _PROCESSES.pop(session_id, None)
                    _LAST_USED.pop(session_id, None)
                    _RUNAS.pop(session_id, None)
                    _SESSION_TYPES.pop(session_id, None)
                    # _TURN_STARTED is cleared in the send() finally block; do
                    # not pop it here because the finally runs after this raise.
                    raise TurnLimitExceeded(session_id, ceiling)
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    await _kill_tree(session_id, proc, "registered turn ceiling reached")
                    _PROCESSES.pop(session_id, None)
                    _LAST_USED.pop(session_id, None)
                    _RUNAS.pop(session_id, None)
                    _SESSION_TYPES.pop(session_id, None)
                    raise TurnLimitExceeded(session_id, ceiling)
                except ValueError as exc:
                    # An output line past STREAM_LIMIT. asyncio catches
                    # LimitOverrunError inside readline() and re-raises a bare
                    # ValueError, so this is the name that arrives, not the one
                    # the documentation suggests. Unhandled it reached the
                    # operator as "Separator is found, but chunk is longer than
                    # limit", which names neither the agent, nor the size, nor
                    # the ceiling -- and on the first line of a wake it read as
                    # the session simply crashing. Say what happened.
                    raise RuntimeError(
                        f"claude-cli session={session_id}: one output line exceeded "
                        f"{STREAM_LIMIT} bytes and could not be read ({exc}). The "
                        f"turn produced more on a single line than the gateway will "
                        f"buffer."
                    ) from exc
                if not line:
                    # Process exited before producing a result. Wait briefly so
                    # returncode is populated, then capture the full stderr for
                    # the session error record -- "stderr: Error:" (one word) is
                    # not something anyone can diagnose from.
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    rc = proc.returncode
                    tail = _STDERR_TAILS.get(session_id, [])
                    stderr_full = "\n".join(tail) if tail else "no stderr"
                    _eof_error = (
                        f"claude-cli session={session_id} EOF before result "
                        f"(attempt {_attempt + 1}); exit={rc}; "
                        f"stderr:\n{stderr_full}"
                    )
                    _got_eof = True
                    break

                try:
                    data = json.loads(line.decode(errors="replace").strip())
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")
                if msg_type == "control_response":
                    response = data.get("response") or {}
                    request_id = response.get("request_id")
                    item = _TURN_STEERS.get(session_id, {}).get(request_id)
                    if item and item["status"] == "submitted":
                        item["status"] = "accepted" if response.get("subtype") == "success" else response.get("error", "rejected")
                    ack = _STEER_ACKS.get(request_id)
                    if ack and not ack.done():
                        ack.set_result(response)
                    continue
                if msg_type == "steer_status":
                    item = _TURN_STEERS.get(session_id, {}).get(data.get("request_id"))
                    if item:
                        item["status"] = data.get("status", "rejected")
                    continue
                if msg_type == "text_delta":
                    delta = data.get("delta")
                    if self.runtime_name == "flint-cli" and isinstance(delta, str) and delta:
                        if not streamed_reply and reply_count and _TURN_TEXT.get(session_id):
                            _TURN_TEXT[session_id] += "\n\n"
                        _TURN_TEXT[session_id] = _TURN_TEXT.get(session_id, "") + delta
                        streamed_reply += delta
                        _live_changed(session_id)
                    continue
                if msg_type == "assistant":
                    msg = data.get("message", {})
                    # Claude CLI includes usage on each assistant message.  A
                    # final `result.usage` is a cumulative turn total, so keep
                    # the per-request records separately for the panel. The
                    # CLI can repeat an id across text/tool/content events for
                    # one request. Replace its row with the latest usage while
                    # preserving request order, so the last row still measures
                    # the final provider request, not a repeated earlier block.
                    msg_usage = msg.get("usage")
                    if isinstance(msg_usage, dict):
                        request_id = msg.get("id")
                        request_row = {
                            "request_id": request_id,
                            "input_tokens": int(msg_usage.get("input_tokens", 0) or 0),
                            "output_tokens": int(msg_usage.get("output_tokens", 0) or 0),
                            "cache_write_tokens": int(msg_usage.get("cache_creation_input_tokens", 0) or 0),
                            "cache_read_tokens": int(msg_usage.get("cache_read_input_tokens", 0) or 0),
                        }
                        if request_id is not None and request_id in request_row_index:
                            request_usages[request_row_index[request_id]] = request_row
                        else:
                            if request_id is not None:
                                request_row_index[request_id] = len(request_usages)
                            request_usages.append(request_row)
                    for block in msg.get("content", []) or []:
                        btype = block.get("type")
                        if btype == "text":
                            # Separate the pieces. The CLI emits one assistant
                            # message per turn, so a turn that says something,
                            # calls a tool, then says more arrives as SEVERAL
                            # blocks -- and `content +=` glued them with nothing
                            # between. Real output from reference-node: "The juniper
                            # ceiling is noted now.The juniper ceiling is saved:
                            # $6 a pound" and "I'll create that task for you
                            # now.I ran into a permission issue". Every agent
                            # reply on every node carries this seam wherever a
                            # tool was used mid-answer, which is most answers
                            # worth reading.
                            _t = _strip_reminder_ack(block.get("text", ""))
                            if _t:
                                content = (content + "\n\n" + _t) if content else _t
                                if self.runtime_name == "flint-cli":
                                    if not streamed_reply:
                                        if reply_count and _TURN_TEXT.get(session_id):
                                            _TURN_TEXT[session_id] += "\n\n"
                                        _TURN_TEXT[session_id] = _TURN_TEXT.get(session_id, "") + _t
                                    elif _t.startswith(streamed_reply):
                                        _TURN_TEXT[session_id] += _t[len(streamed_reply):]
                                else:
                                    _TURN_TEXT[session_id] = _TURN_TEXT.get(session_id, "") + _t
                                _live_changed(session_id)
                        elif btype == "tool_use":
                            tool_calls.append({
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "input": block.get("input"),
                            })
                            _tname = block.get("name", "")
                            _to = (block.get("input") or {}).get("to_message_id")
                            if _answer_left and _tname.endswith("inbox_reply") and _to is not None:
                                try:
                                    _reply_calls[block.get("id")] = int(_to)
                                except (TypeError, ValueError):
                                    pass
                            # Track live tool call for the desk UI.
                            _tdetail = _tool_detail(block.get("input"))
                            _TURN_STATUS[session_id] = {
                                "name": _tname,
                                "detail": _tdetail,
                                "ts": time.monotonic(),
                            }
                            _push_step(session_id, "tool",
                                       _tname + (": " + _tdetail if _tdetail else ""),
                                       desc=_tool_desc(block.get("input")))
                    streamed_reply = ""
                    reply_count += 1
                elif msg_type == "user" and _reply_calls and not _done_early:
                    for block in (data.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            _answer_left.discard(_answered_id(block, _reply_calls))
                    if not _answer_left:
                        _done_early = True
                        logger.info("claude-cli session=%s: every message of the chat wake "
                                    "answered; ending the turn", session_id)
                        try:
                            proc.stdin.write((json.dumps({
                                "type": "control_request",
                                "request_id": f"sn-done-{uuid.uuid4().hex[:12]}",
                                "request": {"subtype": "interrupt"}}) + "\n").encode())
                            await proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            pass    # the process ended on its own; the loop reads that
                elif msg_type == "result":
                    usage_raw = data.get("usage", {}) or {}
                    cost_usd = float(data.get("cost_usd") or data.get("total_cost_usd") or 0)
                    _result_subtype = data.get("subtype") or ""
                    if _done_early:
                        # Ended on purpose once the chat wake's messages were
                        # answered: an ordinary end, whatever subtype it reports.
                        final_answer = content
                        break
                    if (self.runtime_name == "flint-cli"
                            and _result_subtype != "success"
                            and session_id not in _INTERRUPTS):
                        raise RuntimeError(
                            f"flint turn failed ({_result_subtype}, "
                            f"{data.get('stop_reason') or 'unknown'}): "
                            f"{str(data.get('result') or '')[:500]}"
                        )
                    if data.get("is_error") and session_id not in _INTERRUPTS:
                        # Claude Code ends a turn it could not do with an error
                        # result whose text reads like an answer ("API Error:
                        # 529 ...", "Prompt is too long"). It is not one.
                        _failed = str(data.get("result") or _result_subtype or "error")[:500]
                    elif _result_subtype == "success":
                        final_answer = _strip_reminder_ack(data.get("result", "") or "")
                    if not content:
                        content = _strip_reminder_ack(data.get("result", "") or "")
                    break
                elif msg_type == "error":
                    if session_id in _INTERRUPTS or _done_early:
                        # The CLI reporting the interrupt it was sent is the stop
                        # arriving, not a failure of the turn.
                        _result_subtype = "error"
                        break
                    raise RuntimeError(data.get("message", "claude-cli error"))
                # other types (system, tool_result, control_response, ...) ignored

            # A turn someone stopped ends here, whatever shape the end took: the
            # CLI's own result (error_during_execution, measured on 2.1.143), an
            # error line, or the process going away. It is NOT retried. The
            # retry below exists for a CLI that died on its own; resending a
            # message the person just stopped would run the work they stopped a
            # second time. A result that still says success means the
            # interrupt landed after the turn had already finished: that is an
            # ordinary answer and is reported as one.
            if session_id in _INTERRUPTS and (_got_eof or _result_subtype != "success"):
                _stopped = True
                stopped_by = _INTERRUPTS[session_id].get("by")
                # Stopped mid-sentence, Flint has streamed text that no finished
                # block carries yet. The person watched it being written; it stays
                # in the chat as the stopped answer instead of vanishing.
                if not content and self.runtime_name == "flint-cli":
                    content = (_TURN_TEXT.get(session_id) or "").strip()
                logger.info(
                    "claude-cli session=%s: turn stopped by %s (%s)",
                    session_id, stopped_by or "unknown",
                    "process exited" if _got_eof else f"result {_result_subtype}",
                )
                break

            if not _got_eof:
                break  # got a result; leave the retry loop

            # The process exited without producing a result.
            if _attempt == 1:
                # Both retries exhausted. The person's message is in
                # ai_session_messages but NOT in the transcript -- the two
                # sides of the conversation now hold different histories and
                # the agent will answer from an incomplete view on every
                # subsequent turn. Store the raw message so the next
                # send to this session can re-present it as a context note,
                # and log the divergence with the DB message count so it is
                # visible in logs rather than only discovered when the agent
                # answers the wrong question.
                _UNDELIVERED[session_id] = message
                try:
                    _db_rows = db.query(
                        "SELECT COUNT(*) AS n FROM sn_core.ai_session_messages"
                        " WHERE session_id = %s",
                        (session_id,), fetch=True,
                    )
                    _db_count = _db_rows[0]["n"] if _db_rows else "unknown"
                except Exception:
                    _db_count = "unknown"
                logger.error(
                    "claude-cli session=%s: both retry attempts exhausted; "
                    "user message NOT written to transcript. DB records %s "
                    "message(s) for this session; transcript is missing this "
                    "turn (transcript/DB diverged). Message queued for relay "
                    "on next send.",
                    session_id, _db_count,
                )
                # Tell the person the turn failed rather than leaving silence.
                # A8 asserts this honest-failure shape and passes on the stand.
                raise RuntimeError(_eof_error)

            # First failure: log, respawn once, then retry.
            logger.warning(
                "claude-cli session=%s died without result on attempt 1; "
                "retrying once. %s",
                session_id, _eof_error,
            )
            _TURN_TEXT[session_id] = ""
            if _retry_runtime is None:
                _retry_runtime = _load_session_runtime(session_id)
            _tail_for_mismatch = " | ".join(_STDERR_TAILS.get(session_id, [])[-3:])
            _resume = _session_has_turns(session_id)  # DB-first, same as respawn path
            if not _resume:
                _resume = _transcript_exists(
                    session_id, _runas_user(_retry_runtime.get("metadata")))
            if _RESUME_MISMATCH.search(_tail_for_mismatch or ""):
                _resume = not _resume
            await self.create(
                session_id, _retry_runtime["model"],
                _retry_runtime.get("system_prompt"),
                _retry_runtime.get("metadata") or {},
                resume=_resume,
            )
            # Loop continues to attempt 2 (index 1)

        # Map usage. CLI returns at least {input_tokens, output_tokens, ...}
        # when it provides usage. Fall back to tiktoken estimate if missing.
        if usage_raw:
            # input_tokens from this provider is the UNCACHED input only; the
            # cached halves are reported separately and are billed. Translate
            # them into the canonical pair rather than folding them in, so the
            # caller can still see what was uncached.
            usage = {
                "input_tokens": int(usage_raw.get("input_tokens", 0)),
                "output_tokens": int(usage_raw.get("output_tokens", 0)),
                "cache_write_tokens": int(usage_raw.get("cache_creation_input_tokens", 0)),
                "cache_read_tokens": int(usage_raw.get("cache_read_input_tokens", 0)),
                "cost_usd": cost_usd,
                "estimated": False,
                # result.usage sums the turn's internal requests. Without
                # request records it cannot measure the current context.
                "cumulative": True,
                "requests": request_usages,
            }
        else:
            usage = {
                "input_tokens": self.estimate_tokens(message),
                "output_tokens": self.estimate_tokens(content),
                # An estimate cannot see the cache, and saying 0 here would be a
                # measurement rather than an absence -- but `estimated` already
                # marks the whole row as approximate, so the pair stays 0 and the
                # flag carries the caveat.
                "cache_write_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": cost_usd,
                "estimated": True,
                "requests": request_usages,
            }

        # Silent-turn guard: the model ran tools but never emitted any assistant
        # text, so content is empty. Without this the caller stores "" and the
        # chat shows "The agent did not reply." with nothing to go back to. One
        # service line turns the silent end into an honest one. The owner asked for
        # this as a hotfix on 2026-11-04 after a live occurrence.
        if (not content and not _failed and not _stopped
                and tool_calls and not _done_early):
            n = len(tool_calls)
            content = ("[runtime] the turn ran %d tool call%s but ended without "
                       "any reply text. Try asking again." % (n, "" if n == 1 else "s"))
            if not final_answer:
                final_answer = content
            logger.warning("claude-cli session=%s: silent turn guard fired "
                           "(%d tool calls, no reply text)", session_id, n)

        return BackendResponse(
            content=content,
            usage=usage,
            tool_calls=tool_calls or None,
            stopped=_stopped,
            stopped_by=stopped_by,
            failed=_failed if not _stopped else None,
            final_answer=final_answer if not (_stopped or _failed) else "",
        )

    # ----------------------------------------------------------------- close

    async def close(self, session_id):
        async with _get_lock():
            proc = _PROCESSES.pop(session_id, None)
            _STDERR_TAILS.pop(session_id, None)
            _SEND_LOCKS.pop(session_id, None)
            _LAST_USED.pop(session_id, None)
            _TURN_STARTED.pop(session_id, None)
            _TURN_STATUS.pop(session_id, None)
            _TURN_TEXT.pop(session_id, None)
            _TURN_STEERS.pop(session_id, None)
            _SESSION_RUNTIME.pop(session_id, None)
            _UNDELIVERED.pop(session_id, None)
            _SESSION_TYPES.pop(session_id, None)
            _ANSWER_IDS.pop(session_id, None)
            _WAKE_DESK.pop(session_id, None)
            # Read BEFORE dropping it: _kill_tree resolves the owning user from
            # this dict, and popping it first would send the signal as the
            # gateway -- which an agent-owned process simply refuses.
            os_user = _RUNAS.pop(session_id, None)

        if proc is None:
            return

        if proc.returncode is not None:
            return  # already exited

        try:
            try:
                proc.stdin.close()
            except Exception:
                pass
            # Group kill, so `claude`'s own children go with it.
            await _kill_tree(session_id, proc, "session closed", os_user=os_user)
        except Exception as e:
            logger.warning("claude-cli session=%s close error: %s", session_id, e)

    # ---------------------------------------------------------------- resume

    async def resume(self, session_id, history):
        """Best-effort respawn. CLI-side conversation state is lost (opacity
        limit, see RFC v0.3); next send starts a fresh CLI conversation. The
        gateway-side history is preserved in ai_session_messages -- callers
        wanting continuity should inject a recap on the next send."""
        runtime = _load_session_runtime(session_id)
        await self.create(
            session_id,
            runtime["model"],
            runtime.get("system_prompt"),
            runtime.get("metadata") or {},
        )

    async def restart(self, session_id):
        """Restart the CLI process in place (UI 'Reload' button): respawn with
        --resume so the conversation survives, re-reading the agent's .claude
        config (CLAUDE.md, .mcp.json, settings) + system prompt. Falls back to a
        fresh process if the session has no on-disk transcript yet."""
        runtime = _load_session_runtime(session_id)
        await self.create(
            session_id, runtime["model"], runtime.get("system_prompt"),
            runtime.get("metadata") or {}, resume=True,
        )
        proc = _PROCESSES.get(session_id)
        resumed = proc is not None and proc.returncode is None
        if not resumed:
            tail = _STDERR_TAILS.get(session_id, [])
            hint = " | ".join(tail[-3:]) if tail else "no stderr"
            logger.warning("claude-cli session=%s --resume failed (%s); fresh respawn",
                           session_id, hint)
            await self.create(
                session_id, runtime["model"], runtime.get("system_prompt"),
                runtime.get("metadata") or {}, resume=False,
            )
            proc = _PROCESSES.get(session_id)
        return {
            "session_id": session_id,
            "status": "restarted",
            "pid": proc.pid if proc else None,
            "resumed": resumed,
        }


class FlintCliBackend(ClaudeCliBackend):
    """Flint's stream-json runtime, sharing CLI process and activity handling.

    Flint accepts both --session-id and --resume for a known or new session id.
    Its provider key is transferred to the agent wrapper over stdin so sudo
    never receives the value in argv or its logged environment.
    """

    provider_name = "flint-cli"
    runtime_name = "flint-cli"

    async def resume(self, session_id, history):
        runtime = _load_session_runtime(session_id)
        await self.create(
            session_id, runtime["model"], runtime.get("system_prompt"),
            runtime.get("metadata") or {}, resume=True,
        )
