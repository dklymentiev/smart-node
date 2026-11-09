"""
Notification Service -- send messages via Telegram or Email.

Telegram: Bot API sendMessage with chunking for long messages.
Email: SMTP via local Mailcow with STARTTLS.
"""

import asyncio
import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import httpx

from models.notification import NotificationChannel, NotificationRequest, NotificationResult

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096


def _load_env_with_fallback(key: str, fallback_file: str = "/opt/smart-node/brin-client-tg/.env") -> Optional[str]:
    """Load env var, fall back to brin-client-tg .env."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        for line in open(fallback_file):
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


class NotificationService:
    """Send notifications via Telegram or Email."""

    def __init__(self):
        # Telegram config
        self.tg_token = _load_env_with_fallback("TELEGRAM_BOT_TOKEN")
        self.tg_owner_id = _load_env_with_fallback("TELEGRAM_OWNER_ID")
        self.tg_base_url = f"https://api.telegram.org/bot{self.tg_token}" if self.tg_token else None

        # SMTP config
        self.smtp_host = os.environ.get("SMTP_HOST", "mail.local")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER")
        self.smtp_password = os.environ.get("SMTP_PASSWORD")
        self.smtp_from = os.environ.get("SMTP_FROM", "noreply@example.com")

        # Try loading SMTP creds from secrets file
        if not self.smtp_user:
            self._load_smtp_secrets()

        channels = []
        if self.tg_token:
            channels.append("telegram")
        if self.smtp_host:
            channels.append("email")
        logger.info("NotificationService initialized", extra={"action": "notify_init", "channels": channels})

    def _load_smtp_secrets(self):
        """Load SMTP credentials from secrets file."""
        secrets_file = "/etc/smart-node/secrets/smtp-credentials"
        try:
            for line in open(secrets_file):
                line = line.strip()
                if line.startswith("SMTP_USER="):
                    self.smtp_user = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("SMTP_PASSWORD="):
                    self.smtp_password = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("SMTP_FROM="):
                    self.smtp_from = line.split("=", 1)[1].strip().strip('"')
        except FileNotFoundError:
            pass

    async def send(self, req: NotificationRequest) -> NotificationResult:
        """Route notification to the appropriate channel."""
        if req.channel == NotificationChannel.telegram:
            return await self._send_telegram(req)
        elif req.channel == NotificationChannel.email:
            return await self._send_email(req)
        else:
            return NotificationResult(error=f"Unknown channel: {req.channel}")

    # -- Telegram --

    async def _send_telegram(self, req: NotificationRequest) -> NotificationResult:
        """Send message via Telegram Bot API."""
        if not self.tg_token:
            return NotificationResult(channel="telegram", error="TELEGRAM_BOT_TOKEN not configured")

        chat_id = req.recipient or self.tg_owner_id
        if not chat_id:
            return NotificationResult(channel="telegram", error="No recipient: set TELEGRAM_OWNER_ID or pass recipient")

        text = req.text
        parse_mode = req.parse_mode

        try:
            # Split long messages into chunks
            chunks = self._split_message(text)
            last_msg_id = None

            for i, chunk in enumerate(chunks):
                msg_id = await self._tg_send_message(chat_id, chunk, parse_mode)
                last_msg_id = msg_id
                if i < len(chunks) - 1:
                    await asyncio.sleep(1)

            logger.info(
                "Telegram notification sent",
                extra={"action": "notify_telegram", "chat_id": chat_id, "chunks": len(chunks)},
            )
            return NotificationResult(
                sent=True,
                channel="telegram",
                recipient=str(chat_id),
                message_id=str(last_msg_id) if last_msg_id else None,
            )

        except Exception as e:
            logger.error("Telegram send failed", extra={"action": "notify_telegram", "error": str(e)})
            return NotificationResult(channel="telegram", recipient=str(chat_id), error=str(e))

    async def _tg_send_message(
        self, chat_id: str, text: str, parse_mode: Optional[str] = None
    ) -> Optional[int]:
        """Send a single message via Telegram API. Retries without parse_mode on failure."""
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{self.tg_base_url}/sendMessage", json=payload)

            if r.status_code == 400 and parse_mode:
                # Retry without parse_mode (Markdown parse error)
                logger.warning(
                    "Telegram parse error, retrying as plain text",
                    extra={"action": "notify_telegram_retry"},
                )
                payload.pop("parse_mode", None)
                r = await client.post(f"{self.tg_base_url}/sendMessage", json=payload)

            r.raise_for_status()
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            raise RuntimeError(data.get("description", "Unknown Telegram error"))

    @staticmethod
    def _split_message(text: str) -> list[str]:
        """Split text into chunks of TELEGRAM_MAX_LENGTH, preferring line breaks."""
        if len(text) <= TELEGRAM_MAX_LENGTH:
            return [text]

        chunks = []
        while text:
            if len(text) <= TELEGRAM_MAX_LENGTH:
                chunks.append(text)
                break

            # Find a good split point
            split_at = text.rfind("\n", 0, TELEGRAM_MAX_LENGTH)
            if split_at < TELEGRAM_MAX_LENGTH // 2:
                split_at = text.rfind(" ", 0, TELEGRAM_MAX_LENGTH)
            if split_at < TELEGRAM_MAX_LENGTH // 2:
                split_at = TELEGRAM_MAX_LENGTH

            chunks.append(text[:split_at])
            text = text[split_at:].lstrip("\n")

        return chunks

    # -- Email --

    async def _send_email(self, req: NotificationRequest) -> NotificationResult:
        """Send email via SMTP (Mailcow). Runs blocking SMTP in executor."""
        if not req.recipient:
            return NotificationResult(channel="email", error="Email recipient is required")

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._smtp_send, req.recipient, req.subject, req.text)

            logger.info(
                "Email notification sent",
                extra={"action": "notify_email", "recipient": req.recipient},
            )
            return NotificationResult(
                sent=True,
                channel="email",
                recipient=req.recipient,
            )

        except Exception as e:
            logger.error("Email send failed", extra={"action": "notify_email", "error": str(e)})
            return NotificationResult(channel="email", recipient=req.recipient or "", error=str(e))

    def _smtp_send(self, to: str, subject: Optional[str], body: str):
        """Blocking SMTP send via Mailcow."""
        msg = MIMEMultipart("alternative")
        msg["From"] = self.smtp_from
        msg["To"] = to
        msg["Subject"] = subject or "Notification from Brin"
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
            server.starttls()
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.smtp_from, [to], msg.as_string())
