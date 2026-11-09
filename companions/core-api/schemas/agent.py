"""Pydantic schemas for sn_config.agents."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, constr

SlugStr = constr(pattern=r"^[a-z][a-z0-9-]{1,40}$", min_length=2, max_length=41)
# Not an enumeration: the models an agent may be given are the enabled chat
# rows of sn_config.models, and the database refuses anything else (trigger
# agents_model_in_catalog). Listing ids here again is how the old list drifted.
# The shape is <runtime>/<model>: claude-cli/sonnet, openrouter/openai/gpt-5.
Model = constr(min_length=1, max_length=200)


class AgentCreate(BaseModel):
    slug: SlugStr
    title: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    description: str | None = None
    model: Model = "claude-cli/sonnet"
    enabled: bool = True

    # Runtime / OS mapping.
    os_user: str | None = Field(default=None, max_length=64)
    schedule: str | None = Field(default=None, max_length=120)  # cron expression
    os_groups: list[str] = Field(default_factory=list)
    mesh_workspace: str | None = Field(default=None, max_length=64)  # own memory

    # Filesystem access (OS-enforced on the node; stored + rendered here).
    fs_read: list[str] = Field(default_factory=list)
    fs_write: list[str] = Field(default_factory=list)
    forbidden_fs: list[str] = Field(default_factory=list)
    # Honor-system permissions injected into the prompt.
    forbidden_behavior: list[str] = Field(default_factory=list)

    # Specialist: either reference an existing one by slug, or inline-create with body.
    specialist_slug: str | None = None
    specialist_body: str | None = None  # if specialist_slug is None, this creates a new specialist with slug=agent.slug

    # immutable product-role marker, set at creation, never
    # changed. Examples: 'configurator', 'developer', 'sysadmin', 'secretary',
    # 'pm', 'architect', 'finance'. The owner does not choose this; it comes
    # from the product template. An empty string is accepted for backward
    # compatibility with callers that predate this field.
    capability: str = Field(default='', max_length=64)

    tenant_id: int | None = None
    department_id: int | None = None


class AgentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = None
    description: str | None = None
    model: Model | None = None
    enabled: bool | None = None
    os_user: str | None = Field(default=None, max_length=64)
    schedule: str | None = Field(default=None, max_length=120)
    os_groups: list[str] | None = None
    mesh_workspace: str | None = Field(default=None, max_length=64)
    fs_read: list[str] | None = None
    fs_write: list[str] | None = None
    forbidden_fs: list[str] | None = None
    forbidden_behavior: list[str] | None = None
    policy_categories: list[Literal["safety", "workflow", "communication", "data-handling"]] | None = None
    department_id: int | None = None
    specialist_body: str | None = None  # rewrites linked specialist's body + CLAUDE.md
    # A key under Studio's /avatars store (crew/x.webp, custom/x.webp). Without
    # this field pydantic dropped the Studio picker's PUT and every avatar was lost
    # "" or null clears it: the picker's Remove sends "".
    avatar: str | None = Field(default=None, max_length=512)
    change_note: str | None = None
    # capability is NOT in AgentUpdate: it is immutable after creation.
    # Any PUT that includes capability is silently ignored at the route level.


class AgentRead(BaseModel):
    id: int
    slug: str
    title: str
    role: str | None
    description: str | None
    specialist_id: int | None
    specialist_slug: str | None = None  # joined from specialists table
    model: str
    # Who executes the agent's turns: the first segment of model,
    # and its kind -- 'cli' when that runtime is an enabled row of
    # sn_config.cli_providers (it runs its own opaque tool loop), 'http' otherwise
    # (brin owns the loop), None when the agent has no model. Derived here from
    # the registry so no reader decides the mode from the shape of the string.
    runtime: str | None = None
    runtime_kind: Literal["cli", "http"] | None = None
    enabled: bool
    version: str
    os_user: str | None = None
    schedule: str | None = None
    os_groups: list[str] = Field(default_factory=list)
    mesh_workspace: str | None = None
    # The agent's picture, as a path under Studio's /avatars store
    # (e.g. "crew/alex.webp"). Read by voice-ui for its picker and by the Studio
    # crew list; both already expected it on this model.
    avatar: str | None = None
    fs_read: list[str] = Field(default_factory=list)
    fs_write: list[str] = Field(default_factory=list)
    forbidden_fs: list[str] = Field(default_factory=list)
    forbidden_behavior: list[str] = Field(default_factory=list)
    policy_categories: list[str] = Field(default_factory=list)
    tenant_id: int | None
    department_id: int | None
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    updated_by: str | None
    capability: str = ''  # immutable product-role marker
    specialist_body: str | None = None  # raw specialist role text (specialists.body_md); for the Instructions editor
    body_md: str | None = None  # populated only when ?body=true (COMPOSED CLAUDE.md: role + tool contracts + policies)
