"""MemoryService -- smart search with query cleaning, parallel strategies, httpx to Mesh API."""

import asyncio
import logging
import os
import time
from typing import Optional

import httpx

from models.memory import (
    SmartSearchRequest, MemoryDocument, SmartSearchResult, MemoryContextResult,
)

logger = logging.getLogger(__name__)

MESH_URL = os.environ.get("MESH_API_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = int(os.environ.get("MEMORY_SEARCH_TIMEOUT", "3"))

# -- Query cleaning constants --

SKIP_PATTERNS = [
    "hi", "ok", "yes", "no", "thanks", "bye",
]

TEMPORAL_PATTERNS = [
    "recent", "latest", "yesterday", "today", "this week",
]

CLEANUP_WORDS = {
    "find", "show", "search", "look",
    "all", "me", "my", "which", "what", "how",
    "in", "on", "from", "by", "for", "to",
    "inbox",
    "sent", "please",
}

KEYWORD_MAP = {
    # Non-English platform aliases were removed for the English-only build.
    # English platform words (tiktok, youtube, ...) are matched directly downstream.
}

MIN_QUERY_LENGTH = 5


class MemoryService:
    """Smart memory search via Mesh API with query cleaning and parallel strategies."""

    def __init__(self, mesh_url: str = MESH_URL, timeout: int = TIMEOUT):
        self.mesh_url = mesh_url
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
        # Circuit breaker state
        self._failure_count = 0
        self._failure_threshold = 3
        self._cooldown_seconds = 60
        self._last_failure_time = 0.0
        logger.info(f"MemoryService initialized (mesh={mesh_url}, timeout={timeout}s)")

    async def close(self):
        """Close persistent httpx client."""
        await self._client.aclose()

    # -- Public API --

    def _is_circuit_open(self) -> bool:
        if self._failure_count < self._failure_threshold:
            return False
        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self._cooldown_seconds:
            logger.info("Circuit breaker: cooldown expired, resetting (was %d failures)", self._failure_count)
            self._failure_count = 0
            return False
        return True

    def _record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self._failure_threshold:
            logger.warning("Circuit breaker OPEN: %d consecutive failures, pausing for %ds",
                           self._failure_count, self._cooldown_seconds)

    def _record_success(self):
        if self._failure_count > 0:
            logger.info("Circuit breaker: success after %d failure(s), resetting", self._failure_count)
            self._failure_count = 0

    async def search(self, req: SmartSearchRequest) -> SmartSearchResult:
        query = req.query
        workspace = req.workspace

        # Circuit breaker check
        if self._is_circuit_open():
            logger.debug("Circuit breaker open, skipping memory search")
            return SmartSearchResult(query=query, search_strategy="circuit_open",
                                     error="Memory search temporarily disabled")

        # Skip greetings/short if no structured params
        if not req.scope and not req.kind and not req.platform and self._should_skip(query):
            return SmartSearchResult(query=query, search_strategy="skipped")

        try:
            if req.scope and req.scope != "all":
                docs = await self._search_by_scope(req.scope, req.kind, req.platform, req.limit)
                strategy = "scope"
            elif req.kind or req.platform:
                docs = await self._search_by_filters(req.kind, req.platform, req.limit)
                strategy = "filters"
            else:
                docs = await self._smart_search(query, req.limit, workspace=workspace)
                strategy = "smart"

            return SmartSearchResult(
                documents=docs, total=len(docs),
                query=query, search_strategy=strategy,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Memory search timeout ({self.timeout}s)")
            return SmartSearchResult(query=query, search_strategy="timeout", error="Search timeout")
        except Exception as e:
            logger.error(f"Memory search error: {e}")
            return SmartSearchResult(query=query, search_strategy="error", error=str(e))

    def format_context(self, documents: list[MemoryDocument], max_length: int = 500) -> MemoryContextResult:
        if not documents:
            return MemoryContextResult()

        lines = [
            "[MEMORY CONTEXT]",
            f"Found {len(documents)} relevant document(s) in memory:",
            "",
        ]
        for i, doc in enumerate(documents, 1):
            content = doc.content
            if len(content) > max_length:
                content = content[:max_length] + "..."
            content = " ".join(content.split())

            date = ""
            if doc.created_at and len(doc.created_at) >= 10:
                date = doc.created_at[:10]
            else:
                for tag in doc.tags:
                    if tag.startswith("date:"):
                        date = tag[5:]
                        break

            useful_tags = [t for t in doc.tags if not t.startswith(("guid:", "date:", "agent:"))]
            tags_str = f" [{', '.join(useful_tags[:3])}]" if useful_tags else ""
            date_str = f" ({date})" if date else ""

            lines.append(f"{i}. {content}{tags_str}{date_str}")
            lines.append("")

        lines.append("---")
        lines.append("")

        return MemoryContextResult(context="\n".join(lines), document_count=len(documents))

    # -- Internal search strategies --

    async def _smart_search(self, query: str, limit: int,
                            workspace: Optional[str] = None) -> list[MemoryDocument]:
        clean = self._clean_query(query)
        is_temporal = self._is_temporal_query(query)
        platform_tag = self._detect_platform(query)

        tasks: list[tuple[str, asyncio.Task]] = []
        tasks.append(("semantic", asyncio.create_task(self._search_semantic(clean, limit, workspace=workspace))))

        if is_temporal:
            tasks.append(("recent", asyncio.create_task(self._search_recent(limit))))
        if platform_tag:
            tasks.append(("bytag", asyncio.create_task(self._search_bytag(platform_tag, limit))))
        if any(w in query.lower() for w in ["url", "link"]):
            tasks.append(("urls", asyncio.create_task(self._search_bytag("kind:url", limit))))

        results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

        docs: list[MemoryDocument] = []
        seen: set[str] = set()
        for i, (name, _) in enumerate(tasks):
            result = results[i]
            if isinstance(result, Exception):
                logger.warning(f"Task {name} failed: {result}")
                continue
            for doc in result:
                if doc.guid and doc.guid not in seen:
                    docs.append(doc)
                    seen.add(doc.guid)

        if is_temporal and docs:
            docs.sort(key=lambda d: d.created_at or "", reverse=True)

        return docs

    async def _search_by_scope(self, scope: str, kind: Optional[str],
                                platform: Optional[str], limit: int) -> list[MemoryDocument]:
        scope_tags = {"inbox": "type:inbox", "notes": "type:note", "worklog": "type:worklog"}
        scope_tag = scope_tags.get(scope, f"type:{scope}")

        if platform:
            primary_tag = f"platform:{platform}"
        elif kind:
            primary_tag = f"kind:{kind}"
        else:
            primary_tag = scope_tag

        docs = await self._search_bytag(primary_tag, limit)

        filtered = []
        for doc in docs:
            tags = set(doc.tags)
            if primary_tag != scope_tag and scope_tag not in tags:
                continue
            if kind and primary_tag != f"kind:{kind}" and f"kind:{kind}" not in tags:
                continue
            if platform and primary_tag != f"platform:{platform}" and f"platform:{platform}" not in tags:
                continue
            filtered.append(doc)

        return filtered

    async def _search_by_filters(self, kind: Optional[str],
                                  platform: Optional[str], limit: int) -> list[MemoryDocument]:
        tasks = []
        if kind:
            tasks.append(asyncio.create_task(self._search_bytag(f"kind:{kind}", limit)))
        if platform:
            tasks.append(asyncio.create_task(self._search_bytag(f"platform:{platform}", limit)))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)

        if len(tasks) == 2:
            # Intersect by guid
            sets = []
            all_docs: dict[str, MemoryDocument] = {}
            for result in results:
                if isinstance(result, Exception):
                    sets.append(set())
                    continue
                guids = set()
                for doc in result:
                    guids.add(doc.guid)
                    all_docs[doc.guid] = doc
                sets.append(guids)
            common = sets[0] & sets[1]
            return [all_docs[g] for g in common if g in all_docs]
        else:
            result = results[0]
            if isinstance(result, Exception):
                return []
            return result

    # -- Mesh API calls --

    async def _search_semantic(self, query: str, limit: int,
                               workspace: Optional[str] = None) -> list[MemoryDocument]:
        try:
            payload = {"query": query, "limit": limit}
            headers = {}
            if workspace:
                headers["X-Workspace"] = workspace
                logger.info("Semantic search scoped to workspace=%s", workspace)
            r = await self._client.post(f"{self.mesh_url}/search", json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self._record_failure()
            logger.warning("Semantic search failed: %s (%s)", type(e).__name__, e)
            return []

        self._record_success()
        results = data.get("results", [])
        return [
            MemoryDocument(
                guid=d.get("guid", ""),
                content=d.get("content", ""),
                tags=d.get("tags", []),
                created_at=d.get("created_at", ""),
                similarity_score=d.get("similarity_score", d.get("similarity", 0)),
            )
            for d in results
        ]

    async def _search_bytag(self, tag: str, limit: int) -> list[MemoryDocument]:
        try:
            r = await self._client.get(f"{self.mesh_url}/bytag", params={"tag": tag, "limit": limit})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"Bytag search failed ({tag}): {e}")
            return []

        results = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(results, list):
            results = []
        return [
            MemoryDocument(
                guid=d.get("guid", ""),
                content=d.get("content", ""),
                tags=d.get("tags", []),
                created_at=d.get("created_at", ""),
            )
            for d in results
        ]

    async def _search_recent(self, limit: int) -> list[MemoryDocument]:
        try:
            r = await self._client.get(f"{self.mesh_url}/recent", params={"limit": limit, "type": "worklog"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"Recent search failed: {e}")
            return []

        results = data.get("results", data) if isinstance(data, dict) else data
        if not isinstance(results, list):
            results = []
        return [
            MemoryDocument(
                guid=d.get("guid", ""),
                content=d.get("content", ""),
                tags=d.get("tags", []),
                created_at=d.get("created_at", ""),
            )
            for d in results
        ]

    # -- Query processing --

    def _clean_query(self, query: str) -> str:
        words = query.lower().strip().split()
        cleaned = [w for w in words if w not in CLEANUP_WORDS]
        translated = [KEYWORD_MAP.get(w, w) for w in cleaned]
        result = " ".join(translated)
        return result if result else query

    def _should_skip(self, query: str) -> bool:
        if len(query.strip()) < MIN_QUERY_LENGTH:
            return True
        q = query.lower().strip()
        if q in SKIP_PATTERNS:
            return True
        for p in SKIP_PATTERNS:
            if q.startswith(p + " "):
                return True
        return False

    def _is_temporal_query(self, query: str) -> bool:
        q = query.lower()
        return any(p in q for p in TEMPORAL_PATTERNS)

    def _detect_platform(self, query: str) -> Optional[str]:
        q = query.lower()
        platforms = {
            "tiktok": ["tiktok"],
            "youtube": ["youtube"],
            "instagram": ["instagram"],
            "twitter": ["twitter", "x.com"],
            "telegram": ["telegram"],
        }
        for platform, keywords in platforms.items():
            if any(kw in q for kw in keywords):
                return f"platform:{platform}"
        return None

    def _deduplicate(self, docs: list[MemoryDocument]) -> list[MemoryDocument]:
        seen: set[str] = set()
        result = []
        for doc in docs:
            if doc.guid not in seen:
                result.append(doc)
                seen.add(doc.guid)
        return result
