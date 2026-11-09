"""
OpenRouter API Provider
"""
import json
from typing import List, AsyncIterator
from models import Message
from providers.base import BaseProvider
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_DEFAULT_MODEL
from secrets_store import get_provider_key
from sse_parser import SSEParser


class OpenRouterProvider(BaseProvider):
    """OpenRouter API provider - access to multiple models"""

    door_name = "openrouter"

    def door_format(self):
        import openai_relay
        return openai_relay.FORMAT

    def __init__(self, api_key: str = None, model: str = None):
        self._explicit_key = api_key
        self.model = model or OPENROUTER_DEFAULT_MODEL
        self.base_url = OPENROUTER_BASE_URL

    @property
    def api_key(self):
        # Resolved lazily from the encrypted store (gateway_secrets) so a key set
        # in AI Gateway settings applies without a restart. Explicit ctor arg wins
        # (tests); otherwise the store is the source of truth -- no env fallback.
        if self._explicit_key:
            return self._explicit_key
        return get_provider_key("openrouter")

    def _headers(self):
        key = self.api_key
        if not key:
            raise RuntimeError("openrouter: no API key configured (set it in AI Gateway settings)")
        return {
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/your-org/ai-gateway",
        }

    async def chat(self, messages: List[Message], model: str = None,
                   max_tokens: int = None, tools: list = None,
                   tool_choice: str = None, system: str = None,
                   timeout: float = None) -> dict:
        """Send messages to OpenRouter API.

        tools/tool_choice/system are OpenAI-format passthroughs. When the model
        emits tool_calls the returned dict carries `tool_calls` (a list of
        OpenAI-format calls) and `content` may be empty.
        """
        use_model = model or self.model
        chat_messages = []
        for msg in messages:
            entry = {"role": msg.role}
            if msg.content is not None:
                entry["content"] = msg.content
            if getattr(msg, "tool_calls", None):
                entry["tool_calls"] = msg.tool_calls
            if getattr(msg, "tool_call_id", None):
                entry["tool_call_id"] = msg.tool_call_id
            if getattr(msg, "name", None):
                entry["name"] = msg.name
            chat_messages.append(entry)
        # OpenAI convention: system prompt is the first message with role=system.
        if system and not (chat_messages and chat_messages[0].get("role") == "system"):
            chat_messages = [{"role": "system", "content": system}] + chat_messages

        body = {
            "model": use_model,
            "messages": chat_messages,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice

        client = await self.get_client()
        response = await client.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=body,
            timeout=self.call_timeout(timeout),
        )

        response.raise_for_status()
        data = response.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})

        return {
            "id": data.get("id", ""),
            "content": msg.get("content") or "",
            "usage": data.get("usage", {}),
            "tool_calls": msg.get("tool_calls") or None,
        }

    async def chat_stream(self, messages: List[Message], model: str = None) -> AsyncIterator[dict]:
        """Stream response from OpenRouter API"""
        use_model = model or self.model
        chat_messages = [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]

        client = await self.get_client()
        parser = SSEParser()

        async with client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json={
                "model": use_model,
                "messages": chat_messages,
                "stream": True,
            },
        ) as response:
            async for line in parser.parse(response):
                if line.startswith("data: "):
                    content = line[6:]
                    if content == "[DONE]":
                        yield {"chunk": "", "done": True}
                        break
                    try:
                        data = json.loads(content)
                        if "choices" in data and data["choices"]:
                            delta = data["choices"][0].get("delta", {})
                            text = delta.get("content", "")
                            if text:
                                yield {"chunk": text, "done": False}
                    except json.JSONDecodeError:
                        continue

    async def health_check(self) -> bool:
        """Check if OpenRouter API is accessible"""
        if not self.api_key:
            return False
        try:
            client = await self.get_client()
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 10,
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception:
            return False
