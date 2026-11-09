"""Pydantic models for the References registry (sn_config.agent_references).

A reference is a stable semantic name (slug) that an agent declares in its
manifest; the resolver looks it up here and injects the target document into
the prompt. API field is `key` (what agents write); it maps to the DB `slug`
column, mirroring the Policies registry.
"""

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


TARGET_KIND = Literal["doci", "papers", "mesh", "url"]
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,60}$")


class ReferenceBase(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    target_kind: TARGET_KIND = "doci"
    target_id: str = Field(min_length=1, max_length=500)
    required: bool = False


class ReferenceCreate(ReferenceBase):
    key: str = Field(min_length=2, max_length=61)

    @field_validator("key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        if not KEY_RE.match(v):
            raise ValueError("key must match ^[a-z][a-z0-9-]{1,60}$")
        return v


class ReferenceUpdate(ReferenceBase):
    pass


class Reference(ReferenceBase):
    id: int
    tenant_id: int
    key: str
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
