"""
Shadow Cache for AI Gateway
Tracks duplicate requests without actually caching responses.
Marks cache_hit in logs to show potential savings.
"""

import hashlib
import time
import threading
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


class ShadowCache:
    """In-memory shadow cache that tracks seen requests"""

    def __init__(self, ttl: int = 3600, max_size: int = 10000):
        self._cache: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self.ttl = ttl
        self.max_size = max_size
        self._hits = 0
        self._misses = 0

    def _make_key(self, model: str, system_prompt: str, message: str) -> str:
        raw = f"{model}|{system_prompt}|{message}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def check(self, model: str, system_prompt: Optional[str], message: str) -> bool:
        """Return True if this exact request was seen before (within TTL)"""
        key = self._make_key(model, system_prompt or "", message)
        with self._lock:
            entry = self._cache.get(key)
            if entry and (time.time() - entry["ts"]) < self.ttl:
                self._hits += 1
                return True
            self._misses += 1
            return False

    def store(self, model: str, system_prompt: Optional[str], message: str,
              tokens: Optional[dict] = None):
        """Store request in shadow cache"""
        key = self._make_key(model, system_prompt or "", message)
        with self._lock:
            if len(self._cache) >= self.max_size:
                oldest_key = min(self._cache, key=lambda k: self._cache[k]["ts"])
                del self._cache[oldest_key]
            self._cache[key] = {
                "tokens": tokens,
                "ts": time.time(),
            }

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            active = sum(1 for v in self._cache.values() if (now - v["ts"]) < self.ttl)
            return {
                "entries": len(self._cache),
                "active": active,
                "max_size": self.max_size,
                "ttl": self.ttl,
                "hits": self._hits,
                "misses": self._misses,
            }


shadow_cache = ShadowCache()
