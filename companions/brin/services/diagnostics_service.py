"""
Diagnostics Service -- runs system diagnostic scripts via subprocess.

Scripts: scripts/diagnostics/diagnose-system.sh, process-list.sh, system-info.sh.
Supports sync (blocking) and async (background task) execution.
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Optional

from models.diagnostics import (
    DiagnosticTarget,
    DiagnosticRequest,
    DiagnosticResult,
    AsyncDiagnosticRequest,
    AsyncDiagnosticResult,
    DiagnosticTaskStatus,
)

logger = logging.getLogger(__name__)

DIAG_SCRIPT_DIR = os.environ.get(
    "DIAG_SCRIPT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "diagnostics"),
)
DIAG_SYNC_TIMEOUT = int(os.environ.get("DIAG_SYNC_TIMEOUT", "30"))
DIAG_ASYNC_TIMEOUT = int(os.environ.get("DIAG_ASYNC_TIMEOUT", "120"))
DIAG_TASK_TTL = int(os.environ.get("DIAG_TASK_TTL", "3600"))


class DiagnosticsService:
    """Delegates to brin-client-tg diagnostic scripts via subprocess."""

    def __init__(self):
        self._tasks: dict[str, DiagnosticTaskStatus] = {}
        self._task_timestamps: dict[str, float] = {}

    def _build_command(self, target: DiagnosticTarget) -> list[str]:
        """Build shell command for given diagnostic target."""
        if target == DiagnosticTarget.processes:
            return [os.path.join(DIAG_SCRIPT_DIR, "process-list.sh")]
        if target == DiagnosticTarget.system:
            return [os.path.join(DIAG_SCRIPT_DIR, "system-info.sh")]
        # cpu, memory, disk, docker, fail2ban, general -> diagnose-system.sh <target>
        return [os.path.join(DIAG_SCRIPT_DIR, "diagnose-system.sh"), target.value]

    async def run_diagnostic(self, req: DiagnosticRequest) -> DiagnosticResult:
        """Run diagnostic synchronously with timeout."""
        target = req.target
        cmd = self._build_command(target)

        logger.info(
            f"Running diagnostic: {target.value}",
            extra={"action": "diag_run", "status": "start"},
        )

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DIAG_SYNC_TIMEOUT
            )
            elapsed = round(time.monotonic() - start, 2)
            output = stdout.decode(errors="replace")

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
                logger.error(
                    f"Diagnostic {target.value} failed: {err_msg}",
                    extra={"action": "diag_run", "status": "error"},
                )
                return DiagnosticResult(
                    target=target.value,
                    output=output,
                    exit_code=proc.returncode,
                    time_sec=elapsed,
                    error=err_msg[:500],
                )

            logger.info(
                f"Diagnostic {target.value} completed",
                extra={"action": "diag_run", "status": "ok"},
            )
            return DiagnosticResult(
                target=target.value,
                output=output,
                exit_code=0,
                time_sec=elapsed,
            )

        except asyncio.TimeoutError:
            elapsed = round(time.monotonic() - start, 2)
            logger.error(
                f"Diagnostic {target.value} timed out after {DIAG_SYNC_TIMEOUT}s",
                extra={"action": "diag_run", "status": "timeout"},
            )
            return DiagnosticResult(
                target=target.value,
                exit_code=-1,
                time_sec=elapsed,
                error=f"timed out after {DIAG_SYNC_TIMEOUT}s",
            )
        except Exception as e:
            elapsed = round(time.monotonic() - start, 2)
            logger.error(
                f"Diagnostic {target.value} error: {e}",
                extra={"action": "diag_run", "status": "error"},
            )
            return DiagnosticResult(
                target=target.value,
                exit_code=-1,
                time_sec=elapsed,
                error=str(e)[:300],
            )

    async def run_diagnostic_async(
        self, req: AsyncDiagnosticRequest
    ) -> AsyncDiagnosticResult:
        """Start diagnostic in background, return task_id immediately."""
        self._cleanup_expired()

        task_id = f"diag-{uuid.uuid4().hex[:8]}"
        target = req.target

        self._tasks[task_id] = DiagnosticTaskStatus(
            task_id=task_id,
            status="running",
            target=target.value,
        )
        self._task_timestamps[task_id] = time.monotonic()

        asyncio.create_task(self._run_background(task_id, target))

        return AsyncDiagnosticResult(
            task_id=task_id,
            target=target.value,
            status="running",
        )

    async def get_task_status(self, task_id: str) -> DiagnosticTaskStatus:
        """Get status of an async diagnostic task."""
        if task_id not in self._tasks:
            return DiagnosticTaskStatus(
                task_id=task_id,
                status="not_found",
                error="Task not found",
            )
        return self._tasks[task_id]

    async def _run_background(
        self, task_id: str, target: DiagnosticTarget
    ) -> None:
        """Background worker for async diagnostics."""
        cmd = self._build_command(target)
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=DIAG_ASYNC_TIMEOUT
            )
            elapsed = round(time.monotonic() - start, 2)
            output = stdout.decode(errors="replace")

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
                self._tasks[task_id] = DiagnosticTaskStatus(
                    task_id=task_id,
                    status="failed",
                    target=target.value,
                    output=output,
                    exit_code=proc.returncode,
                    time_sec=elapsed,
                    error=err_msg[:500],
                )
            else:
                self._tasks[task_id] = DiagnosticTaskStatus(
                    task_id=task_id,
                    status="completed",
                    target=target.value,
                    output=output,
                    exit_code=0,
                    time_sec=elapsed,
                )

        except asyncio.TimeoutError:
            elapsed = round(time.monotonic() - start, 2)
            self._tasks[task_id] = DiagnosticTaskStatus(
                task_id=task_id,
                status="failed",
                target=target.value,
                exit_code=-1,
                time_sec=elapsed,
                error=f"timed out after {DIAG_ASYNC_TIMEOUT}s",
            )
        except Exception as e:
            elapsed = round(time.monotonic() - start, 2)
            self._tasks[task_id] = DiagnosticTaskStatus(
                task_id=task_id,
                status="failed",
                target=target.value,
                exit_code=-1,
                time_sec=elapsed,
                error=str(e)[:300],
            )

    def _cleanup_expired(self) -> None:
        """Remove tasks older than TTL."""
        now = time.monotonic()
        expired = [
            tid
            for tid, ts in self._task_timestamps.items()
            if now - ts > DIAG_TASK_TTL
        ]
        for tid in expired:
            self._tasks.pop(tid, None)
            self._task_timestamps.pop(tid, None)
