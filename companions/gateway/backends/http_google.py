"""GoogleBackend -- direct Gemini SessionBackend.

Wraps existing providers/gemini.GeminiProvider. Transparent (not opaque):
- Token counts from usageMetadata (accurate)
- Full PII filter applies (pre-send + post-receive)
- Single generateContent per send call (multi-turn via session history)

Model id format: "gemini/<gemini-model-id>". The prefix is stripped before
calling the Google API. Examples:
    "gemini/gemini-flash-latest" -> generativelanguage model "gemini-flash-latest"
    "gemini/gemini-1.5-pro"      -> generativelanguage model "gemini-1.5-pro"

The upstream key is 'gemini' rather than 'google' so it lines up with
ALLOWED_MODELS['gemini'] in config.py and with sn_config.models.provider='gemini'
seeded in bootstrap/B02-sn-core-seed.sql. The placeholder in __init__.py that
said 'google' was ahead of any real usage; the runtime catalog standardizes on 'gemini'.
"""
import logging
from typing import List

from backends import SessionBackend, BackendResponse
from models import Message
from providers.gemini import GeminiProvider
import db

logger = logging.getLogger(__name__)


class GoogleBackend(SessionBackend):
    is_opaque = False
    provider_name = "gemini"

    def __init__(self):
        # Stateless backend: one GeminiProvider instance shared across sessions.
        # Provider's httpx client is lazy + persistent (auto-pooled).
        self._provider = GeminiProvider()

    async def create(self, session_id, model, system_prompt=None, metadata=None):
        # Stateless: nothing to provision. DB row is created by SessionService.
        return None

    async def send(self, session_id, history, message):
        """Send via Google Gemini. history is list of dicts from ai_session_messages.
        Returns BackendResponse with accurate token usage from usageMetadata."""
        # Fetch session config (model + system_prompt) from DB
        rows = db.query(
            "SELECT model, system_prompt FROM sn_core.ai_sessions WHERE id = %s",
            (session_id,),
            fetch=True,
        )
        if not rows:
            raise ValueError(f"session {session_id} not found")
        full_model_id, system_prompt = rows[0]

        # Strip provider prefix: "gemini/gemini-flash-latest" -> "gemini-flash-latest"
        if "/" not in full_model_id:
            raise ValueError(f"model_id {full_model_id!r} missing provider prefix")
        gemini_model = full_model_id.split("/", 1)[1]

        # Convert SessionService history (list of dicts) -> list[Message].
        # sessions_v2 send_message appends the inbound user row BEFORE it reads
        # history, so the last row is this same turn (possibly its raw_content
        # form). Drop it and send `message`, the enriched text, once; otherwise
        # Gemini receives the question twice.
        turns = list(history)
        if turns and turns[-1].get("role") == "user":
            turns = turns[:-1]
        msgs: List[Message] = []
        for h in turns:
            role = h["role"]
            if role not in ("user", "assistant"):
                continue  # system goes as systemInstruction; tool rows have no Gemini shape in Track 1
            msgs.append(Message(role=role, content=h.get("content")))

        msgs.append(Message(role="user", content=message))

        logger.info("gemini.send session=%s model=%s history=%d msgs",
                    session_id, gemini_model, len(msgs))
        try:
            result = await self._provider.chat(msgs, model=gemini_model,
                                               system=system_prompt or None)
        except Exception as e:
            logger.exception("gemini.send failed session=%s: %s", session_id, e)
            raise

        # Map Gemini usageMetadata -> backend canonical usage shape.
        # Google names: promptTokenCount, candidatesTokenCount, cachedContentTokenCount,
        # totalTokenCount. Cache-write is not reported separately by Gemini today, so
        # cache_write_tokens stays 0 (consistent with OpenrouterBackend's handling).
        provider_usage = result.get("usage", {}) or {}
        usage = {
            "input_tokens": int(provider_usage.get("promptTokenCount", 0) or 0),
            "output_tokens": int(provider_usage.get("candidatesTokenCount", 0) or 0),
            "cache_read_tokens": int(provider_usage.get("cachedContentTokenCount", 0) or 0),
            "cache_write_tokens": 0,
            "estimated": False,  # accurate from Google's usageMetadata
        }

        return BackendResponse(
            content=result.get("content", "") or "",
            usage=usage,
            tool_calls=None,  # Gemini function-calling not wired in Track 1
        )

    async def close(self, session_id):
        # Nothing per-session to free (stateless HTTP).
        return None

    async def resume(self, session_id, history):
        # HTTP is stateless -- next send will load history from caller naturally.
        return None
