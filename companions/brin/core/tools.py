"""ToolsConfig: loads tools.yaml with mtime-based hot reload."""

import logging
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

logger = logging.getLogger("brin.tools")

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "tools.yaml"


class MemoryToolConfig(BaseModel):
    """Memory tool configuration."""
    enabled: bool = False
    mode: str = "off"  # auto-inject | on-demand | off
    min_score: float = 0.3
    limit: int = 5
    max_context_length: int = 500
    timeout: int = 5
    exclude_types: list[str] = []


class STMConfig(BaseModel):
    """Short-term memory configuration."""
    enabled: bool = True
    max_facts: int = 50


class ToolsConfig:
    """Loads and caches tools.yaml, reloads on file change."""

    def __init__(self, config_path: Optional[str] = None):
        self._path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._mtime: float = 0
        self._memory = MemoryToolConfig()
        self._stm = STMConfig()
        self._load()

    def get_memory(self) -> MemoryToolConfig:
        """Get memory tool config (reloads if file changed)."""
        self._reload_if_changed()
        return self._memory

    def get_stm(self) -> STMConfig:
        """Get STM config (reloads if file changed)."""
        self._reload_if_changed()
        return self._stm

    def _reload_if_changed(self):
        if not self._path.exists():
            return
        mtime = self._path.stat().st_mtime
        if mtime != self._mtime:
            self._load()

    def _load(self):
        if not self._path.exists():
            logger.info("tools.yaml not found at %s, using defaults", self._path)
            self._memory = MemoryToolConfig()
            self._stm = STMConfig()
            return

        try:
            self._mtime = self._path.stat().st_mtime
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            tools = raw.get("tools", {})
            mem_cfg = tools.get("memory", {})
            self._memory = MemoryToolConfig(**mem_cfg)
            stm_cfg = tools.get("stm", {})
            self._stm = STMConfig(**stm_cfg)
            logger.info(
                "tools.yaml loaded: memory.mode=%s memory.enabled=%s stm.enabled=%s",
                self._memory.mode, self._memory.enabled, self._stm.enabled,
            )
        except Exception as e:
            logger.error("Failed to load tools.yaml: %s", e)
            self._memory = MemoryToolConfig()
            self._stm = STMConfig()
