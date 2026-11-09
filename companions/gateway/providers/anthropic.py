"""
Anthropic API Provider
"""
import json
from typing import List, AsyncIterator
from models import Message
from providers.base import BaseProvider
from config import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, ANTHROPIC_DEFAULT_MODEL
from sse_parser import SSEParser


class AnthropicProvider(BaseProvider):
    """Anthropic Claude API provider"""

    door_name = "anthropic"

    def door_format(self):
        import anthropic_relay
        return anthropic_relay.FORMAT

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.model = model or ANTHROPIC_DEFAULT_MODEL
        self.base_url = ANTHROPIC_BASE_URL

    def _headers(self):
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    async def chat(self, messages: List[Message], model: str = None, max_tokens: int = None) -> dict:
        """Send messages to Anthropic API"""
        use_model = model or self.model
        system = None
        chat_messages = []

        for msg in messages:
            if msg.role == "system":
                system = msg.content
            else:
                chat_messages.append({"role": msg.role, "content": msg.content})

        client = await self.get_client()
        response = await client.post(
            f"{self.base_url}/messages",
            headers=self._headers(),
            json={
                "model": use_model,
                "max_tokens": max_tokens or 4096,
                "system": system,
                "messages": chat_messages,
            },
        )

        response.raise_for_status()
        data = response.json()

        return {
            "id": data.get("id", ""),
            "content": data["content"][0]["text"],
            "usage": {
                "input_tokens": data["usage"]["input_tokens"],
                "output_tokens": data["usage"]["output_tokens"],
            }
        }

    async def chat_stream(self, messages: List[Message], model: str = None) -> AsyncIterator[dict]:
        """Stream response from Anthropic API"""
        use_model = model or self.model
        system = None
        chat_messages = []

        for msg in messages:
            if msg.role == "system":
                system = msg.content
            else:
                chat_messages.append({"role": msg.role, "content": msg.content})

        client = await self.get_client()
        parser = SSEParser()

        async with client.stream(
            "POST",
            f"{self.base_url}/messages",
            headers=self._headers(),
            json={
                "model": use_model,
                "max_tokens": 4096,
                "system": system,
                "messages": chat_messages,
                "stream": True,
            },
        ) as response:
            async for line in parser.parse(response):
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        if data.get("type") == "content_block_delta":
                            yield {
                                "chunk": data["delta"].get("text", ""),
                                "done": False
                            }
                        elif data.get("type") == "message_stop":
                            yield {"chunk": "", "done": True}
                    except json.JSONDecodeError:
                        continue

    async def health_check(self) -> bool:
        """Check if Anthropic API is accessible"""
        if not self.api_key:
            return False
        try:
            client = await self.get_client()
            response = await client.post(
                f"{self.base_url}/messages",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception:
            return False
