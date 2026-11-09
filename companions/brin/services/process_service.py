"""ProcessService -- SQLite-backed background process lifecycle management."""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Callable, Coroutine, Optional
from uuid import uuid4

from models.process import (
    ProcessStatus, ProcessInfo, RegisterProcessResult,
    ProcessActionResult, ProcessListResult, ProcessLogEntry,
)

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.environ.get(
    "PROCESS_DB_PATH",
    str(Path(__file__).parent.parent / "_storage" / "processes.db"),
)

_MAX_CONCURRENT = int(os.environ.get("PROCESS_MAX_CONCURRENT", "5"))


class _RuntimeProcess:
    """Runtime state for a process (not persisted)."""
    __slots__ = ("task", "pause_event", "cancel_event")

    def __init__(self):
        self.task: Optional[asyncio.Task] = None
        self.pause_event: asyncio.Event = asyncio.Event()
        self.pause_event.set()  # not paused
        self.cancel_event: asyncio.Event = asyncio.Event()


class ProcessService:
    """SQLite-backed process registry with start/stop/pause/resume."""

    def __init__(self, db_path: str = _DEFAULT_DB, max_concurrent: int = _MAX_CONCURRENT,
                 event_bus=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_concurrent = max_concurrent
        self._runtime: dict[str, _RuntimeProcess] = {}
        self._event_bus = event_bus
        self._init_db()
        self._mark_interrupted_as_failed()
        logger.info(f"ProcessService initialized (db={self.db_path}, max={max_concurrent})")

    # -- DB setup --

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS processes (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL DEFAULT 0,
                    input_data TEXT,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    paused_at TEXT,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS process_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    process_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT,
                    FOREIGN KEY (process_id) REFERENCES processes(id)
                );
                CREATE INDEX IF NOT EXISTS idx_processes_status ON processes(status);
                CREATE INDEX IF NOT EXISTS idx_processes_type ON processes(type);
                CREATE INDEX IF NOT EXISTS idx_processes_created ON processes(created_at);
            """)
        finally:
            conn.close()

    def _mark_interrupted_as_failed(self):
        conn = self._conn()
        try:
            now = datetime.utcnow().isoformat()
            conn.execute(
                "UPDATE processes SET status=?, error=?, completed_at=? WHERE status IN (?,?)",
                (ProcessStatus.failed.value, "Process interrupted by restart", now,
                 ProcessStatus.running.value, ProcessStatus.pending.value),
            )
            conn.commit()
        finally:
            conn.close()

    # -- Internal sync helpers --

    def _save(self, info: ProcessInfo):
        conn = self._conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO processes
                (id,type,name,status,progress,input_data,result,error,created_at,started_at,paused_at,completed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                info.id, info.type, info.name, info.status.value, info.progress,
                json.dumps(info.input_data) if info.input_data else None,
                json.dumps(info.result) if info.result else None,
                info.error, info.created_at, info.started_at, None, info.completed_at,
            ))
            conn.commit()
        finally:
            conn.close()

    def _load(self, process_id: str) -> Optional[ProcessInfo]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM processes WHERE id=?", (process_id,)).fetchone()
            if not row:
                return None
            return self._row_to_info(row)
        finally:
            conn.close()

    def _row_to_info(self, row: sqlite3.Row) -> ProcessInfo:
        return ProcessInfo(
            id=row["id"], type=row["type"], name=row["name"],
            status=ProcessStatus(row["status"]),
            progress=row["progress"] or 0,
            input_data=json.loads(row["input_data"]) if row["input_data"] else None,
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            created_at=row["created_at"], started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def _add_log(self, process_id: str, level: str, message: str):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO process_logs (process_id,timestamp,level,message) VALUES (?,?,?,?)",
                (process_id, datetime.utcnow().isoformat(), level, message),
            )
            conn.commit()
        finally:
            conn.close()

    # -- Async helpers --

    async def _run(self, func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    # -- Public API --

    async def register(self, process_type: str, name: str,
                       input_data: Optional[dict] = None) -> RegisterProcessResult:
        process_id = uuid4().hex[:8]
        now = datetime.utcnow().isoformat()
        info = ProcessInfo(
            id=process_id, type=process_type, name=name,
            status=ProcessStatus.pending, progress=0,
            input_data=input_data, created_at=now,
        )
        await self._run(self._save, info)
        await self._run(self._add_log, process_id, "INFO", f"Process registered: {name}")

        rt = _RuntimeProcess()
        self._runtime[process_id] = rt

        return RegisterProcessResult(process_id=process_id, status=ProcessStatus.pending.value)

    async def start(self, process_id: str,
                    coroutine_func: Callable[..., Coroutine], **kwargs) -> ProcessActionResult:
        info = await self._run(self._load, process_id)
        if not info:
            return ProcessActionResult(process_id=process_id, success=False, error="Not found")

        if info.status not in (ProcessStatus.pending, ProcessStatus.paused):
            return ProcessActionResult(
                process_id=process_id, success=False,
                status=info.status.value, error=f"Cannot start from status {info.status.value}",
            )

        # Check concurrent limit
        running = sum(1 for rt in self._runtime.values() if rt.task and not rt.task.done())
        if running >= self.max_concurrent:
            return ProcessActionResult(
                process_id=process_id, success=False,
                error=f"Max concurrent ({self.max_concurrent}) reached",
            )

        info.status = ProcessStatus.running
        info.started_at = datetime.utcnow().isoformat()
        await self._run(self._save, info)
        await self._run(self._add_log, process_id, "INFO", "Process started")

        rt = self._runtime.get(process_id) or _RuntimeProcess()
        rt.pause_event.set()
        self._runtime[process_id] = rt
        rt.task = asyncio.create_task(self._run_process(process_id, coroutine_func, **kwargs))

        return ProcessActionResult(process_id=process_id, success=True, status=ProcessStatus.running.value)

    async def _run_process(self, process_id: str,
                           coroutine_func: Callable[..., Coroutine], **kwargs):
        rt = self._runtime.get(process_id)
        try:
            result = await coroutine_func(process_id=process_id, registry=self, **kwargs)
            if rt and rt.cancel_event.is_set():
                status = ProcessStatus.cancelled
            else:
                status = ProcessStatus.completed
            info = await self._run(self._load, process_id)
            if info:
                info.status = status
                info.progress = 100 if status == ProcessStatus.completed else info.progress
                info.result = result if isinstance(result, dict) else {"result": result}
                info.completed_at = datetime.utcnow().isoformat()
                await self._run(self._save, info)
            await self._run(self._add_log, process_id, "INFO", f"Process {status.value}")
            if status == ProcessStatus.completed and self._event_bus:
                await self._event_bus.emit(
                    "process.completed",
                    {"process_id": process_id, "name": info.name if info else "",
                     "type": info.type if info else ""},
                    source="process_service",
                )
        except asyncio.CancelledError:
            info = await self._run(self._load, process_id)
            if info:
                info.status = ProcessStatus.cancelled
                info.completed_at = datetime.utcnow().isoformat()
                await self._run(self._save, info)
            await self._run(self._add_log, process_id, "INFO", "Process cancelled")
        except Exception as e:
            info = await self._run(self._load, process_id)
            if info:
                info.status = ProcessStatus.failed
                info.error = str(e)
                info.completed_at = datetime.utcnow().isoformat()
                await self._run(self._save, info)
            await self._run(self._add_log, process_id, "ERROR", f"Process failed: {e}")
            logger.error(f"Process {process_id} failed: {e}", exc_info=True)
            if self._event_bus:
                await self._event_bus.emit(
                    "process.failed",
                    {"process_id": process_id, "name": info.name if info else "",
                     "type": info.type if info else "", "error": str(e)},
                    source="process_service",
                )

    async def stop(self, process_id: str) -> ProcessActionResult:
        info = await self._run(self._load, process_id)
        if not info:
            return ProcessActionResult(process_id=process_id, success=False, error="Not found")
        if info.status != ProcessStatus.running:
            return ProcessActionResult(
                process_id=process_id, success=False,
                status=info.status.value, error="Not running",
            )

        rt = self._runtime.get(process_id)
        if rt:
            rt.cancel_event.set()
            if rt.task and not rt.task.done():
                rt.task.cancel()

        info.status = ProcessStatus.cancelled
        info.completed_at = datetime.utcnow().isoformat()
        await self._run(self._save, info)
        await self._run(self._add_log, process_id, "INFO", "Process stopped by user")
        return ProcessActionResult(process_id=process_id, success=True, status=ProcessStatus.cancelled.value)

    async def pause(self, process_id: str) -> ProcessActionResult:
        info = await self._run(self._load, process_id)
        if not info:
            return ProcessActionResult(process_id=process_id, success=False, error="Not found")
        if info.status != ProcessStatus.running:
            return ProcessActionResult(
                process_id=process_id, success=False,
                status=info.status.value, error="Not running",
            )

        rt = self._runtime.get(process_id)
        if rt:
            rt.pause_event.clear()

        info.status = ProcessStatus.paused
        await self._run(self._save, info)
        await self._run(self._add_log, process_id, "INFO", "Process paused")
        return ProcessActionResult(process_id=process_id, success=True, status=ProcessStatus.paused.value)

    async def resume(self, process_id: str) -> ProcessActionResult:
        info = await self._run(self._load, process_id)
        if not info:
            return ProcessActionResult(process_id=process_id, success=False, error="Not found")
        if info.status != ProcessStatus.paused:
            return ProcessActionResult(
                process_id=process_id, success=False,
                status=info.status.value, error="Not paused",
            )

        rt = self._runtime.get(process_id)
        if rt:
            rt.pause_event.set()

        info.status = ProcessStatus.running
        await self._run(self._save, info)
        await self._run(self._add_log, process_id, "INFO", "Process resumed")
        return ProcessActionResult(process_id=process_id, success=True, status=ProcessStatus.running.value)

    async def get_status(self, process_id: str) -> Optional[ProcessInfo]:
        return await self._run(self._load, process_id)

    async def list_active(self) -> ProcessListResult:
        def _query():
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM processes WHERE status IN (?,?,?) ORDER BY created_at DESC",
                    (ProcessStatus.running.value, ProcessStatus.paused.value, ProcessStatus.pending.value),
                ).fetchall()
                return [self._row_to_info(r) for r in rows]
            finally:
                conn.close()
        procs = await self._run(_query)
        return ProcessListResult(processes=procs, total=len(procs))

    async def list_recent(self, limit: int = 20) -> ProcessListResult:
        def _query():
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM processes ORDER BY created_at DESC LIMIT ?", (limit,),
                ).fetchall()
                return [self._row_to_info(r) for r in rows]
            finally:
                conn.close()
        procs = await self._run(_query)
        return ProcessListResult(processes=procs, total=len(procs))

    async def update_progress(self, process_id: str, progress: float) -> None:
        def _update():
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE processes SET progress=? WHERE id=?",
                    (min(100, max(0, progress)), process_id),
                )
                conn.commit()
            finally:
                conn.close()
        await self._run(_update)

    async def check_pause(self, process_id: str) -> bool:
        rt = self._runtime.get(process_id)
        if not rt:
            return False
        if rt.cancel_event.is_set():
            return False
        if not rt.pause_event.is_set():
            await rt.pause_event.wait()
        return True

    async def cleanup_old(self, days: int = 7) -> int:
        def _cleanup():
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
            conn = self._conn()
            try:
                conn.execute(
                    "DELETE FROM process_logs WHERE process_id IN (SELECT id FROM processes WHERE created_at < ?)",
                    (cutoff,),
                )
                cur = conn.execute("DELETE FROM processes WHERE created_at < ?", (cutoff,))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        deleted = await self._run(_cleanup)
        logger.info(f"Cleanup: deleted {deleted} old processes")
        return deleted

    async def get_logs(self, process_id: str, limit: int = 50) -> list[ProcessLogEntry]:
        def _query():
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT timestamp,level,message FROM process_logs WHERE process_id=? ORDER BY timestamp DESC LIMIT ?",
                    (process_id, limit),
                ).fetchall()
                return [ProcessLogEntry(timestamp=r["timestamp"], level=r["level"], message=r["message"]) for r in rows]
            finally:
                conn.close()
        return await self._run(_query)
