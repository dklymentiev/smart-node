"""
AI Gateway Client - Simple Python client for AI Gateway API

Usage:
    from client import AIGateway

    ai = AIGateway()
    response = await ai.chat("What is 2+2?")
"""
import httpx
from typing import Optional, AsyncIterator


class AIGateway:
    """
    AI Gateway client for Claude CLI sessions.

    Supports both one-shot calls and persistent sessions.
    """

    def __init__(
        self,
        model: str = "haiku",
        base_url: str = "http://host.docker.internal:19850",
        timeout: float = 120.0,
        service: str = "default",
    ):
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.service = service
        self._session_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def chat(self, prompt: str, model: Optional[str] = None) -> str:
        """
        One-shot chat: create session, send message, close session.

        Args:
            prompt: Message to send
            model: Override default model (haiku/sonnet/opus)

        Returns:
            Response text
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Create session
            r = await client.post(
                f"{self.base_url}/api/v1/sessions",
                json={"model": model or self.model, "service": self.service}
            )
            r.raise_for_status()
            session_id = r.json()["session_id"]

            try:
                # Send message
                r = await client.post(
                    f"{self.base_url}/api/v1/sessions/{session_id}/chat",
                    json={"content": prompt}
                )
                r.raise_for_status()
                return r.json()["content"]
            finally:
                # Always close session
                await client.delete(f"{self.base_url}/api/v1/sessions/{session_id}")

    # ---- Persistent Session API ----

    async def create_session(self, model: Optional[str] = None, system_prompt: Optional[str] = None, service: Optional[str] = None) -> str:
        """
        Create persistent session for multi-turn conversation.

        Args:
            model: Model to use (haiku/sonnet/opus)
            system_prompt: Optional system prompt

        Returns:
            Session ID
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)

        payload = {"model": model or self.model}
        if system_prompt:
            payload["system_prompt"] = system_prompt
        if service:
            payload["service"] = service

        r = await self._client.post(f"{self.base_url}/api/v1/sessions", json=payload)
        r.raise_for_status()
        self._session_id = r.json()["session_id"]
        return self._session_id

    async def send(self, prompt: str) -> str:
        """
        Send message to current session.

        Args:
            prompt: Message to send

        Returns:
            Response text

        Raises:
            RuntimeError: If no session created
        """
        if not self._session_id or not self._client:
            raise RuntimeError("No active session. Call create_session() first.")

        r = await self._client.post(
            f"{self.base_url}/api/v1/sessions/{self._session_id}/chat",
            json={"content": prompt}
        )
        r.raise_for_status()
        return r.json()["content"]

    async def close(self) -> None:
        """Close current session and cleanup."""
        if self._client and self._session_id:
            try:
                await self._client.delete(f"{self.base_url}/api/v1/sessions/{self._session_id}")
            except Exception:
                pass
            self._session_id = None

        if self._client:
            await self._client.aclose()
            self._client = None

    # ---- Context Manager ----

    async def __aenter__(self):
        await self.create_session()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ---- Health Check ----

    async def health(self) -> dict:
        """Check API health status."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.base_url}/api/v1/health")
            r.raise_for_status()
            return r.json()


# Convenience function
async def ask(prompt: str, model: str = "haiku") -> str:
    """
    Quick one-shot question.

    Usage:
        from client import ask
        response = await ask("What is 2+2?")
    """
    ai = AIGateway(model=model)
    return await ai.chat(prompt)
