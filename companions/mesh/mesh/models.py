#!/usr/bin/env python3
"""
Pydantic models for API requests and responses
"""
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


# ---- Auth context (not a Pydantic model -- internal only) ----

@dataclass
class AuthContext:
    """Resolved authentication context for the current request."""
    workspace: str = "default"           # active workspace
    workspaces: list = dc_field(default_factory=lambda: ["default"])  # all allowed
    is_admin: bool = False

class DocumentCreateRequest(BaseModel):
    """Request model for creating a document"""
    content: str = Field(..., description="Document content", min_length=1)
    tags: Optional[List[str]] = Field(default=None, description="Optional tags for the document")
    filename: Optional[str] = Field(default=None, description="Original filename")
    source: Optional[str] = Field(default=None, description="Source: file, api, chat, import")
    created_at: Optional[datetime] = Field(default=None, description="Original creation date (file mtime)")
    updated_at: Optional[datetime] = Field(default=None, description="Last update date")

class DocumentResponse(BaseModel):
    """Response model for document data"""
    guid: str = Field(..., description="Document GUID in format doc_{8_hex_chars}")
    content: str = Field(..., description="Document content")
    content_hash: Optional[str] = Field(default=None, description="MD5 hash of content")
    filename: Optional[str] = Field(default=None, description="Original filename")
    source: Optional[str] = Field(default=None, description="Source: file, api, chat, import")
    tags: List[str] = Field(..., description="Document tags")
    created_at: datetime = Field(..., description="Document creation timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="Last update timestamp")
    directory: str = Field(..., description="Assigned directory path")
    workspace: str = Field(default="default", description="Workspace ID")
    pinned: bool = Field(default=False, description="Pinned to top of workspace")

class SearchRequest(BaseModel):
    """Request model for semantic search"""
    query: str = Field(..., description="Search query", min_length=1)
    limit: Optional[int] = Field(default=10, description="Maximum number of results", ge=1, le=100)
    tags: Optional[List[str]] = Field(default=None, description="Filter results to documents containing ALL of these tags")
    date_from: Optional[datetime] = Field(default=None, description="Only return documents created on or after this date")
    date_to: Optional[datetime] = Field(default=None, description="Only return documents created on or before this date")
    workspaces: Optional[dict] = Field(default=None, description="Weighted multi-workspace search: {workspace: weight 0.0-1.0}")

class SearchResult(BaseModel):
    """Individual search result"""
    guid: str = Field(..., description="Document GUID")
    content: str = Field(..., description="Document content")
    tags: List[str] = Field(..., description="Document tags")
    created_at: datetime = Field(..., description="Document creation timestamp")
    updated_at: Optional[datetime] = Field(default=None, description="Last update timestamp")
    similarity_score: float = Field(..., description="Similarity score (0-1)")
    directory: str = Field(..., description="Document directory")
    workspace: str = Field(default="default", description="Workspace ID")

class SearchResponse(BaseModel):
    """Response model for search results"""
    results: List[SearchResult] = Field(..., description="Search results")
    total_count: int = Field(..., description="Total number of results")
    query: str = Field(..., description="Original search query")


# Metadata models
class MetadataRequest(BaseModel):
    """Request model for creating/updating metadata"""
    doc_type: str = Field(..., description="Document type: twitter, youtube, github, webpage, etc.")
    metadata: dict = Field(..., description="Arbitrary metadata as JSON object")
    extractor_version: Optional[str] = Field(default=None, description="Version of extractor that produced this")


class MetadataResponse(BaseModel):
    """Response model for metadata"""
    guid: str = Field(..., description="Document GUID")
    doc_type: str = Field(..., description="Document type")
    metadata: dict = Field(..., description="Metadata JSON object")
    extracted_at: datetime = Field(..., description="When metadata was extracted")
    extractor_version: Optional[str] = Field(default=None, description="Extractor version")
