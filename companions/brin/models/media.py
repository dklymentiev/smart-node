"""Models for media processing tools (vision, transcribe, generate, download)."""

from typing import Optional
from pydantic import BaseModel


# ── Vision ──

class VisionRequest(BaseModel):
    """Request to analyze an image."""
    image_path: str
    model: Optional[str] = None
    prompt: Optional[str] = None


class VisionResult(BaseModel):
    """Result of image analysis."""
    description: str = ""
    text: str = ""
    model: str = ""
    cost: Optional[float] = None
    time_sec: float = 0
    file_size_kb: float = 0
    error: Optional[str] = None


# ── Transcribe ──

class TranscribeRequest(BaseModel):
    """Request to transcribe audio."""
    audio_path: str
    model: Optional[str] = None


class TranscribeResult(BaseModel):
    """Result of audio transcription."""
    text: str = ""
    model: str = ""
    time_sec: float = 0
    file_size_kb: float = 0
    error: Optional[str] = None


# ── Generate ──

class GenerateRequest(BaseModel):
    """Request to generate an image."""
    prompt: str
    model: Optional[str] = None
    width: int = 1024
    height: int = 1024
    steps: Optional[int] = None


class GenerateResult(BaseModel):
    """Result of image generation."""
    image_path: str = ""
    prompt: str = ""
    model: str = ""
    width: int = 0
    height: int = 0
    steps: int = 0
    time_sec: float = 0
    cost: Optional[float] = None
    error: Optional[str] = None


# ── Download ──

class DownloadRequest(BaseModel):
    """Request to download a video."""
    url: str
    max_duration: int = 600
    max_filesize_mb: int = 100


class DownloadResult(BaseModel):
    """Result of video download."""
    file_path: str = ""
    url: str = ""
    platform: str = ""
    video_id: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[int] = None
    file_size_mb: Optional[float] = None
    error: Optional[str] = None
