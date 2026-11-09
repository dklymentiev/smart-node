"""ModelService -- SQLite-backed model catalog, user preferences, usage stats."""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional

from models.model_catalog import (
    ModelEntry, ModelListResult, SetModelResult, UserSettingsResult,
    UsageLogEntry, UsageStatsResult, ServiceUsageStats, AddModelRequest,
)

logger = logging.getLogger(__name__)

# Default DB path relative to project root
_DEFAULT_DB = os.environ.get(
    "MODEL_DB_PATH",
    str(Path(__file__).parent.parent / "_storage" / "models.db"),
)

# Default model per service
SERVICE_DEFAULTS: dict[str, str] = {
    "vision": "nvidia/nemotron-nano-12b-v2-vl:free",
    "audio": "faster-whisper:small",
    "chat": "google/gemini-3-flash-preview",
    "intent": "google/gemini-3-flash-preview",
    "image_gen": "black-forest-labs/FLUX.1-schnell",
}

# Seed catalog -- 20 default models
_SEED_MODELS: list[tuple[str, str, str, bool, str]] = [
    # (service, alias, model_id, is_free, description)
    ("vision", "nemotron-free", "nvidia/nemotron-nano-12b-v2-vl:free", True, "Free Nvidia Nemotron vision model"),
    ("vision", "nemotron", "nvidia/nemotron-nano-12b-v2-vl:free", True, "Alias for nemotron-free"),
    ("vision", "gemini-flash-lite", "google/gemini-2.0-flash-lite", False, "Fast Gemini vision"),
    ("vision", "gemini-flash", "google/gemini-2.0-flash", False, "Gemini Flash vision"),
    ("vision", "gemini-2.5-flash-lite", "google/gemini-2.5-flash-lite", False, "Gemini 2.5 Flash Lite"),
    ("vision", "nemotron-paid", "nvidia/nemotron-nano-12b-v2-vl", False, "Paid Nemotron (no queue)"),
    ("vision", "gemini-2.5-flash", "google/gemini-2.5-flash", False, "Gemini 2.5 Flash"),
    ("vision", "gemini-2.5-pro", "google/gemini-2.5-pro", False, "Gemini 2.5 Pro (best quality)"),
    ("audio", "whisper-local", "faster-whisper:small", True, "Local Whisper (faster-whisper, CPU)"),
    ("audio", "whisper-medium", "faster-whisper:medium", True, "Local Whisper medium model"),
    ("audio", "gemini-audio", "google/gemini-2.0-flash-001", False, "Gemini Audio API"),
    ("chat", "gemini-3-flash", "google/gemini-3-flash-preview", False, "Gemini 3 Flash (default)"),
    ("chat", "gemini-2.5-flash", "google/gemini-2.5-flash", False, "Gemini 2.5 Flash"),
    ("chat", "claude-haiku", "anthropic/claude-3-haiku", False, "Claude 3 Haiku"),
    ("chat", "claude-sonnet", "anthropic/claude-3.5-sonnet", False, "Claude 3.5 Sonnet"),
    ("intent", "gemini-3-flash", "google/gemini-3-flash-preview", False, "Gemini 3 Flash (default for intent)"),
    ("intent", "gemini-2.5-flash", "google/gemini-2.5-flash", False, "Gemini 2.5 Flash"),
    ("image_gen", "flux-schnell", "black-forest-labs/FLUX.1-schnell", False, "Flux Schnell - fast, cheap (~$0.003)"),
    ("image_gen", "flux-dev", "black-forest-labs/FLUX.1-dev", False, "Flux Dev - balanced (~$0.025)"),
    ("image_gen", "flux-pro", "black-forest-labs/FLUX.1-pro", False, "Flux Pro - best quality (~$0.05)"),
]


