"""Shared session models for cross-channel conversation handoff."""

from typing import Optional
from pydantic import BaseModel


class SessionMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str
    timestamp: Optional[str] = None


class CreateSessionRequest(BaseModel):
    channel: str  # "email", "telegram", etc.
    messages: list[SessionMessage]
    specialist: Optional[str] = None
    model: str = "haiku"
    metadata: dict = {}


class CreateSessionResponse(BaseModel):
    session_id: str
    url: str


class SharedSession(BaseModel):
    id: str
    channel: str
    messages: list[SessionMessage]
    specialist: Optional[str] = None
    model: str = "haiku"
    created_at: str
    expires_at: str
    metadata: dict = {}
