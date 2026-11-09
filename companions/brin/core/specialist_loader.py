"""Loads specialist .md files from /srv/agents/specialists/.

Supports optional YAML front matter for agent workspace configuration:
---
workspace: agent-branding
history_dir: /opt/smart-node/brin/data/spaces/branding
session_summary: true
---
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SPECIALISTS_DIR = "/srv/agents/specialists"


@dataclass
class AgentMeta:
    """Agent workspace metadata parsed from YAML front matter."""
    workspace: Optional[str] = None
    history_dir: Optional[str] = None
    session_summary: bool = False

    @property
    def is_agent(self) -> bool:
        """True if this specialist has agent workspace config."""
        return self.workspace is not None


@dataclass
class Specialist:
    id: str
    title: str
    description: str
    content: str
    file_path: str
    mtime: float
    agent_meta: AgentMeta = field(default_factory=AgentMeta)


class SpecialistLoader:
    """Loads and caches specialist definitions from .md files."""

    def __init__(self, directory: str = SPECIALISTS_DIR):
        self.directory = Path(directory)
        self._cache: dict[str, Specialist] = {}

    def list(self) -> list[Specialist]:
        """Return all available specialists (refreshes cache for changed files)."""
        self._scan()
        return list(self._cache.values())

    def get(self, specialist_id: str) -> Optional[Specialist]:
        """Get a single specialist by ID. Returns None if not found."""
        self._scan()
        return self._cache.get(specialist_id)

    def list_agents(self) -> list[Specialist]:
        """Return only specialists that have agent workspace config."""
        self._scan()
        return [s for s in self._cache.values() if s.agent_meta.is_agent]

    def reload(self):
        """Force full reload of all specialists."""
        self._cache.clear()
        self._scan()

    def _scan(self):
        """Scan directory for .md files, update cache for changed/new files."""
        if not self.directory.exists():
            return

        seen = set()
        for f in sorted(self.directory.iterdir()):
            if not f.suffix == ".md" or f.name == "README.md":
                continue

            specialist_id = f.stem
            seen.add(specialist_id)
            mtime = f.stat().st_mtime

            # Skip if cached and unchanged
            if specialist_id in self._cache and self._cache[specialist_id].mtime == mtime:
                continue

            raw = f.read_text(encoding="utf-8")
            agent_meta, content = self._parse_front_matter(raw)
            title, description = self._parse_header(content)

            self._cache[specialist_id] = Specialist(
                id=specialist_id,
                title=title,
                description=description,
                content=content,
                file_path=str(f),
                mtime=mtime,
                agent_meta=agent_meta,
            )

        # Remove deleted files from cache
        for old_id in list(self._cache.keys()):
            if old_id not in seen:
                del self._cache[old_id]

    @staticmethod
    def _parse_front_matter(raw: str) -> tuple[AgentMeta, str]:
        """Extract YAML front matter and return (meta, content_without_front_matter)."""
        if not raw.startswith("---"):
            return AgentMeta(), raw

        end = raw.find("\n---", 3)
        if end == -1:
            return AgentMeta(), raw

        yaml_block = raw[3:end].strip()
        content = raw[end + 4:].lstrip("\n")

        # Simple key: value parsing (no full YAML dependency)
        meta = AgentMeta()
        for line in yaml_block.split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")

            if key == "workspace":
                meta.workspace = val
            elif key == "history_dir":
                meta.history_dir = val
            elif key == "session_summary":
                meta.session_summary = val.lower() in ("true", "yes", "1")

        return meta, content

    @staticmethod
    def _parse_header(content: str) -> tuple[str, str]:
        """Extract title from first # heading and description from first paragraph."""
        title = "Unknown"
        description = ""

        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()

        # Description: first non-empty line after title that isn't a heading
        lines = content.split("\n")
        past_title = False
        for line in lines:
            stripped = line.strip()
            if not past_title:
                if stripped.startswith("# "):
                    past_title = True
                continue
            if not stripped or stripped.startswith("#"):
                if description:
                    break
                continue
            description += stripped + " "

        return title, description.strip()[:200]
