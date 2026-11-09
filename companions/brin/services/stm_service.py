"""Short-Term Memory (STM) service -- per-session fact storage in Mesh.

One document per session, cached in-memory. Facts are extracted from
<memory_update> blocks in AI responses and merged into existing docs.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger("brin.stm")

MESH_URL = os.environ.get("MESH_API_URL", "http://localhost:8000").rstrip("/")


@dataclass
class STMEntry:
    """Single session's short-term memory."""
    guid: Optional[str] = None
    facts: list[str] = field(default_factory=list)


class ShortTermMemory:
    """Per-session fact storage backed by Mesh.

    Each session gets one Mesh document tagged with session:{key}
    and type:short-term-memory. Facts are bullet points.
    """

    def __init__(self, mesh_url: str = MESH_URL, timeout: int = 3):
        self.mesh_url = mesh_url
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        self._cache: dict[str, STMEntry] = {}
        logger.info("STM initialized (mesh=%s)", mesh_url)

    async def close(self):
        """Close persistent httpx client."""
        await self._client.aclose()

    async def load(self, session_key: str) -> STMEntry:
        """Load STM from cache or fetch from Mesh."""
        cached = self._cache.get(session_key)
        if cached is not None:
            return cached

        try:
            r = await self._client.get(
                f"{self.mesh_url}/bytag",
                params={
                    "tag": f"session:{session_key},type:short-term-memory",
                    "limit": 1,
                },
            )
            if r.status_code != 200:
                entry = STMEntry()
                self._cache[session_key] = entry
                return entry

            data = r.json()
            docs = data if isinstance(data, list) else data.get("results", data.get("documents", []))
            if not docs:
                entry = STMEntry()
                self._cache[session_key] = entry
                return entry

            doc = docs[0]
            facts = self._parse_facts(doc.get("content", ""))
            entry = STMEntry(guid=doc.get("guid"), facts=facts)
            self._cache[session_key] = entry
            return entry

        except Exception as e:
            logger.warning("STM load failed for %s: %s", session_key, e)
            entry = STMEntry()
            self._cache[session_key] = entry
            return entry

    async def update(self, session_key: str, new_facts: list[str]) -> None:
        """Merge new facts into existing STM doc (dedup by exact match)."""
        if not new_facts:
            return

        existing = await self.load(session_key)
        merged = list(existing.facts)
        for fact in new_facts:
            if fact not in merged:
                merged.append(fact)

        # Enforce max facts limit
        if len(merged) > 50:
            merged = merged[-50:]

        content = f"# Short-term Memory: {session_key}\n" + "\n".join(f"- {f}" for f in merged)

        try:
            if existing.guid:
                # Update existing doc
                r = await self._client.patch(
                    f"{self.mesh_url}/{existing.guid}",
                    json={"content": content},
                )
                if r.status_code == 200:
                    self._cache[session_key] = STMEntry(guid=existing.guid, facts=merged)
                    logger.info("STM updated for %s: %d facts (%d new)",
                                session_key, len(merged), len(new_facts))
                else:
                    logger.warning("STM PATCH failed: %d %s", r.status_code, r.text[:200])
            else:
                # Create new doc
                tags = [
                    f"session:{session_key}",
                    "type:short-term-memory",
                    f"date:{date.today().isoformat()}",
                ]
                r = await self._client.put(
                    self.mesh_url,
                    json={"content": content, "tags": tags},
                )
                if r.status_code == 200:
                    data = r.json()
                    guid = data.get("guid", "")
                    self._cache[session_key] = STMEntry(guid=guid, facts=merged)
                    logger.info("STM created for %s: %d facts, guid=%s",
                                session_key, len(merged), guid)
                else:
                    logger.warning("STM PUT failed: %d %s", r.status_code, r.text[:200])

        except Exception as e:
            logger.warning("STM update failed for %s: %s", session_key, e)

    def format_block(self, entry: STMEntry) -> str:
        """Format STM entry as injection block."""
        if not entry.facts:
            return ""
        lines = ["[SHORT-TERM MEMORY]"]
        for fact in entry.facts:
            lines.append(f"- {fact}")
        lines.append("[END SHORT-TERM MEMORY]")
        return "\n".join(lines)

    def invalidate(self, session_key: str) -> None:
        """Clear cache entry for session."""
        self._cache.pop(session_key, None)

    @staticmethod
    def _parse_facts(content: str) -> list[str]:
        """Parse bullet-point facts from Mesh document content."""
        facts = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip bullet prefix
            if line.startswith(("- ", "* ")):
                line = line[2:].strip()
            if line:
                facts.append(line)
        return facts
