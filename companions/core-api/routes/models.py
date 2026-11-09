"""Models catalog -- sn_config.models.

The ONE statement of which models an agent may be given: the database
refuses an agent model that is not an enabled chat row here (trigger
agents_model_in_catalog), and the panel offers exactly this list. Never
fabricates -- empty table yields an empty list rather than a hardcoded default.

Writable now, so the configurator can add a provider's model and an agent
on it from a one-line request: look around (GET /providers), ask the owner for
the key if the provider has none, add the model (POST), create the agent. Before
this the table could only be changed with psql, and a model added that way was
still refused by a list hardcoded in the gateway.

Which upstreams exist is NOT stated here. The gateway reports the backends it can
run (GET /api/v1/backends, read from the table its dispatcher uses) and a new row
must name one of them. When the gateway cannot be asked, the write is refused
rather than checked against a guess.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import Principal, get_principal
from db import cursor
from events_emit import emit
from permissions import requires

log = logging.getLogger("core-api.models")

router = APIRouter(prefix="/api/models", tags=["models"])

# Who may change the catalog. The catalog decides what agents can be put on, and
# some upstreams cost money per token, so it is not open to every agent: the owner,
# the operator seat, and an agent whose CAPABILITY is configurator. Capability, not
# slug -- slugs are personal names and were renamed once already (2026-09-19).
_WRITER_ROLES = {"super-admin", "operator"}
_WRITER_CAPABILITIES = {"configurator"}

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_VENDOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_GATEWAY_TIMEOUT = 5.0


class ModelCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    vendor: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    sort_order: int = 100
    is_default: bool = False
    notes: str | None = None


class ModelPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    vendor: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    sort_order: int | None = None
    is_default: bool | None = None
    notes: str | None = None


def _require_writer(principal: Principal) -> Principal:
    if principal.role in _WRITER_ROLES:
        return principal
    with cursor() as cur:
        cur.execute(
            "SELECT capability FROM sn_config.agents WHERE slug = %s AND archived_at IS NULL",
            (principal.username,),
        )
        row = cur.fetchone()
    if row and (row["capability"] or "") in _WRITER_CAPABILITIES:
        return principal
    raise HTTPException(
        status_code=403,
        detail="changing the model catalog is for the owner and the configurator; "
               "ask the configurator agent to do it")


def _gateway_backends() -> list[dict]:
    """The gateway's own report of the upstreams it can run. Raises 502 when it
    cannot be asked: validating against a remembered list is how a second list
    starts, and that is the thing this route exists to remove."""
    gw = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        with urllib.request.urlopen(gw + "/api/v1/backends", timeout=_GATEWAY_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))["backends"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"the AI gateway did not answer which providers it can run ({e}); "
                   "nothing was changed")


def _split_id(model_id: str) -> tuple[str, str]:
    provider, sep, model = model_id.partition("/")
    if not sep or not provider or not model:
        raise HTTPException(status_code=400,
                            detail=f"model id '{model_id}' is not <provider>/<model>")
    return provider, model


def _agents_on(cur, model_id: str) -> list[str]:
    cur.execute(
        "SELECT slug FROM sn_config.agents WHERE model = %s AND archived_at IS NULL ORDER BY slug",
        (model_id,),
    )
    return [r["slug"] for r in cur.fetchall()]


def _row(cur, provider: str, model: str):
    cur.execute(
        "SELECT provider || '/' || model AS id, provider, model, vendor, label, "
        "modality, enabled, is_default, sort_order, notes "
        "FROM sn_config.models WHERE provider = %s AND model = %s",
        (provider, model),
    )
    return cur.fetchone()


@router.get("", dependencies=[Depends(requires("models.read"))])
def list_models(
    modality: str | None = "chat",
    principal: Principal = Depends(get_principal),
):
    """Enabled models, ordered. Filter by modality (default 'chat'); pass empty to list all.

    `id` is what an agent stores, <provider>/<model>: provider is the
    runtime that executes the turn, `vendor` who makes the model. The panel sends
    `id` back unchanged and groups by provider.
    """
    sql = (
        "SELECT provider || '/' || model AS id, provider, model, vendor, label, "
        "modality, is_default, sort_order "
        "FROM sn_config.models WHERE enabled = true"
    )
    params: list = []
    if modality:
        sql += " AND modality = %s"
        params.append(modality)
    sql += " ORDER BY sort_order, label"
    with cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return {"models": rows}


@router.get("/providers", dependencies=[Depends(requires("models.read"))])
def list_providers(principal: Principal = Depends(get_principal)):
    """One call to look around before adding anything: every upstream the gateway
    can run, whether it can run now, the key ref it needs when it cannot, and what
    the catalog and the agents already have on it.

    A provider with status not_configured needs its key stored at key_ref through
    the owner's door (POST /api/gateway/secrets). The key is the owner's to give;
    `missing` says so in words, so the configurator asks instead of hunting.
    """
    backends = _gateway_backends()
    with cursor() as cur:
        cur.execute(
            "SELECT provider, provider || '/' || model AS id, label, enabled "
            "FROM sn_config.models WHERE modality = 'chat' ORDER BY sort_order, label"
        )
        models = cur.fetchall()
        cur.execute(
            "SELECT slug, model FROM sn_config.agents "
            "WHERE archived_at IS NULL AND model <> 'none' ORDER BY slug"
        )
        agents = cur.fetchall()
    out = []
    for b in backends:
        pid = b["id"]
        entry = {
            "id": pid,
            "kind": b.get("kind"),
            "status": b.get("status"),
            "key_ref": b.get("key_ref"),
            "reason": b.get("reason"),
            "models": [{"id": m["id"], "label": m["label"], "enabled": m["enabled"]}
                       for m in models if m["provider"] == pid],
            "agents": [a["slug"] for a in agents if (a["model"] or "").split("/", 1)[0] == pid],
        }
        if b.get("status") != "available":
            if b.get("reason"):
                entry["missing"] = b["reason"]
            elif b.get("key_ref"):
                entry["missing"] = (f"no key stored at {b['key_ref']}: ask the owner for the "
                                    f"{pid} API key; it is stored through POST /api/gateway/secrets")
        out.append(entry)
    return {"providers": out}


@router.post("", status_code=201, dependencies=[Depends(requires("models.write"))])
def create_model(payload: ModelCreate, principal: Principal = Depends(get_principal)):
    """Add a chat model to the catalog. Agents can be put on it at once; the gateway
    reads the catalog per request, so nothing restarts."""
    _require_writer(principal)
    provider = payload.provider.strip()
    model = payload.model.strip()
    vendor = payload.vendor.strip().lower()
    if not _MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail=f"invalid model name '{model}'")
    if not _VENDOR_RE.match(vendor):
        raise HTTPException(status_code=400, detail=f"invalid vendor '{vendor}'")
    known = {b["id"] for b in _gateway_backends()}
    if provider not in known:
        raise HTTPException(
            status_code=400,
            detail=f"the gateway cannot run provider '{provider}'; it runs: "
                   f"{', '.join(sorted(known))}. A model of another vendor goes through "
                   "one of these (GPT, Grok, ... are openrouter/<vendor>/<model>)")
    with cursor(commit=True) as cur:
        if _row(cur, provider, model):
            raise HTTPException(status_code=409, detail=f"{provider}/{model} is already in the catalog")
        if payload.is_default:
            cur.execute("UPDATE sn_config.models SET is_default = false "
                        "WHERE modality = 'chat' AND is_default")
        cur.execute(
            "INSERT INTO sn_config.models (provider, model, vendor, label, modality, "
            "enabled, is_default, sort_order, notes) "
            "VALUES (%s, %s, %s, %s, 'chat', true, %s, %s, %s)",
            (provider, model, vendor, payload.label.strip(), payload.is_default,
             payload.sort_order, payload.notes),
        )
        row = _row(cur, provider, model)
        emit(cur, actor=f"user:{principal.username}", event_type="model.added",
             entity_type="model", entity_id=row["id"],
             payload={"provider": provider, "model": model, "vendor": vendor})
    log.info("model added %s/%s by %s", provider, model, principal.username)
    return row


@router.patch("/{model_id:path}", dependencies=[Depends(requires("models.write"))])
def update_model(model_id: str, payload: ModelPatch, principal: Principal = Depends(get_principal)):
    """Relabel, reorder, make default, enable or disable. Disabling a model an agent
    runs on is refused with the agents named: the agent would fail at its next turn."""
    _require_writer(principal)
    provider, model = _split_id(model_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="nothing to change")
    if "vendor" in changes:
        changes["vendor"] = changes["vendor"].strip().lower()
        if not _VENDOR_RE.match(changes["vendor"]):
            raise HTTPException(status_code=400, detail=f"invalid vendor '{changes['vendor']}'")
    with cursor(commit=True) as cur:
        if not _row(cur, provider, model):
            raise HTTPException(status_code=404, detail=f"{model_id} is not in the catalog")
        if changes.get("enabled") is False:
            users = _agents_on(cur, model_id)
            if users:
                raise HTTPException(
                    status_code=409,
                    detail=f"{model_id} runs agents {', '.join(users)}; move them to "
                           "another model first")
        if changes.get("is_default"):
            cur.execute("UPDATE sn_config.models SET is_default = false "
                        "WHERE modality = 'chat' AND is_default")
        cols = ", ".join(f"{k} = %s" for k in changes)
        cur.execute(
            f"UPDATE sn_config.models SET {cols} WHERE provider = %s AND model = %s",
            (*changes.values(), provider, model),
        )
        row = _row(cur, provider, model)
        emit(cur, actor=f"user:{principal.username}", event_type="model.updated",
             entity_type="model", entity_id=model_id, payload={"changes": changes})
    log.info("model updated %s by %s: %s", model_id, principal.username, sorted(changes))
    return row


@router.delete("/{model_id:path}", status_code=204, dependencies=[Depends(requires("models.write"))])
def delete_model(model_id: str, principal: Principal = Depends(get_principal)):
    """Remove a model from the catalog. Refused while an agent runs on it."""
    _require_writer(principal)
    provider, model = _split_id(model_id)
    with cursor(commit=True) as cur:
        if not _row(cur, provider, model):
            raise HTTPException(status_code=404, detail=f"{model_id} is not in the catalog")
        users = _agents_on(cur, model_id)
        if users:
            raise HTTPException(
                status_code=409,
                detail=f"{model_id} runs agents {', '.join(users)}; move them to "
                       "another model first")
        cur.execute("DELETE FROM sn_config.models WHERE provider = %s AND model = %s",
                    (provider, model))
        emit(cur, actor=f"user:{principal.username}", event_type="model.removed",
             entity_type="model", entity_id=model_id, payload={})
    log.info("model removed %s by %s", model_id, principal.username)
