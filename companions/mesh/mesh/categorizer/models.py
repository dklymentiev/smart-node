"""Pydantic models for the categorizer module."""

from typing import List, Optional
from pydantic import BaseModel, Field


class Subcategory(BaseModel):
    id: str = Field(..., description="Slug identifier (e.g. 'docker')")
    name: str = Field(..., description="Display name (e.g. 'Docker')")
    description: str = Field(default="", description="What this subcategory covers")


class Category(BaseModel):
    id: str = Field(..., description="Slug identifier (e.g. 'infrastructure')")
    name: str = Field(..., description="Display name (e.g. 'Infrastructure')")
    description: str = Field(default="", description="What this category covers")
    subcategories: List[Subcategory] = Field(default_factory=list)


class Taxonomy(BaseModel):
    version: int = Field(default=1)
    categories: List[Category] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    category_score: float = 0.0
    subcategory_id: Optional[str] = None
    subcategory_name: Optional[str] = None
    subcategory_score: float = 0.0
    method: str = Field(default="embedding", description="'embedding' or 'llm'")


class BootstrapRequest(BaseModel):
    mode: str = Field(
        default="cluster",
        description="'cluster' = HDBSCAN bottom-up, 'designed' = LLM designs taxonomy top-down",
    )
    use_llm: bool = Field(default=False, description="Use LLM for naming clusters (cluster mode)")
    sample_size: int = Field(default=1000, ge=100, le=10000, description="Max docs to sample")
    min_cluster_size: int = Field(default=5, ge=3, le=50, description="Min docs per cluster")


class BatchRequest(BaseModel):
    limit: int = Field(default=500, ge=1, le=5000, description="Max docs to process")
    force: bool = Field(default=False, description="Re-classify already tagged docs")


class CategoryCreateRequest(BaseModel):
    id: str = Field(..., max_length=64, description="Slug identifier for the category")
    name: str = Field(..., max_length=200, description="Display name")
    description: str = Field(default="", description="What this category covers")
    parent_id: Optional[str] = Field(default=None, description="Parent category ID for subcategories")


class CategoryUpdateRequest(BaseModel):
    name: str = Field(..., max_length=200, description="New display name")
    description: Optional[str] = Field(default=None, description="New description")


class TaxonomyResponse(BaseModel):
    taxonomy: Taxonomy
    stats: dict = Field(default_factory=dict)
    proposals: List[dict] = Field(default_factory=list, description="Pending category proposals")
