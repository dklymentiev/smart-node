"""
Simple in-memory rate limiter for AI Gateway

Two tiers:
- Internal (Docker, localhost): high limits for service-to-service traffic
- External: stricter limits for outside access
"""
import os
import time
from collections import defaultdict
import threading

_default_prefixes = "172.,127.,10.,192.168."
INTERNAL_PREFIXES = tuple(p.strip() for p in os.getenv("RATE_LIMIT_INTERNAL_PREFIXES", _default_prefixes).split(",") if p.strip())

EXTERNAL_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))
EXTERNAL_BURST = int(os.getenv("RATE_LIMIT_BURST", "10"))
INTERNAL_RPM = int(os.getenv("RATE_LIMIT_INTERNAL_RPM", "300"))
INTERNAL_BURST = int(os.getenv("RATE_LIMIT_INTERNAL_BURST", "50"))


class RateLimiter:
    """Token bucket rate limiter per IP address with internal/external tiers."""

    def __init__(self):
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def _get_limits(self, ip: str) -> tuple:
        """Return (rpm, burst) based on IP tier."""
        if ip.startswith(INTERNAL_PREFIXES):
            return INTERNAL_RPM, INTERNAL_BURST
        return EXTERNAL_RPM, EXTERNAL_BURST

    def allow(self, key: str) -> bool:
        """Check if request is allowed. Returns True if allowed."""
        rpm, burst = self._get_limits(key)
        now = time.time()
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = {"tokens": burst, "last": now}
            bucket = self._buckets[key]
            # Refill tokens based on elapsed time
            elapsed = now - bucket["last"]
            bucket["tokens"] = min(
                burst,
                bucket["tokens"] + elapsed * (rpm / 60.0)
            )
            bucket["last"] = now

            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                return True
            return False

    def cleanup(self):
        """Remove stale entries (idle > 10 min)."""
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._buckets.items() if now - v["last"] > 600]
            for k in stale:
                del self._buckets[k]


rate_limiter = RateLimiter()
