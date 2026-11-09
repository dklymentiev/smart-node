"""Pydantic models for the Runtimes registry (sn_config.runtimes)."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


KIND = Literal["claude-code", "codex", "gpt", "gemini", "other"]
STATUS = Literal["available", "not-installed", "broken"]
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


class RuntimeBase(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="", max_length=60)
    description: str = ""
    kind: KIND = "other"
    status: STATUS = "available"
    install_uri: str = ""
    install_path: str = ""
    companion_slug: str | None = None
    metadata: dict = Field(default_factory=dict)


class RuntimeCreate(RuntimeBase):
    slug: str = Field(min_length=2, max_length=41)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must match ^[a-z][a-z0-9-]{1,40}$")
        return v


class RuntimeUpdate(RuntimeBase):
    pass


class Runtime(RuntimeBase):
    id: int
    slug: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
