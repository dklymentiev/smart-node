"""The owner's ChatGPT plan as the model for Flint agents (Sign in with ChatGPT).

The protocol is chatgpt_oauth.py; this is where it meets the node. The owner signs in in their
own browser, which ends on a loopback page that cannot load (127.0.0.1 is the owner's computer),
and pastes that page's address back. Tokens are kept in the node's one secret store and are never
returned to a browser. The gateway asks for a short-lived access token here (POST /chatgpt/token,
a node service, not a person) and this is the only place that refreshes: the refresh token changes
with every refresh, so one writer under one lock keeps it from being spent twice.

Connecting points the Flint runtime row (sn_config.cli_providers, flint-cli) at the plan:
SN_FLINT_UPSTREAM=chatgpt tells the gateway's OpenAI door where its calls go; the row's earlier
settings are kept and put back on disconnect. Like every provider choice on this node it applies to
all agents on that runtime.

Only an open-source or self-hosted node signed in by its own owner belongs here
(developers.openai.com/siwc/token-sharing-open-source). One token is never shared between people.
"""
import json
import logging
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import chatgpt_oauth as oauth
from db import cursor
from events_emit import emit
from permissions import requires
from lib import secret_store
from routes.gateway_secrets import _require_session_owner, _unavailable

log = logging.getLogger("core-api.gateway-chatgpt")

router = APIRouter(prefix="/api/gateway/chatgpt", tags=["gateway"])

CRED = "gateway.chatgpt.credentials"
HOST = "gateway.chatgpt.host_id"
PENDING = "gateway.chatgpt.pending"
PREVIOUS = "gateway.chatgpt.previous_flint_env"
VENDOR = "chatgpt"                      # sn_config.models.vendor of the rows this adds
UPSTREAM_KEY = "SN_FLINT_UPSTREAM"
_KEYS_OFF = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FLINT_BASE_URL")


class Landing(BaseModel):
    url: str = Field(..., min_length=1, max_length=4000)


def _get_json(cur, ref):
    raw = secret_store.get_value(cur, ref)
    return json.loads(raw) if raw else None


def _put(cur, ref, value, username):
    secret_store.set_value(cur, ref, value if isinstance(value, str) else json.dumps(value),
                           "user:" + username, provider="chatgpt")


