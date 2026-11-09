"""Brin HTTP client with circuit breaker for bot/service consumers."""

import asyncio
import time
from typing import Any, Callable, Coroutine, Optional

import httpx


class CircuitBreaker:
    """Simple three-state circuit breaker: closed -> open -> half_open -> closed."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._state = "closed"
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open" and (time.time() - self._opened_at) >= self.recovery_timeout:
            self._state = "half_open"
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = "open"
            self._opened_at = time.time()

    def allow_request(self) -> bool:
        s = self.state
        return s in ("closed", "half_open")


class BrinClient:
    """Async HTTP client for Brin API with retry + circuit breaker.

    Usage:
        client = BrinClient(api_key="...", base_url="http://localhost:8101")
        data = await client.get("/api/specialists")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8101",
        timeout: float = 60.0,
        retries: int = 1,
        retry_delay: float = 0.5,
        fallback: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.fallback = fallback
        self._headers = {"X-API-Key": api_key}
        self._breaker = CircuitBreaker()

    @property
    def circuit_state(self) -> str:
        return self._breaker.state

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send request with retry and circuit breaker logic."""
        if not self._breaker.allow_request():
            if self.fallback:
                return await self.fallback(method, path, **kwargs)
            raise ConnectionError(
                f"Circuit breaker open for {self.base_url} "
                f"(recovery in {self._breaker.recovery_timeout}s)"
            )

        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(1 + self.retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.request(
                        method, url, headers=self._headers, **kwargs
                    )
                    if resp.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"Server error {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    self._breaker.record_success()
                    return resp
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                self._breaker.record_failure()
                if attempt < self.retries:
                    await asyncio.sleep(self.retry_delay)

        # All retries exhausted
        if self.fallback:
            return await self.fallback(method, path, **kwargs)
        raise last_exc  # type: ignore[misc]

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("POST", path, **kwargs)
