"""Intent classification and search parsing models."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class Intent(str, Enum):
    """User intent categories."""
    diagnostic = "diagnostic"
    question = "question"
    file_op = "file_op"
    save = "save"
    search = "search"
    model_settings = "model_settings"
    image_generation = "image_generation"
    video_download = "video_download"
    video_analyze = "video_analyze"
    session = "session"
    hint = "hint"
    check_inbox = "check_inbox"  # the fast tier: "any new colleague messages?"
    chat = "chat"
    unknown = "unknown"


class ClassifyRequest(BaseModel):
    """Request for intent classification."""
    text: str
    context: Optional[dict] = None


class ClassifyResult(BaseModel):
    """Result of intent classification."""
    intent: str
    confidence: Optional[str] = None
    error: Optional[str] = None


class SearchParseRequest(BaseModel):
    """Request for search query parsing."""
    text: str


class SearchParseResult(BaseModel):
    """Structured search parameters extracted from natural language."""
    scope: str = "all"
    kind: Optional[str] = None
    platform: Optional[str] = None
    query: str = ""
    error: Optional[str] = None


class ModelParseRequest(BaseModel):
    """Request for model command parsing."""
    text: str


class ModelParseResult(BaseModel):
    """Structured model command extracted from natural language."""
    action: str = ""
    service: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None


class SessionParseResult(BaseModel):
    """Structured session command extracted from natural language."""
    action: str = ""  # switch, list, delete, deactivate, status
    name: Optional[str] = None
    error: Optional[str] = None
