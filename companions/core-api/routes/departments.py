"""Departments registry -- read-only (sn_config.departments).

Feeds the agent wizard's Department picker. Write-side (create/edit
departments, interactions, teams) is a separate phase; this exposes the
catalog so an agent can be assigned to an existing department.
"""

from fastapi import APIRouter, Depends

from db import cursor
from permissions import requires

router = APIRouter(prefix="/api/departments", tags=["departments"])


@router.get("", dependencies=[Depends(requires("departments.read"))])
def list_departments():
    with cursor() as cur:
        cur.execute(
            "SELECT id, slug, name, type_slug, head_agent_id "
            "FROM sn_config.departments "
            "WHERE archived_at IS NULL ORDER BY name"
        )
        rows = cur.fetchall()
    return {"departments": rows}
