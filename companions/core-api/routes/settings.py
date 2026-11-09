"""Node settings -- the editable chat base preamble (shared system prompt).

The preamble that wraps every chat agent used to be hardcoded in the studio
(server.js BASE_PROMPT). It now lives in the DB so it can be edited from the UI.
Stored on the default tenant row (single-business node: tenant == node).

  GET /api/settings   read the preamble (open: the studio loads it at boot, no user)
  PUT /api/settings   update the preamble (requires an authenticated principal)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import Principal
from db import cursor
from permissions import requires

log = logging.getLogger("core-api.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    preamble_md: str


@router.get("", dependencies=[Depends(requires("settings.read"))])
def get_settings():
    with cursor() as cur:
        cur.execute("SELECT preamble_md FROM sn_config.tenants WHERE slug = 'default'")
        row = cur.fetchone()
    return {"preamble_md": (row["preamble_md"] if row and row["preamble_md"] is not None else "")}


@router.put("")
def update_settings(payload: SettingsUpdate,
                    principal: Principal = Depends(requires("settings.write"))):
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sn_config.tenants SET preamble_md = %s, updated_at = now() WHERE slug = 'default'",
            (payload.preamble_md,),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "default tenant not found")
    return {"ok": True, "preamble_md": payload.preamble_md}
