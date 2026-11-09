#!/usr/bin/env python3
"""
Tag Schema — configurable tag taxonomy for Mesh.

Loads schema from mesh.yaml, provides:
- Default tags on save (date, source)
- Tag inference config for neighbor-based auto-tagging
- Validation (advisory)
- Schema endpoint for API/MCP/CLI
"""
import logging
from datetime import date
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).parent / "mesh.yaml"


class TagSchema:

    def __init__(self, config_path: str | Path | None = None):
        self.config: dict = {}
        self.schema: dict = {}
        self.defaults: dict = {}
        self.infer_config: dict = {}
        self._project_key: str | None = None
        self.load(config_path or DEFAULT_CONFIG_PATH)

    def load(self, path: str | Path) -> None:
        path = Path(path)
        if not path.exists():
            logger.warning(f"Tag schema not found: {path}")
            self.config = {}
            self.schema = {}
            self.defaults = {}
            self.infer_config = {}
            return

        with open(path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f) or {}

        self.schema = self.config.get("schema", {})
        self.defaults = self.config.get("defaults", {})
        self.infer_config = self.config.get("auto_infer", {})

        self._project_key = None
        for key, spec in self.schema.items():
            if spec.get("is_project"):
                self._project_key = key
                break

        logger.info(f"Tag schema loaded: {len(self.schema)} prefixes, infer={self.infer_enabled}")

    # ── Properties ──

    @property
    def project_prefix(self) -> str | None:
        if self._project_key:
            return self.schema[self._project_key].get("prefix")
        return None

    @property
    def infer_enabled(self) -> bool:
        return self.infer_config.get("enabled", False)

    @property
    def infer_neighbors(self) -> int:
        return self.infer_config.get("neighbors", 5)

    @property
    def infer_threshold(self) -> float:
        return self.infer_config.get("threshold", 0.80)

    @property
    def infer_min_agreement(self) -> int:
        return self.infer_config.get("min_agreement", 3)

    @property
    def infer_prefixes(self) -> list[str]:
        return [
            spec.get("prefix", f"{key}:")
            for key, spec in self.schema.items()
            if spec.get("auto_infer")
        ]

    # ── Auto-tagging at save time ──

    def apply_defaults(self, tags: list[str] | None, source: str | None = None) -> list[str]:
        """Add date and source tags if not already present."""
        tags = list(tags or [])
        existing_prefixes = {t.split(":")[0] + ":" for t in tags if ":" in t}

        # Auto date
        if self.defaults.get("auto_date"):
            date_prefix = self.schema.get("date", {}).get("prefix", "date:")
            if date_prefix not in existing_prefixes:
                tags.append(f"{date_prefix}{date.today().isoformat()}")

        # Source
        source_prefix = self.schema.get("source", {}).get("prefix", "source:")
        if source_prefix not in existing_prefixes:
            src = source or self.defaults.get("source")
            if src:
                tags.append(f"{source_prefix}{src}")

        return tags

    # ── Validation ──

    def validate(self, tags: list[str]) -> list[str]:
        """Advisory validation. Returns warnings, never blocks."""
        warnings = []
        for tag in tags:
            if ":" not in tag:
                continue
            prefix = tag.split(":")[0] + ":"
            value = tag.split(":", 1)[1]
            for key, spec in self.schema.items():
                if spec.get("prefix") == prefix and spec.get("values"):
                    if value not in spec["values"]:
                        warnings.append(f"'{tag}': '{value}' not in {spec['values']}")
        return warnings

    # ── Schema description ──

    def to_dict(self) -> dict:
        """Schema as dict for GET /schema."""
        prefixes = {}
        for key, spec in self.schema.items():
            info = {
                "prefix": spec.get("prefix", f"{key}:"),
                "description": spec.get("description", key),
            }
            if spec.get("values"):
                info["values"] = spec["values"]
            if spec.get("default"):
                info["default"] = spec["default"]
            if spec.get("is_project"):
                info["is_project"] = True
            if spec.get("auto_infer"):
                info["auto_infer"] = True
            prefixes[key] = info

        return {
            "prefixes": prefixes,
            "defaults": self.defaults,
            "auto_infer": self.infer_config,
            "note": "Custom tags (any prefix:value) are always accepted."
        }
