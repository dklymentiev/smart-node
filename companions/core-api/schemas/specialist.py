"""Pydantic schemas for sn_config.specialists."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, constr

SlugStr = constr(pattern=r"^[a-z][a-z0-9-]{1,60}$", min_length=2, max_length=61)
Archetype = Literal["head", "specialist", "reviewer", "autonomous", "shared"]


class SpecialistCreate(BaseModel):
    slug: SlugStr
    name: str = Field(min_length=1, max_length=200)
    body_md: str = ""
    archetype: Archetype | None = None
    tenant_id: int | None = None


class SpecialistUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    body_md: str | None = None
    archetype: Archetype | None = None
    version_bump: Literal["arch", "minor", "patch"] | None = None
    change_note: str | None = None


class SpecialistRead(BaseModel):
    id: int
    slug: str
    name: str
    body_md: str
    archetype: str | None
    category: str | None
    version: str
    tenant_id: int | None
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    updated_by: str | None
