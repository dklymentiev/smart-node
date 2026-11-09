"""Pydantic models for Policies registry (sn_config.policies).

Policies are tagged rules injected into agent prompts at runtime. Agents
declare which categories they need; the resolver (downstream task) stitches
all enabled policies in those categories into the system message.

Per-agent overrides live in sn_config.agent_policy_opts (include / exclude /
override) -- not modelled here; this schema is the base registry only.
"""

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


CATEGORY = Literal["safety", "workflow", "communication", "data-handling"]
SEVERITY = Literal["info", "warn", "block"]
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,60}$")


class PolicyBase(BaseModel):
    category: CATEGORY
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1)
    severity: SEVERITY = "warn"
    enabled: bool = True


class PolicyCreate(PolicyBase):
    slug: str = Field(min_length=2, max_length=61)
    tenant_id: Optional[int] = 1   # single-tenant default

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must match ^[a-z][a-z0-9-]{1,60}$")
        return v


class PolicyUpdate(PolicyBase):
    pass


class Policy(PolicyCreate):
    id: int
    version: str
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
