"""Event log Pydantic models for sn_core.events.

Used by routes/events.py (HTTP) and events_emit.emit (in-process).
"""

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class EventBase(BaseModel):
    actor: str = Field(
        min_length=1, max_length=200,
        description="Who/what caused the event: system:cron, agent:<id>, user:<name>, component:<name>.",
    )
    event_type: str = Field(
        min_length=1, max_length=120,
        description="verb-noun: controller.heartbeat, cron.started, tool.registered, etc.",
    )
    entity_type: str | None = Field(
        default=None, max_length=80,
        description="Optional reference: tool, integration, cron, mcp_server, etc.",
    )
    entity_id: str | None = Field(
        default=None, max_length=200,
        description="Optional reference: id or slug of the entity_type row.",
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_event_id: int | None = None


class EventCreate(EventBase):
    pass


class Event(EventBase):
    id: int
    ts: datetime
