"""OpenrouterBackend -- first HTTP-style SessionBackend.

Wraps existing providers/openrouter.OpenRouterProvider. Transparent (not opaque):
- Token counts from provider response (accurate)
- Full PII filter applies (pre-send + post-receive)
- Single chat completion per send call (multi-turn via session history)

Model id format: "openrouter/<openrouter-model-id>". The prefix is stripped
before calling OpenRouter. Examples:
    "openrouter/anthropic/claude-3-haiku" -> openrouter model "anthropic/claude-3-haiku"
    "openrouter/google/gemini-2.0-flash"  -> openrouter model "google/gemini-2.0-flash"
"""
import logging
from typing import Optional, List, Dict, Any

from backends import SessionBackend, BackendResponse
from models import Message
from providers.openrouter import OpenRouterProvider
import db

logger = logging.getLogger(__name__)


class OpenrouterBackend(SessionBackend):
    is_opaque = False
    provider_name = "openrouter"

    def __init__(self):
        # Stateless backend: one OpenRouterProvider instance shared across sessions.
        # Provider's httpx client is lazy + persistent (auto-pooled).
        self._provider = OpenRouterProvider()

    async def create(self, session_id, model, system_prompt=None, metadata=None):
        # Stateless: nothing to provision. DB row is created by SessionService.
        return None

    async def send(self, session_id, history, message):
        """Send via OpenRouter. history is list of dicts from ai_session_messages.
        Returns BackendResponse with accurate token usage from provider."""
        # Fetch session config (model + system_prompt) from DB
        rows = db.query(
            "SELECT model, system_prompt FROM sn_core.ai_sessions WHERE id = %s",
            (session_id,),
            fetch=True,
        )
        if not rows:
            raise ValueError(f"session {session_id} not found")
        full_model_id, system_prompt = rows[0]

        # Strip provider prefix: "openrouter/anthropic/claude-3-haiku" -> "anthropic/claude-3-haiku"
        if "/" not in full_model_id:
            raise ValueError(f"model_id {full_model_id!r} missing provider prefix")
        openrouter_model = full_model_id.split("/", 1)[1]

        # Convert SessionService history (list of dicts) -> list[Message]
        msgs: List[Message] = []
        for h in history:
            role = h["role"]
            if role not in ("user", "assistant", "system", "tool"):
                continue  # skip operator-injected outside protocol
            msgs.append(Message(
                role=role,
                content=h.get("content"),
                tool_calls=h.get("tool_calls"),
                tool_call_id=h.get("tool_call_id"),
            ))

        # Append the new user message
        msgs.append(Message(role="user", content=message))

        # Make the call (provider handles system prompt prepending)
        logger.info("openrouter.send session=%s model=%s history=%d msgs",
                    session_id, openrouter_model, len(msgs))
        try:
            result = await self._provider.chat(
                msgs,
                model=openrouter_model,
                system=system_prompt,
            )
        except Exception as e:
            logger.exception("openrouter.send failed session=%s: %s", session_id, e)
            raise

        # Map provider usage -> backend canonical usage shape
        provider_usage = result.get("usage", {}) or {}
        # OpenAI-compatible APIs report cached prompt tokens under
        # prompt_tokens_details.cached_tokens, and -- unlike Anthropic -- count
        # them INSIDE prompt_tokens rather than beside it. So the cached figure
        # is surfaced without being added back, and input_tokens stays the
        # provider's own total. There is no separate cache-write figure here.
        _details = provider_usage.get("prompt_tokens_details") or {}
        usage = {
            "input_tokens": provider_usage.get("prompt_tokens", 0),
            "output_tokens": provider_usage.get("completion_tokens", 0),
            "cache_read_tokens": int(_details.get("cached_tokens", 0) or 0),
            "cache_write_tokens": 0,
            "estimated": False,  # accurate from openrouter
        }

        return BackendResponse(
            content=result.get("content", "") or "",
            usage=usage,
            tool_calls=result.get("tool_calls"),
        )

    async def close(self, session_id):
        # Nothing per-session to free (stateless HTTP).
        return None

    async def resume(self, session_id, history):
        # HTTP is stateless -- next send will load history from caller naturally.
        return None
