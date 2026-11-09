"""Notification delivery models."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class NotificationChannel(str, Enum):
    """Supported notification channels."""
    telegram = "telegram"
    email = "email"


class NotificationRequest(BaseModel):
    """Request to send a notification."""
    text: str
    channel: NotificationChannel = NotificationChannel.telegram
    recipient: Optional[str] = None
    subject: Optional[str] = None
    parse_mode: Optional[str] = None
    priority: str = "normal"


class NotificationResult(BaseModel):
    """Result of notification delivery."""
    sent: bool = False
    channel: str = ""
    recipient: str = ""
    message_id: Optional[str] = None
    error: Optional[str] = None
