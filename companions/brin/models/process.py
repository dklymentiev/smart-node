"""Pydantic models for ProcessService -- background process lifecycle."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class ProcessStatus(str, Enum):
    """Process lifecycle status."""
    pending = "pending"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ProcessInfo(BaseModel):
    """Process information (DB-backed)."""
    id: str
    type: str
    name: str
    status: ProcessStatus
    progress: float = 0.0
    input_data: Optional[dict] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class RegisterProcessRequest(BaseModel):
    """Request to register a new process."""
    process_type: str
    name: str
    input_data: Optional[dict] = None


class RegisterProcessResult(BaseModel):
    """Result of process registration."""
    process_id: str = ""
    status: str = ""
    error: Optional[str] = None


class ProcessActionResult(BaseModel):
    """Result of a process action (stop, pause, resume)."""
    process_id: str
    success: bool
    status: str = ""
    error: Optional[str] = None


class ProcessListResult(BaseModel):
    """List of processes."""
    processes: list[ProcessInfo] = []
    total: int = 0
    error: Optional[str] = None


class UpdateProgressRequest(BaseModel):
    """Request to update process progress."""
    progress: float


class ProcessLogEntry(BaseModel):
    """Single process log entry."""
    timestamp: str
    level: str
    message: str
