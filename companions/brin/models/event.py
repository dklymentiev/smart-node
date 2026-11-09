"""Pydantic models for EventBus -- persistent event queue."""

from typing import Optional
from pydantic import BaseModel


class EventRecord(BaseModel):
    """Single event from the store."""
    id: int
    event_type: str
    source: str = ""
    payload: dict = {}
    created_at: str = ""
    read: bool = False


class EventListResult(BaseModel):
    """Response for event listing."""
    events: list[EventRecord] = []
    total: int = 0
    unread: int = 0


class EmitEventRequest(BaseModel):
    """Request to emit a new event."""
    event_type: str
    payload: dict = {}
    source: str = ""
    ttl_hours: Optional[int] = None
