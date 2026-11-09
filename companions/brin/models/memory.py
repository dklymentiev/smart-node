"""Pydantic models for MemoryService -- smart search with query cleaning."""

from typing import Optional
from pydantic import BaseModel


class SmartSearchRequest(BaseModel):
    """Request for smart memory search."""
    query: str
    scope: Optional[str] = None
    kind: Optional[str] = None
    platform: Optional[str] = None
    limit: int = 10
    workspace: Optional[str] = None  # Mesh workspace for agent-scoped search


class MemoryDocument(BaseModel):
    """Single document from memory search."""
    guid: str
    content: str = ""
    tags: list[str] = []
    created_at: str = ""
    similarity_score: float = 0.0


class SmartSearchResult(BaseModel):
    """Result of smart memory search."""
    documents: list[MemoryDocument] = []
    total: int = 0
    query: str = ""
    search_strategy: str = ""
    error: Optional[str] = None


class MemoryContextResult(BaseModel):
    """Formatted memory context for prompt injection."""
    context: str = ""
    document_count: int = 0
    error: Optional[str] = None