def _models(access_token: str) -> list:
    """The models the plan offers this account, in the server's order (visibility "list")."""
    req = urllib.request.Request(oauth.RESOURCE + "/models", headers={"Authorization": "Bearer " + access_token,
                                                                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise HTTPException(status_code=502, detail="signed in, but the model list could not be read: " + str(e)[:100])
    return [m["slug"] for m in data.get("models", []) if m.get("visibility") == "list" and m.get("slug")]


@router.post("/login/start", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def login_start(request: Request):
    """The address to open in the browser. A second sign-in on a node that is already registered
    reuses its registration and hints the account, so OpenAI skips the account chooser."""
    owner = _require_session_owner(request)
    with cursor(commit=True) as cur:
        try:
            host = secret_store.get_value(cur, HOST)
            if not host:
                host = oauth.new_host_id()
                _put(cur, HOST, host, owner.username)
            cred = _get_json(cur, CRED) or {}
            url, pending = oauth.start(host, client_id=cred.get("client_id"), id_token_hint=cred.get("id_token"),
                                       login_hint=cred.get("email"))
            _put(cur, PENDING, pending, owner.username)
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
        emit(cur, actor="user:" + owner.username, event_type="config.changed", entity_type="cli_provider",
             entity_id="flint-cli", payload={"field": "chatgpt_login", "step": "started"})
    return {"auth_url": url}


@router.post("/login/complete", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def login_complete(payload: Landing, request: Request):
    """Finish the sign-in with the address the browser ended on. Returns who signed in and the
    models offered; never a token."""
    owner = _require_session_owner(request)
    with cursor() as cur:
        try:
            pending = _get_json(cur, PENDING)
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
    if not pending:
        raise HTTPException(status_code=409, detail="start the sign-in first")
    try:
        landing = oauth.read_landing(payload.url, pending)
    except oauth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        tok = oauth.exchange(landing["code"], landing["client_id"], pending["verifier"])
    except oauth.AuthError as e:
        _forget_attempt(owner.username)
        raise HTTPException(status_code=400, detail=str(e))
    _forget_attempt(owner.username)             # a code is used once, whatever came of it
    scope_text = tok.get("scope") or landing["scope"]
    try:
        if not oauth.has_plan_scope(scope_text):
            raise oauth.AuthError("this ChatGPT account did not allow ChatGPT plan use; sign in again and "
                                  "agree to it, or check that the plan includes it")
        if not tok.get("id_token"):
            raise oauth.AuthError("OpenAI did not return who signed in")
        ident = oauth.verify_id_token(tok["id_token"], landing["client_id"], pending["nonce"])
    except oauth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cred = oauth.credential(tok, ident, landing["client_id"], pending["host_id"], scope_text)
    models = _models(cred["access_token"])
    with cursor(commit=True) as cur:
        try:
            _put(cur, CRED, cred, owner.username)
            _point_flint_at_plan(cur, owner.username, models)
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
        emit(cur, actor="user:" + owner.username, event_type="config.changed", entity_type="cli_provider",
             entity_id="flint-cli", payload={"field": "chatgpt_login", "step": "connected", "models": len(models)})
    log.info("chatgpt plan connected by=%s models=%d", owner.username, len(models))
    return {"ok": True, "email": ident.get("email"), "models": models}


def _forget_attempt(username: str) -> None:
    with cursor(commit=True) as cur:
        secret_store.delete_value(cur, PENDING, "user:" + username)


def _point_flint_at_plan(cur, username: str, models: list) -> None:
    cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = 'flint-cli' FOR UPDATE")
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="Flint is not set up on this node; the ChatGPT plan is used by Flint agents")
    env = dict(row["env_vars"] or {})
    if env.get(UPSTREAM_KEY) != "chatgpt":          # keep what the row held before, once
        _put(cur, PREVIOUS, env, username)
    env = {k: v for k, v in env.items() if k not in _KEYS_OFF}
    # Flint speaks the OpenAI format to the gateway's door; the door sends the call to the plan.
    # The wrapper wants a key for the provider, so it gets the word "none", which nothing sends.
    env.update({"FLINT_PROVIDER": "openai", "OPENAI_API_KEY": "none", UPSTREAM_KEY: "chatgpt"})
    cur.execute("UPDATE sn_config.cli_providers SET env_vars = %s::jsonb, enabled = true WHERE slug = 'flint-cli'",
                (json.dumps(env),))
    cur.execute("DELETE FROM sn_config.models WHERE provider = 'flint-cli' AND vendor = %s", (VENDOR,))
    for m in models:
        cur.execute("INSERT INTO sn_config.models (provider, model, vendor, label, modality, enabled, is_default, sort_order) "
                    "VALUES ('flint-cli', %s, %s, %s, 'chat', true, false, 0) "
                    "ON CONFLICT (provider, model) DO UPDATE SET label = EXCLUDED.label, vendor = EXCLUDED.vendor",
                    (m, VENDOR, f"{m} (ChatGPT plan)"))


@router.get("", response_model=dict, dependencies=[Depends(requires("gateway_secrets.read"))])
def status(request: Request):
    _require_session_owner(request)
    with cursor() as cur:
        try:
            cred = _get_json(cur, CRED)
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = 'flint-cli'")
        row = cur.fetchone()
        cur.execute("SELECT model FROM sn_config.models WHERE provider = 'flint-cli' AND vendor = %s ORDER BY model", (VENDOR,))
        models = [r["model"] for r in cur.fetchall()]
    return {"signed_in": bool(cred), "email": (cred or {}).get("email"), "models": models,
            "plan_use": bool(cred) and oauth.PLAN_SCOPE in (cred.get("scopes") or []),
            "flint_uses_it": bool(row) and (row["env_vars"] or {}).get(UPSTREAM_KEY) == "chatgpt"}


@router.delete("", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def disconnect(request: Request):
    """Forget the sign-in and put Flint's provider row back as it was."""
    owner = _require_session_owner(request)
    actor = "user:" + owner.username
    with cursor(commit=True) as cur:
        try:
            previous = _get_json(cur, PREVIOUS)
            had = secret_store.delete_value(cur, CRED, actor)
            secret_store.delete_value(cur, PENDING, actor)
            secret_store.delete_value(cur, PREVIOUS, actor)
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = 'flint-cli' FOR UPDATE")
        row = cur.fetchone()
        if row is not None and (row["env_vars"] or {}).get(UPSTREAM_KEY) == "chatgpt":
            env = previous if isinstance(previous, dict) else {k: v for k, v in (row["env_vars"] or {}).items()
                                                               if k not in (UPSTREAM_KEY, "OPENAI_API_KEY")}
            cur.execute("UPDATE sn_config.cli_providers SET env_vars = %s::jsonb WHERE slug = 'flint-cli'", (json.dumps(env),))
        cur.execute("DELETE FROM sn_config.models WHERE provider = 'flint-cli' AND vendor = %s", (VENDOR,))
        emit(cur, actor=actor, event_type="config.changed", entity_type="cli_provider", entity_id="flint-cli",
             payload={"field": "chatgpt_login", "step": "disconnected"})
    return {"ok": True, "was_signed_in": had}


@router.post("/token", response_model=dict, dependencies=[Depends(requires("gateway.chatgpt.token"))])
def token():
    """A fresh access token for the gateway. Refreshes under one lock when the saved one is about to
    end and saves the replacement refresh token in the same step."""
    with cursor(commit=True) as cur:
        try:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('gateway.chatgpt.token'))")
            cred = _get_json(cur, CRED)
            if not cred:
                raise HTTPException(status_code=404, detail="the ChatGPT plan is not connected on this node")
            if not oauth.access_is_fresh(cred):
                try:
                    cred = oauth.after_refresh(cred, oauth.refresh(cred["client_id"], cred["refresh_token"]))
                except oauth.AuthError as e:
                    raise HTTPException(status_code=401, detail=str(e))
                _put(cur, CRED, cred, "service:gateway")
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
    return {"access_token": cred["access_token"], "expires_at": int(cred["saved_at"]) + int(cred["expires_in"])}
