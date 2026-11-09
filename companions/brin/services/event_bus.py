"""EventBus -- async pub/sub with persistent storage, SSE streaming, and TTL cleanup."""

import asyncio
import logging
from functools import partial
from typing import Callable, Optional

from services.event_store import EventStore

logger = logging.getLogger("brin.event_bus")

_CLEANUP_INTERVAL = 3600  # 1 hour


class EventBus:
    """Async event bus backed by EventStore (SQLite).

    Features:
    - Persistent storage (survives restarts)
    - In-process pub/sub (event_type -> handler)
    - SSE push to web clients
    - Startup drain (process unread events accumulated while offline)
    - Periodic TTL cleanup
    """

    def __init__(self, store: EventStore):
        self._store = store
        self._subscribers: dict[str, list[Callable]] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

    # -- Lifecycle --

    async def startup(self):
        """Start cleanup loop and drain unread events."""
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        drained = await self.drain_unread()
        logger.info("EventBus started (drained %d unread events)", drained)

    async def shutdown(self):
        """Cancel cleanup loop and close SSE queues."""
        self._running = False
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("EventBus shut down")

    # -- Subscribe --

    def subscribe(self, event_type: str, handler: Callable):
        """Register a handler for an event type. Handler: async fn(event_record)."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        logger.debug("Subscribed to '%s': %s", event_type, handler.__name__)

    # -- Emit --

    async def emit(self, event_type: str, payload: dict = None,
                   source: str = "", ttl_hours: int = None) -> int:
        """Persist event, dispatch to subscribers, push to SSE queues.

        Returns the event ID.
        """
        loop = asyncio.get_event_loop()
        event_id = await loop.run_in_executor(
            None,
            partial(self._store.insert, event_type, payload or {}, source, ttl_hours),
        )

        # Build record-like dict for handlers
        event_data = {
            "id": event_id,
            "event_type": event_type,
            "source": source,
            "payload": payload or {},
        }

        # Dispatch to in-process subscribers (fire-and-forget)
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            asyncio.create_task(self._safe_dispatch(handler, event_data))

        logger.info("Event emitted: %s (id=%d, source=%s)", event_type, event_id, source)
        return event_id

    async def _safe_dispatch(self, handler: Callable, event_data: dict):
        """Run handler with error protection."""
        try:
            await handler(event_data)
        except Exception:
            logger.exception("Event handler %s failed for %s",
                             handler.__name__, event_data.get("event_type"))

    # -- Drain --

    async def drain_unread(self) -> int:
        """Process unread events accumulated while offline. Returns count."""
        loop = asyncio.get_event_loop()
        unread = await loop.run_in_executor(None, self._store.get_unread)
        if not unread:
            return 0

        for event in unread:
            handlers = self._subscribers.get(event.event_type, [])
            event_data = {
                "id": event.id,
                "event_type": event.event_type,
                "source": event.source,
                "payload": event.payload,
            }
            for handler in handlers:
                await self._safe_dispatch(handler, event_data)

        # Mark all as read
        ids = [e.id for e in unread]
        await loop.run_in_executor(None, self._store.mark_all_read, ids)

        logger.info("Drained %d unread events", len(unread))
        return len(unread)

    # -- Query --

    async def list_events(self, event_type: Optional[str] = None,
                          limit: int = 50) -> list[dict]:
        """List recent events."""
        loop = asyncio.get_event_loop()
        records = await loop.run_in_executor(
            None,
            partial(self._store.list_recent, event_type, limit),
        )
        return [r.model_dump() for r in records]

    async def count_unread(self) -> int:
        """Count unread events."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._store.count_unread)

    # -- Cleanup --

    async def _cleanup_loop(self):
        """Periodically remove expired events."""
        while self._running:
            try:
                await asyncio.sleep(_CLEANUP_INTERVAL)
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._store.cleanup_expired)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Event cleanup failed")
