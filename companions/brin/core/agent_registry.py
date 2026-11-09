"""Agent Registry: maps users to active agents with workspace config.

Tracks which agent is active for each user. Agent = specialist with
workspace metadata (YAML front matter in .md file).
"""

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from core.specialist_loader import SpecialistLoader, Specialist

logger = logging.getLogger("brin.agent_registry")

DB_PATH = os.environ.get(
    "AGENT_REGISTRY_DB",
    os.path.join(os.path.dirname(os.path.dirname(__file__)),
                 "_storage", "agent_registry.db"),
)


@dataclass
class AgentConfig:
    """Resolved agent configuration."""
    name: str               # agent id (stem of .md file)
    workspace: str           # Mesh workspace name
    history_dir: str         # path for JSON session logs
    specialist_id: str       # specialist .md file id
    session_summary: bool    # auto-summarize on session end


class AgentRegistry:
    """Maps users to active agents. Backed by SQLite."""

    def __init__(self, loader: SpecialistLoader, db_path: str = DB_PATH):
        self.loader = loader
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_agents (
                    user_id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    activated_at REAL NOT NULL
                )
            """)
            conn.commit()
        logger.info("AgentRegistry initialized: %s", self.db_path)

    def list_agents(self) -> list[AgentConfig]:
        """List all available agents (specialists with workspace config)."""
        agents = []
        for s in self.loader.list_agents():
            cfg = self._specialist_to_config(s)
            if cfg:
                agents.append(cfg)
        return agents

    def get(self, agent_name: str) -> Optional[AgentConfig]:
        """Get agent config by name. Returns None if not found or not an agent."""
        specialist = self.loader.get(agent_name)
        if not specialist or not specialist.agent_meta.is_agent:
            return None
        return self._specialist_to_config(specialist)

    def get_active(self, user_id: str) -> Optional[AgentConfig]:
        """Get currently active agent for user. Returns None if no agent active."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT agent_name FROM active_agents WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return self.get(row[0])

    def activate(self, user_id: str, agent_name: str) -> Optional[AgentConfig]:
        """Activate an agent for user. Returns config or None if agent not found."""
        config = self.get(agent_name)
        if not config:
            return None

        # Ensure history dir exists
        os.makedirs(config.history_dir, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO active_agents (user_id, agent_name, activated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "agent_name = excluded.agent_name, "
                "activated_at = excluded.activated_at",
                (user_id, agent_name, time.time()),
            )
            conn.commit()

        logger.info("Agent '%s' activated for user '%s'", agent_name, user_id)
        return config

    def deactivate(self, user_id: str) -> bool:
        """Deactivate current agent for user. Returns True if was active."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM active_agents WHERE user_id = ?",
                (user_id,),
            )
            conn.commit()
            deleted = cursor.rowcount > 0

        if deleted:
            logger.info("Agent deactivated for user '%s'", user_id)
        return deleted

    @staticmethod
    def _specialist_to_config(s: Specialist) -> Optional[AgentConfig]:
        """Convert Specialist with agent_meta to AgentConfig."""
        meta = s.agent_meta
        if not meta.is_agent:
            return None
        return AgentConfig(
            name=s.id,
            workspace=meta.workspace,
            history_dir=meta.history_dir or os.path.join(
                os.environ.get("BRIN_DATA_DIR",
                               os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")),
                "spaces", s.id),
            specialist_id=s.id,
            session_summary=meta.session_summary,
        )
