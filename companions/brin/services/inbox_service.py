"""
Inbox Service -- save incoming messages to Mesh memory with auto-tagging.

Ported from brin-client-tg/src/services/inbox_service.py.
Drops: TelegramMessage dependency, mem-cli subprocess, Telegram file download.
Uses: httpx direct POST to Mesh API.
"""

import logging
import os
import re
from datetime import datetime
from typing import Optional

import httpx

from models.inbox import IncomingMessage, InboxSaveResult, ContentKind

logger = logging.getLogger(__name__)

MESH_URL = os.environ.get("MESH_API_URL", "http://localhost:8000").rstrip("/")

# URL detection
URL_PATTERN = re.compile(r'https?://[^\s<>"{}|\\^`\[\]]+')

# Platform detection
PLATFORM_PATTERNS = {
    "twitter": re.compile(r'(twitter\.com|x\.com)'),
    "tiktok": re.compile(r'(tiktok\.com|vm\.tiktok\.com)'),
    "youtube": re.compile(r'(youtube\.com|youtu\.be)'),
    "telegram": re.compile(r't\.me'),
    "github": re.compile(r'github\.com'),
    "linkedin": re.compile(r'linkedin\.com'),
}


class InboxService:
    """Save incoming messages to Mesh memory with auto-tagging."""

    async def save(self, msg: IncomingMessage) -> InboxSaveResult:
        """Save incoming message to Mesh memory."""
        try:
            kind = self._detect_kind(msg)
            text = msg.text or msg.caption or ""

            # Skip empty text messages
            if kind == ContentKind.text and len(text.strip()) < 3:
                return InboxSaveResult(error="Message too short")

            content, extra_tags = self._extract_content(msg, kind, text)
            if not content:
                return InboxSaveResult(error="No content to save")

            # Build tags
            today = datetime.now().strftime("%Y-%m-%d")
            tags = [
                "type:inbox",
                f"kind:{kind.value}",
                f"date:{today}",
                f"source:{msg.source}",
            ]
            tags.extend(extra_tags)

            if msg.file_path:
                tags.append(f"file_path:{msg.file_path}")
            if msg.is_forwarded:
                tags.append("forwarded:true")

            # Save to Mesh via HTTP
            guid = await self._save_to_mesh(content, tags)

            preview = content[:100] + "..." if len(content) > 100 else content
            logger.info(
                "Inbox saved",
                extra={"action": "inbox_save", "kind": kind.value, "guid": guid},
            )
            return InboxSaveResult(
                guid=guid, kind=kind.value, tags=tags, content_preview=preview
            )

        except Exception as e:
            logger.error(f"Inbox save error: {e}", extra={"action": "inbox_save"})
            return InboxSaveResult(error=str(e))

    def _detect_kind(self, msg: IncomingMessage) -> ContentKind:
        """Detect content kind from message fields."""
        # Explicit kind from caller
        if msg.kind:
            return msg.kind

        # Infer from file_path / mime_type
        if msg.file_path:
            mime = (msg.mime_type or "").lower()
            if mime.startswith("image/"):
                return ContentKind.photo
            if mime.startswith("video/"):
                return ContentKind.video
            if mime.startswith("audio/"):
                return ContentKind.audio
            return ContentKind.document

        # Check text for URLs
        text = msg.text or ""
        if URL_PATTERN.search(text):
            return ContentKind.url

        return ContentKind.text

    def _extract_content(
        self, msg: IncomingMessage, kind: ContentKind, text: str
    ) -> tuple[str, list[str]]:
        """Build content string and extra tags."""
        extra_tags: list[str] = []

        if kind == ContentKind.url:
            urls = URL_PATTERN.findall(text)
            for url in urls:
                for platform, pattern in PLATFORM_PATTERNS.items():
                    if pattern.search(url):
                        extra_tags.append(f"platform:{platform}")
                        break
            return text, extra_tags

        if kind == ContentKind.text:
            return text, extra_tags

        # Media kinds: photo, video, audio, voice, document
        label = kind.value.upper()
        caption = msg.text or msg.caption or ""
        if msg.file_name:
            file_info = f"[{label}: {msg.file_name}]"
        else:
            file_info = f"[{label}]"

        content = f"{file_info} {caption}".strip() if caption else file_info
        if msg.file_path:
            extra_tags.append("has_file:true")
        return content, extra_tags

    async def _save_to_mesh(self, content: str, tags: list[str]) -> Optional[str]:
        """PUT to Mesh API to save document."""
        body = {"content": content, "tags": tags, "source": "omni-inbox"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.put(f"{MESH_URL}/", json=body)
            r.raise_for_status()
            data = r.json()
            return data.get("guid")
