"""Cron registry + cron_runs Pydantic models."""

from datetime import datetime
from pydantic import BaseModel, Field


class CronBase(BaseModel):
    # Stable job identity: used as the cron-runner JOB name and the by-job run
    # key. Optional on input -- the route derives 'cron-<id>' when omitted.
    slug: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    schedule: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1)
    script_path: str | None = Field(default=None, max_length=500)
    description: str = Field(default="", max_length=500)
    group: str = Field(default="other", max_length=50)
    log_file: str = Field(default="", max_length=500)
    crontab_user: str = Field(default="root", max_length=50)
    enabled: bool = True
    disabled_flag: str | None = Field(default=None, max_length=500)
    notes: str = ""


class CronCreate(CronBase):
    pass


class CronUpdate(BaseModel):
    slug: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    schedule: str | None = None
    command: str | None = None
    enabled: bool | None = None
    description: str | None = None
    notes: str | None = None


class Cron(CronBase):
    id: int
    created_at: datetime
    updated_at: datetime
    # Whether /etc/cron.d/smart-node-agents already holds this job; None when
    # that file is unreadable. Set by the routes, not stored.
    materialized: bool | None = None
    # Only on the answer to a write: what happens next, so nobody has to ask.
    note: str | None = None


class CronRun(BaseModel):
    id: int
    cron_id: int
    started_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output_snippet: str = ""
    created_at: datetime


class CronRunCreate(BaseModel):
    started_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output_snippet: str = ""


class CronRunFinish(BaseModel):
    # Close an open (BUSY) cron_runs row. started_at/finished_at are set
    # server-side (now()) so the wrapper need only report the outcome.
    exit_code: int | None = None
    duration_ms: int | None = None
    output_snippet: str = ""
