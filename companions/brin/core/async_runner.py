"""Async tool runner: run a long operation off the turn and land
its result in the DURABLE ledger, not process memory.

WHY: today a long action (e.g. ask_brin on voice) runs in a background task and
parks its result in an in-memory list. If the channel disconnects or the service
restarts before the result is announced, it is lost -- the async gap we kept hitting.

The contract here closes that gap:

    1. launch() returns a call_id immediately (no blocking)
    2. the coroutine runs detached
    3. on completion it writes a terminal (tool.succeeded / tool.failed) into
       sn_core.events, tagged with that call_id
    4. the next prompt assembly surfaces it via the [tool calls] section
       (ledger_service.fetch_tool_calls correlates invoked<->terminal by call_id)

Because the terminal lives in the event log, the result survives disconnect and
restart: whoever talks to the agent next sees it, with no in-memory hand-off.
"""

import asyncio
import logging
import uuid

from services.ledger_service import emit_event

logger = logging.getLogger("brin.async_runner")

# Cap on what we store as the result snippet in the ledger event (full output, if
# any, lives wherever the operation itself wrote it -- the ledger is a status view).
_RESULT_MAX = 4000


async def _run(call_id: str, actor: str, name: str, coro) -> None:
    """Await the operation, then write its terminal to the ledger under call_id."""
    try:
        result = await coro
    except Exception as e:
        logger.warning("async tool %s (%s) failed: %s", name, call_id, e)
        await emit_event(actor, "tool.failed", name,
                         {"call_id": call_id, "name": name, "error": str(e)[:_RESULT_MAX]})
        return
    await emit_event(actor, "tool.succeeded", name,
                     {"call_id": call_id, "name": name, "result": str(result)[:_RESULT_MAX]})
    logger.info("async tool %s (%s) done", name, call_id)


async def launch(agent_slug: str, name: str, coro) -> str:
    """Start `coro` in the background and return its call_id immediately.

    Emits tool.invoked (with the call_id) up front so the operation shows as
    IN PROGRESS in the ledger the moment it starts; the detached task emits the
    terminal when it finishes. The caller does NOT await the result -- it reads it
    later from the [tool calls] section keyed by this call_id.
    """
    call_id = uuid.uuid4().hex[:12]
    actor = f"agent:{agent_slug}"
    await emit_event(actor, "tool.invoked", name, {"call_id": call_id, "name": name})
    # Detach: the task outlives this request. asyncio keeps a reference via the loop;
    # we also log on completion so a silent death is visible.
    asyncio.create_task(_run(call_id, actor, name, coro))
    return call_id
