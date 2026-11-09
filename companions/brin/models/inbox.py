"""Models for inbox (incoming message) processing."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class ContentKind(str, Enum):
    """Type of incoming content."""
    text = "text"
    url = "url"
    photo = "photo"
    video = "video"
    audio = "audio"
    voice = "voice"
    document = "document"


class IncomingMessage(BaseModel):
    """Channel-agnostic incoming message."""
    text: Optional[str] = None
    caption: Optional[str] = None
    kind: Optional[ContentKind] = None
    source: str = "mcp"
    file_path: Optional[str] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    is_forwarded: bool = False
    metadata: Optional[dict] = None


class InboxSaveResult(BaseModel):
    """Result of saving to inbox."""
    guid: Optional[str] = None
    kind: str = "text"
    tags: list[str] = []
    content_preview: str = ""
    error: Optional[str] = None
