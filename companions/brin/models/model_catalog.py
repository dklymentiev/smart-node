"""Pydantic models for ModelService -- model catalog, user preferences, usage stats."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class ModelServiceType(str, Enum):
    """Service types that use AI models."""
    vision = "vision"
    audio = "audio"
    chat = "chat"
    intent = "intent"
    image_gen = "image_gen"


class ModelEntry(BaseModel):
    """Single model in the catalog."""
    alias: str
    model_id: str
    service: str
    is_free: bool = False
    description: Optional[str] = None


class ModelListResult(BaseModel):
    """Result of listing models."""
    models: list[ModelEntry] = []
    service: Optional[str] = None
    error: Optional[str] = None


class SetUserModelRequest(BaseModel):
    """Request to set user's preferred model."""
    user_id: int
    service: str
    alias_or_model: str


class SetModelResult(BaseModel):
    """Result of setting a user model."""
    success: bool
    service: str
    model_id: str = ""
    message: str = ""
    error: Optional[str] = None


class UserSettingsResult(BaseModel):
    """All model settings for a user."""
    user_id: int
    settings: dict[str, str] = {}
    error: Optional[str] = None


class UsageLogEntry(BaseModel):
    """Single usage log entry."""
    user_id: int
    service: str
    model_id: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    processing_time_sec: float = 0.0


class ServiceUsageStats(BaseModel):
    """Usage stats for one service or model."""
    requests: int = 0
    cost_usd: float = 0.0
    time_sec: float = 0.0


class UsageStatsResult(BaseModel):
    """Aggregated usage statistics."""
    total_requests: int = 0
    total_cost_usd: float = 0.0
    total_time_sec: float = 0.0
    by_service: dict[str, ServiceUsageStats] = {}
    by_model: dict[str, ServiceUsageStats] = {}
    days: int = 30
    error: Optional[str] = None


class AddModelRequest(BaseModel):
    """Request to add a model to the catalog."""
    service: str
    alias: str
    model_id: str
    is_free: bool = False
    description: Optional[str] = None