class ModelService:
    """SQLite-backed model catalog with user preferences and usage stats."""

    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"ModelService initialized (db={self.db_path})")

    # -- DB helpers --

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS available_models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    is_free BOOLEAN DEFAULT 0,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(service, alias)
                );
                CREATE TABLE IF NOT EXISTS user_model_settings (
                    user_id INTEGER NOT NULL,
                    service TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, service)
                );
                CREATE TABLE IF NOT EXISTS model_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    service TEXT,
                    model_id TEXT,
                    tokens_in INTEGER DEFAULT 0,
                    tokens_out INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0,
                    processing_time_sec REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_usage_user ON model_usage(user_id);
                CREATE INDEX IF NOT EXISTS idx_usage_date ON model_usage(created_at);
            """)
            # Seed if empty
            count = conn.execute("SELECT COUNT(*) FROM available_models").fetchone()[0]
            if count == 0:
                for svc, alias, mid, free, desc in _SEED_MODELS:
                    conn.execute(
                        "INSERT OR IGNORE INTO available_models (service,alias,model_id,is_free,description) VALUES (?,?,?,?,?)",
                        (svc, alias, mid, free, desc),
                    )
                conn.commit()
                logger.info(f"Seeded {len(_SEED_MODELS)} default models")
        finally:
            conn.close()

    # -- Sync DB methods (called via run_in_executor) --

    def _list_models_sync(self, service: Optional[str] = None) -> list[ModelEntry]:
        conn = self._conn()
        try:
            if service:
                rows = conn.execute(
                    "SELECT alias,model_id,service,is_free,description FROM available_models WHERE service=? ORDER BY is_free DESC,alias",
                    (service,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT alias,model_id,service,is_free,description FROM available_models ORDER BY service,is_free DESC,alias"
                ).fetchall()
            return [ModelEntry(alias=r["alias"], model_id=r["model_id"], service=r["service"],
                              is_free=bool(r["is_free"]), description=r["description"]) for r in rows]
        finally:
            conn.close()

    def _get_model_by_alias_sync(self, alias: str, service: Optional[str] = None) -> Optional[ModelEntry]:
        conn = self._conn()
        try:
            if service:
                row = conn.execute(
                    "SELECT alias,model_id,service,is_free,description FROM available_models WHERE alias=? AND service=?",
                    (alias.lower(), service),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT alias,model_id,service,is_free,description FROM available_models WHERE alias=?",
                    (alias.lower(),),
                ).fetchone()
            if not row:
                return None
            return ModelEntry(alias=row["alias"], model_id=row["model_id"], service=row["service"],
                              is_free=bool(row["is_free"]), description=row["description"])
        finally:
            conn.close()

    def _add_model_sync(self, req: AddModelRequest) -> bool:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO available_models (service,alias,model_id,is_free,description) VALUES (?,?,?,?,?)",
                (req.service, req.alias.lower(), req.model_id, req.is_free, req.description),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to add model: {e}")
            return False
        finally:
            conn.close()

    def _remove_model_sync(self, alias: str, service: str) -> bool:
        conn = self._conn()
        try:
            cur = conn.execute(
                "DELETE FROM available_models WHERE alias=? AND service=?",
                (alias.lower(), service),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def _get_user_model_sync(self, user_id: int, service: str) -> str:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT model_id FROM user_model_settings WHERE user_id=? AND service=?",
                (user_id, service),
            ).fetchone()
            if row:
                return row["model_id"]
            return SERVICE_DEFAULTS.get(service, "")
        finally:
            conn.close()

    def _set_user_model_sync(self, user_id: int, service: str, alias_or_model: str) -> SetModelResult:
        # Resolve alias -> model_id
        entry = self._get_model_by_alias_sync(alias_or_model, service)
        if entry:
            model_id = entry.model_id
        elif "/" in alias_or_model:
            model_id = alias_or_model
        else:
            entry = self._get_model_by_alias_sync(alias_or_model)
            if entry:
                model_id = entry.model_id
                service = entry.service
            else:
                avail = self._list_models_sync(service)
                aliases = ", ".join(m.alias for m in avail)
                return SetModelResult(
                    success=False, service=service,
                    error=f"Unknown model '{alias_or_model}'. Available for {service}: {aliases}",
                )

        conn = self._conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_model_settings (user_id,service,model_id,updated_at) VALUES (?,?,?,?)",
                (user_id, service, model_id, datetime.utcnow().isoformat()),
            )
            conn.commit()
            return SetModelResult(
                success=True, service=service, model_id=model_id,
                message=f"{service.capitalize()} model: {model_id}",
            )
        except Exception as e:
            return SetModelResult(success=False, service=service, error=str(e))
        finally:
            conn.close()

    def _get_user_settings_sync(self, user_id: int) -> UserSettingsResult:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT service,model_id FROM user_model_settings WHERE user_id=?",
                (user_id,),
            ).fetchall()
            settings = {r["service"]: r["model_id"] for r in rows}
            for svc, default in SERVICE_DEFAULTS.items():
                if svc not in settings:
                    settings[svc] = f"{default} (default)"
            return UserSettingsResult(user_id=user_id, settings=settings)
        finally:
            conn.close()

    def _clear_user_settings_sync(self, user_id: int, service: Optional[str] = None) -> bool:
        conn = self._conn()
        try:
            if service:
                conn.execute(
                    "DELETE FROM user_model_settings WHERE user_id=? AND service=?",
                    (user_id, service),
                )
            else:
                conn.execute("DELETE FROM user_model_settings WHERE user_id=?", (user_id,))
            conn.commit()
            return True
        finally:
            conn.close()

    def _log_usage_sync(self, entry: UsageLogEntry) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO model_usage (user_id,service,model_id,tokens_in,tokens_out,cost_usd,processing_time_sec) VALUES (?,?,?,?,?,?,?)",
                (entry.user_id, entry.service, entry.model_id, entry.tokens_in,
                 entry.tokens_out, entry.cost_usd, entry.processing_time_sec),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to log usage: {e}")
        finally:
            conn.close()

    def _get_usage_stats_sync(self, user_id: Optional[int] = None,
                              service: Optional[str] = None, days: int = 30) -> UsageStatsResult:
        conn = self._conn()
        try:
            where = [f"created_at > datetime('now', '-{days} days')"]
            params: list = []
            if user_id:
                where.append("user_id=?")
                params.append(user_id)
            if service:
                where.append("service=?")
                params.append(service)
            where_sql = " AND ".join(where)

            # Totals
            row = conn.execute(f"""
                SELECT COUNT(*) as cnt, COALESCE(SUM(cost_usd),0) as cost,
                       COALESCE(SUM(processing_time_sec),0) as time
                FROM model_usage WHERE {where_sql}
            """, params).fetchone()

            result = UsageStatsResult(
                total_requests=row["cnt"],
                total_cost_usd=round(row["cost"], 4),
                total_time_sec=round(row["time"], 1),
                days=days,
            )

            # By service
            for r in conn.execute(f"""
                SELECT service, COUNT(*) as cnt, COALESCE(SUM(cost_usd),0) as cost,
                       COALESCE(SUM(processing_time_sec),0) as time
                FROM model_usage WHERE {where_sql} GROUP BY service
            """, params).fetchall():
                result.by_service[r["service"]] = ServiceUsageStats(
                    requests=r["cnt"], cost_usd=round(r["cost"], 4), time_sec=round(r["time"], 1),
                )

            # By model
            for r in conn.execute(f"""
                SELECT model_id, COUNT(*) as cnt, COALESCE(SUM(cost_usd),0) as cost,
                       COALESCE(SUM(processing_time_sec),0) as time
                FROM model_usage WHERE {where_sql} GROUP BY model_id ORDER BY cnt DESC LIMIT 10
            """, params).fetchall():
                result.by_model[r["model_id"]] = ServiceUsageStats(
                    requests=r["cnt"], cost_usd=round(r["cost"], 4), time_sec=round(r["time"], 1),
                )

            return result
        finally:
            conn.close()

    # -- Async public API --

    async def _run(self, func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    async def list_models(self, service: Optional[str] = None) -> ModelListResult:
        models = await self._run(self._list_models_sync, service)
        return ModelListResult(models=models, service=service)

    async def get_model_by_alias(self, alias: str, service: Optional[str] = None) -> Optional[ModelEntry]:
        return await self._run(self._get_model_by_alias_sync, alias, service)

    async def add_model(self, req: AddModelRequest) -> bool:
        return await self._run(self._add_model_sync, req)

    async def remove_model(self, alias: str, service: str) -> bool:
        return await self._run(self._remove_model_sync, alias, service)

    async def get_user_model(self, user_id: int, service: str) -> str:
        return await self._run(self._get_user_model_sync, user_id, service)

    async def set_user_model(self, user_id: int, service: str, alias_or_model: str) -> SetModelResult:
        return await self._run(self._set_user_model_sync, user_id, service, alias_or_model)

    async def get_user_settings(self, user_id: int) -> UserSettingsResult:
        return await self._run(self._get_user_settings_sync, user_id)

    async def clear_user_settings(self, user_id: int, service: Optional[str] = None) -> bool:
        return await self._run(self._clear_user_settings_sync, user_id, service)

    async def log_usage(self, entry: UsageLogEntry) -> None:
        await self._run(self._log_usage_sync, entry)

    async def get_usage_stats(self, user_id: Optional[int] = None,
                              service: Optional[str] = None, days: int = 30) -> UsageStatsResult:
        return await self._run(self._get_usage_stats_sync, user_id, service, days)
