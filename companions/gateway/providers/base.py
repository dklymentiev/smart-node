"""
Base Provider Abstract Class
"""
from abc import ABC, abstractmethod
from typing import AsyncIterator, List
import httpx
from models import Message


class BaseProvider(ABC):
    """Abstract base class for AI providers"""

    _client: httpx.AsyncClient = None
    # The provider's name in the record and the door's reader of its API
    # format (model_door.Format). Every model call of the provider passes the
    # door: it is kept live and recorded in sn_core.model_calls like an
    # agent's own call.
    door_name: str = None

    def door_format(self):
        return None

    async def get_client(self) -> httpx.AsyncClient:
        """Get or create shared httpx.AsyncClient with connection pooling."""
        if self._client is None or self._client.is_closed:
            fmt = self.door_format()
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
            transport = None
            if fmt is not None:
                import model_door
                transport = model_door.Transport(self.door_name, fmt,
                                                 httpx.AsyncHTTPTransport(limits=limits))
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                limits=limits,
                transport=transport,
            )
        return self._client

    @staticmethod
    def call_timeout(seconds):
        """Timeout for one completion call.

        A completion is not streamed: nothing arrives until it is whole, so the
        read timeout bounds the entire call. It has to follow the caller's turn
        limit; the shared client's 60 s cut long turns short."""
        if seconds is None:
            return httpx.USE_CLIENT_DEFAULT
        return httpx.Timeout(float(seconds), connect=10.0)

    async def close(self):
        """Close the shared client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @abstractmethod
    async def chat(self, messages: List[Message], model: str = None, max_tokens: int = None) -> dict:
        """
        Send messages and get response

        Args:
            messages: List of conversation messages
            model: Override model for this request (thread-safe)

        Returns:
            dict with 'id', 'content', 'usage'
        """
        pass

    @abstractmethod
    async def chat_stream(self, messages: List[Message], model: str = None) -> AsyncIterator[dict]:
        """
        Send messages and stream response

        Args:
            messages: List of conversation messages
            model: Override model for this request (thread-safe)

        Yields:
            Response chunks
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if provider is healthy

        Returns:
            True if healthy, False otherwise
        """
        pass
