"""Models for system diagnostics service."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class DiagnosticTarget(str, Enum):
    """Supported diagnostic targets."""
    cpu = "cpu"
    memory = "memory"
    disk = "disk"
    docker = "docker"
    fail2ban = "fail2ban"
    general = "general"
    processes = "processes"
    system = "system"


class DiagnosticRequest(BaseModel):
    """Request to run a diagnostic check."""
    target: DiagnosticTarget = DiagnosticTarget.general


class DiagnosticResult(BaseModel):
    """Result of a synchronous diagnostic check."""
    target: str = ""
    output: str = ""
    exit_code: int = 0
    time_sec: float = 0
    error: Optional[str] = None


class AsyncDiagnosticRequest(BaseModel):
    """Request to run a diagnostic check asynchronously."""
    target: DiagnosticTarget = DiagnosticTarget.general


class AsyncDiagnosticResult(BaseModel):
    """Result of starting an async diagnostic task."""
    task_id: str = ""
    target: str = ""
    status: str = "pending"
    error: Optional[str] = None


class DiagnosticTaskStatus(BaseModel):
    """Status of an async diagnostic task."""
    task_id: str = ""
    status: str = "pending"
    target: str = ""
    output: str = ""
    exit_code: int = 0
    time_sec: float = 0
    error: Optional[str] = None
