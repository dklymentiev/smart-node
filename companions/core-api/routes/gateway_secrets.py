"""Owner-gated write path for the gateway's provider credentials.

Provider config (which providers, models, enabled) lives in integrations /
model_pricing / cli_providers. SECRET VALUES (provider API keys, the claude CLI
OAuth token) live in the node's one secret store (sn_config.secrets,
lib/secret_store.py) under refs `gateway/<provider>-key` and
`gateway/claude-cli-token`, beside every other credential of the node. This
route is the AI Gateway settings page's way in; the Secrets page and
/api/secrets see the same rows. Values are never returned here; the gateway
decrypts what it needs in SQL with the node secret key.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth import Principal, get_principal, require_session
from db import cursor
from events_emit import emit
from permissions import requires
from lib.admin_runner import runner_auth_headers
from lib import secret_store

log = logging.getLogger("core-api.gateway-secrets")

router = APIRouter(prefix="/api/gateway", tags=["gateway"])

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_OWNER_ROLES = {"super-admin"}


def _unavailable(e: Exception) -> HTTPException:
    log.error("secret store unavailable: %s", e)
    return HTTPException(status_code=503, detail="secret store key unavailable")


def _require_owner(request: Request) -> Principal:
    p = get_principal(request, request.headers.get("remote-user"))
    if p.role not in _OWNER_ROLES:
        raise HTTPException(status_code=403, detail="owner role required")
    return p


def _resolve_runner_endpoint() -> str:
    """Resolve the admin-runner HTTP endpoint from the integrations catalog
    (slug=admin-runner). Never hardcoded -- follows whatever the node registered."""
    with cursor() as cur:
        cur.execute(
            "SELECT endpoint FROM sn_config.integrations "
            "WHERE slug = %s AND archived_at IS NULL",
            ("admin-runner",),
        )
        row = cur.fetchone()
    if not row or not row["endpoint"]:
        raise HTTPException(status_code=502, detail="admin-runner integration has no endpoint configured")
    return row["endpoint"].rstrip("/")


def _runner_op(remote_user: str, op: str, args: dict) -> dict:
    """Call admin-runner with core-api proof and the original actor."""
    endpoint = _resolve_runner_endpoint()
    body = json.dumps({"args": args or {}}).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}/operations/{op}",
        data=body,
        headers=runner_auth_headers(remote_user),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=502, detail=f"admin-runner {op} HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"admin-runner unreachable: {e.reason}")


def _store_cli_credential(slug: str, value: str, username: str) -> str:
    """Upsert a CLI provider's credential under the secret ref declared in its
    cli_providers.env_vars (a `secret:<ref>` value -- not hardcoded). Returns the
    ref. Owner gate is the caller's responsibility; value is never logged."""
    with cursor(commit=True) as cur:
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown cli provider '{slug}'")
        refs = [v[len("secret:"):] for v in (row["env_vars"] or {}).values()
                if isinstance(v, str) and v.startswith("secret:")]
        if not refs:
            raise HTTPException(status_code=400, detail="provider declares no secret: ref in env_vars")
        ref = refs[0]
        if not secret_store.valid_ref(ref):
            raise HTTPException(status_code=400, detail="invalid secret ref in env_vars")
        try:
            replaced = secret_store.set_value(cur, ref, value, "user:" + username, provider=slug)
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
        emit(cur, actor="user:" + username, event_type="secret.set",
             entity_type="secret", entity_id=ref,
             payload={"provider": slug, "via": "cli-credential", "replaced": replaced})
    log.info("gateway cli credential set slug=%s ref=%s by=%s", slug, ref, username)
    return ref


def _project_cli_credential(slug: str, username: str, token: str | None = None) -> dict:
    """Project a CLI provider's credential from the DB into the gateway's
    EnvironmentFile (/etc/smart-node/agent-runtime.env) via the admin-runner,
    then the runner restarts the gateway so it picks up the value.

    The DB is the source of truth: the env var NAME and its `secret:` ref are read
    from cli_providers.env_vars, and the token is READ + DECRYPTED from
    the secret store (sn_config.secrets, pgcrypto) with the node master key -- UNLESS a freshly issued
    `token` is passed (the on-store fast path, so a browser Connect projects
    immediately without a decrypt round-trip). core-api never writes the file
    itself; the privileged write lives in the admin-runner. Value is never logged."""
    with cursor() as cur:
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown cli provider '{slug}'")
        env_vars = row["env_vars"] or {}
        target = None
        for name, val in env_vars.items():
            if isinstance(val, str) and val.startswith("secret:"):
                target = (name, val[len("secret:"):])
                break
        if not target:
            raise HTTPException(status_code=400, detail="provider declares no secret: ref in env_vars")
        env_name, ref = target
        if not _ENV_NAME_RE.match(env_name):
            raise HTTPException(status_code=400, detail="invalid env var name in env_vars")
        if not secret_store.valid_ref(ref):
            raise HTTPException(status_code=400, detail="invalid secret ref in env_vars")
        if token is None:
            try:
                token = secret_store.get_value(cur, ref)
            except secret_store.StoreUnavailable as e:
                raise _unavailable(e)
            if not token:
                raise HTTPException(status_code=404, detail=f"no stored credential for ref '{ref}'")
    body = env_name + "=" + token + chr(10)
    result = _runner_op(username, "render_agent_runtime_env", {"body": body})
    log.info("projected cli credential slug=%s ref=%s env=%s by=%s", slug, ref, env_name, username)
    return result


class SecretSet(BaseModel):
    ref: str = Field(min_length=2, max_length=81)
    value: str = Field(min_length=1)
    provider: str | None = None


class SecretRef(BaseModel):
    ref: str = Field(min_length=2, max_length=81)


class SecretValue(BaseModel):
    value: str = Field(min_length=1)


class CliCode(BaseModel):
    code: str = Field(min_length=1)


class CliEnvPatch(BaseModel):
    """Merge provider environment entries; null removes a key.

    Secret values must be stored in the secret store (sn_config.secrets) and referenced as
    ``secret:<ref>``.  Ordinary non-secret tuning values (for example the CLI
    auto-compact safety window) may be stored literally.
    """
    env_vars: dict[str, str | None] = Field(default_factory=dict)


def _require_session_owner(request: Request) -> Principal:
    """Node-wide provider configuration needs a signed-in human owner."""
    return require_session(_require_owner(request))


@router.get("/cli/{slug}/env-vars", response_model=dict, dependencies=[Depends(requires("gateway_secrets.read"))])
def get_cli_env_vars(slug: str, request: Request):
    _require_session_owner(request)
    with cursor() as cur:
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s", (slug,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown cli provider '{slug}'")
    return {"slug": slug, "env_vars": row["env_vars"] or {}}


@router.patch("/cli/{slug}/env-vars", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def patch_cli_env_vars(slug: str, payload: CliEnvPatch, request: Request):
    owner = _require_session_owner(request)
    if not payload.env_vars:
        raise HTTPException(status_code=400, detail="env_vars patch is empty")
    if len(payload.env_vars) > 64:
        raise HTTPException(status_code=400, detail="too many env vars")
    for name, value in payload.env_vars.items():
        if not _ENV_NAME_RE.match(name):
            raise HTTPException(status_code=400, detail=f"invalid env var name '{name}'")
        if value is None:
            continue
        if len(value) > 1024 or "\x00" in value or "\n" in value or "\r" in value:
            raise HTTPException(status_code=400, detail=f"invalid value for '{name}'")
        if value.startswith("secret:") and not secret_store.valid_ref(value[len("secret:"):]):
            raise HTTPException(status_code=400, detail=f"invalid secret ref for '{name}'")

    with cursor(commit=True) as cur:
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s FOR UPDATE", (slug,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown cli provider '{slug}'")
        merged = dict(row["env_vars"] or {})
        removed = []
        changed = []
        for name, value in payload.env_vars.items():
            if value is None:
                if name in merged:
                    removed.append(name)
                    merged.pop(name, None)
            else:
                if merged.get(name) != value:
                    changed.append(name)
                merged[name] = value
        cur.execute(
            "UPDATE sn_config.cli_providers SET env_vars = %s::jsonb WHERE slug = %s",
            (json.dumps(merged), slug),
        )
        # Values are deliberately absent from the event: a caller that ignores
        # the secret: convention must not make the event journal a credential log.
        emit(
            cur,
            actor="user:" + owner.username,
            event_type="config.changed",
            entity_type="cli_provider",
            entity_id=slug,
            payload={"field": "env_vars", "changed_keys": changed, "removed_keys": removed},
        )
    return {"slug": slug, "env_vars": merged}


@router.post("/secrets", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def set_secret(payload: SecretSet, request: Request):
    owner = _require_owner(request)
    if not secret_store.valid_ref(payload.ref):
        raise HTTPException(status_code=400, detail="invalid ref")
    try:
        with cursor(commit=True) as cur:
            replaced = secret_store.set_value(cur, payload.ref, payload.value,
                                              "user:" + owner.username, provider=payload.provider)
            emit(cur, actor="user:" + owner.username, event_type="secret.set",
                 entity_type="secret", entity_id=payload.ref,
                 payload={"provider": payload.provider, "via": "gateway-settings", "replaced": replaced})
    except secret_store.StoreUnavailable as e:
        raise _unavailable(e)
    log.info("gateway secret set ref=%s by=%s", payload.ref, owner.username)
    return {"ref": payload.ref, "stored": True}


@router.post("/secrets/delete", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def delete_secret(payload: SecretRef, request: Request):
    owner = _require_owner(request)
    if not secret_store.valid_ref(payload.ref):
        raise HTTPException(status_code=400, detail="invalid ref")
    with cursor(commit=True) as cur:
        deleted = secret_store.delete_value(cur, payload.ref, "user:" + owner.username)
        if deleted:
            emit(cur, actor="user:" + owner.username, event_type="secret.deleted",
                 entity_type="secret", entity_id=payload.ref, payload={"via": "gateway-settings"})
    log.info("gateway secret delete ref=%s by=%s deleted=%s", payload.ref, owner.username, deleted)
    return {"ref": payload.ref, "deleted": deleted}


class CustomProvider(BaseModel):
    """A person's own model server for Claude Code: any Anthropic-compatible address
    (a router, a local model server, another account). One generic shape, no
    per-vendor code: the address and the key go where the CLI itself reads them."""
    base_url: str = Field(min_length=8, max_length=300)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4096)
    name: str | None = Field(default=None, max_length=40)
    make_default: bool = True
    # which agent runtime talks to it: Claude Code (Anthropic format, a row of its
    # own) or Flint (OpenAI format, through the one flint-cli row, FLINT.md)
    runtime: str = Field(default="claude-code", pattern="^(claude-code|flint)$")


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
# A model as the address names it in its own list: OpenRouter's are
# <vendor>/<model>, so a slash between parts is allowed (not leading, no "..").
_CUSTOM_MODEL_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,199}$")


def _custom_provider_on_flint(payload: "CustomProvider", base: str, host: str, model: str,
                              username: str) -> dict:
    """Point the flint-cli row at the address, in the OpenAI format: FLINT_PROVIDER
    openai, FLINT_BASE_URL <address>/v1 (Flint appends /chat/completions), the key
    under the row's own secret ref. The wrapper requires a key for the provider, so
    an address that needs none gets the word "none", which such an address ignores."""
    ref = "gateway.flint-cli.api_key"
    with cursor(commit=True) as cur:
        cur.execute("SELECT env_vars FROM sn_config.cli_providers WHERE slug = 'flint-cli' FOR UPDATE")
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="Flint is not set up on this node")
        try:
            secret_store.set_value(cur, ref, payload.api_key or "none", "user:" + username,
                                   provider="flint-cli")
        except secret_store.StoreUnavailable as e:
            raise _unavailable(e)
        env = {k: v for k, v in (row["env_vars"] or {}).items()
               if k not in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
        env.update({"FLINT_PROVIDER": "openai", "FLINT_BASE_URL": _openai_root(base),
                    "OPENAI_API_KEY": "secret:" + ref})
        cur.execute("UPDATE sn_config.cli_providers SET env_vars = %s::jsonb, enabled = true "
                    "WHERE slug = 'flint-cli'", (json.dumps(env),))
        cur.execute(
            "INSERT INTO sn_config.models (provider, model, vendor, label, modality, enabled, "
            "is_default, sort_order) VALUES ('flint-cli', %s, 'custom', %s, 'chat', true, false, 0) "
            # the label a person reads: the model and the runtime, never the address
            # (a host name such as llm-router.<internal domain> is no word for anyone)
            "ON CONFLICT (provider, model) DO UPDATE SET label = EXCLUDED.label", (model, f"{model} (Flint)"))
        if payload.make_default:
            cur.execute("UPDATE sn_config.models SET is_default = (provider = 'flint-cli' AND model = %s) "
                        "WHERE modality = 'chat'", (model,))
        emit(cur, actor="user:" + username, event_type="config.changed",
             entity_type="cli_provider", entity_id="flint-cli",
             payload={"field": "custom_provider", "host": host, "model": model,
                      "has_key": bool(payload.api_key), "default": payload.make_default})
    log.info("custom provider on flint -> %s model=%s by=%s", host, model, username)
    return {"slug": "flint-cli", "model": f"flint-cli/{model}"}


# An address that already names an API version (".../v1", ".../v1beta/openai") is
# the API root as it is; a bare one gets "/v1". One rule for every OpenAI-format
# address, so a preset (Google, OpenRouter) needs no code of its own.
_VERSIONED_PATH = re.compile(r"/v\d+[a-z0-9]*(/openai)?$")


def _openai_root(base: str) -> str:
    from urllib.parse import urlsplit
    return base if _VERSIONED_PATH.search(urlsplit(base).path) else base + "/v1"


def _unreachable(e: Exception) -> str:
    """Why an address could not be reached, in words a person reads (not Errno)."""
    import socket
    import ssl
    r = getattr(e, "reason", e)
    if isinstance(r, socket.gaierror):
        return "there is no such address: the name does not resolve"
    if isinstance(r, ConnectionRefusedError):
        return "nothing answers at this address"
    if isinstance(r, (TimeoutError, socket.timeout)) or isinstance(e, TimeoutError):
        return "the address did not answer within a minute"
    if isinstance(r, ssl.SSLError):
        return "the address's certificate is not trusted"
    return "the address does not answer"


def _ask(req, model: str | None = None) -> dict:
    """Send one request to a provider; on failure raise 400 with the reason in plain words."""
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        if detail.lstrip().startswith("<"):   # a web page, not an API answer: its markup says nothing
            detail = ""
        if e.code in (401, 403):
            raise HTTPException(status_code=400, detail="the address did not accept the key")
        if e.code == 404:
            reason = (f"the address has no model '{model}', or this is not its API address" if model
                      else "this is not the address of a model API")
        else:
            reason = f"the address answered HTTP {e.code}"
        raise HTTPException(status_code=400, detail=f"{reason} ({detail.strip()[:160]})" if detail.strip() else reason)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise HTTPException(status_code=400, detail=_unreachable(e))
    except ValueError:
        raise HTTPException(status_code=400, detail="the address answered, but not with JSON: is it the API address?")


def _probe_provider(base: str, model: str, api_key: str | None, fmt: str) -> None:
    """Ask the address one short question in the format its runtime will speak and
    raise 400 with the reason when it does not answer. Nothing is saved before this
    passes: a node whose AI does not answer is not worth entering.
    The address is called directly: the gateway's stateless door does not run Flint,
    which works only as an agent's session."""
    question = [{"role": "user", "content": "Reply with the single word OK."}]
    if fmt == "openai":    # Flint: OpenAI chat completions under the API root
        url, headers = _openai_root(base) + "/chat/completions", {}
        body = {"model": model, "messages": question, "max_tokens": 16}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
    else:                  # Claude Code: the Anthropic messages API at the address
        url, headers = base + "/v1/messages", {"anthropic-version": "2023-06-01"}
        body = {"model": model, "messages": question, "max_tokens": 16}
        if api_key:
            headers["x-api-key"] = api_key
    headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    answer = _ask(req, model)
    if fmt == "openai" and not answer.get("choices"):
        raise HTTPException(status_code=400, detail="the address answered without a reply from the model")
    if fmt != "openai" and not answer.get("content"):
        raise HTTPException(status_code=400, detail="the address answered without a reply from the model")


class ProviderModels(BaseModel):
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=2000)
    runtime: str = Field(default="claude-code", pattern="^(claude-code|flint)$")


@router.post("/cli/models", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def provider_models(payload: ProviderModels, request: Request):
    """The models an address offers for this key, so a person picks one instead of
    knowing its name; a refused key is said here, before anything is saved."""
    from urllib.parse import urlsplit
    _require_session_owner(request)
    base = payload.base_url.strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=400, detail="the address must be an http(s) URL")
    if payload.runtime == "flint":
        url, headers = _openai_root(base) + "/models", {}
        if payload.api_key:
            headers["Authorization"] = "Bearer " + payload.api_key
    else:
        url, headers = base + "/v1/models", {"anthropic-version": "2023-06-01"}
        if payload.api_key:
            headers["x-api-key"] = payload.api_key
    headers["Accept"] = "application/json"
    answer = _ask(urllib.request.Request(url, headers=headers))
    ids = sorted({m.get("id") for m in (answer.get("data") or []) if isinstance(m, dict) and m.get("id")})
    return {"models": ids}


@router.post("/cli", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def add_custom_provider(payload: CustomProvider, request: Request):
    """Add (or update) a custom provider.

    Writes one sn_config.cli_providers row that runs the same Claude Code binary as
    claude-cli, with ANTHROPIC_BASE_URL = the address; the gateway relay already
    sends every call of such a row there (anthropic_relay.route_for). The key, if
    any, goes to the secret store and the row holds only its secret: ref. The model
    joins the catalog under the row's slug, so agents_model_in_catalog accepts
    <slug>/<model> when an agent is put on it. One transaction; values never logged."""
    from urllib.parse import urlsplit

    owner = _require_session_owner(request)
    base = payload.base_url.strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=400, detail="the address must be an http(s) URL")
    model = payload.model.strip()
    if not _CUSTOM_MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail=f"invalid model name '{model}'")
    slug = (payload.name or parts.hostname.split(".")[0]).strip().lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
    if not _SLUG_RE.match(slug) or slug in ("claude-cli", "flint-cli", "openrouter", "gemini"):
        raise HTTPException(status_code=400, detail=f"choose another name than '{slug}'")
    _probe_provider(base, model, payload.api_key, "openai" if payload.runtime == "flint" else "anthropic")
    if payload.runtime == "flint":
        return _custom_provider_on_flint(payload, base, parts.hostname, model, owner.username)
    env = {"ANTHROPIC_BASE_URL": base}
    ref = f"gateway.{slug}.api_key"
    with cursor(commit=True) as cur:
        cur.execute("SELECT binary_path, invocation_args FROM sn_config.cli_providers "
                    "WHERE slug = 'claude-cli'")
        cli = cur.fetchone()
        if cli is None:
            raise HTTPException(status_code=409, detail="Claude Code is not installed on this node")
        if payload.api_key:
            try:
                secret_store.set_value(cur, ref, payload.api_key, "user:" + owner.username, provider=slug)
            except secret_store.StoreUnavailable as e:
                raise _unavailable(e)
            env["ANTHROPIC_API_KEY"] = "secret:" + ref
        cur.execute(
            "INSERT INTO sn_config.cli_providers (slug, binary_path, invocation_args, "
            "supported_models, env_vars, enabled) VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, true) "
            "ON CONFLICT (slug) DO UPDATE SET env_vars = EXCLUDED.env_vars, enabled = true, "
            "supported_models = (SELECT array_agg(DISTINCT m) FROM unnest("
            "sn_config.cli_providers.supported_models || EXCLUDED.supported_models) m)",
            (slug, cli["binary_path"], json.dumps(cli["invocation_args"]), [model], json.dumps(env)),
        )
        cur.execute(
            "INSERT INTO sn_config.models (provider, model, vendor, label, modality, enabled, "
            "is_default, sort_order) VALUES (%s, %s, 'custom', %s, 'chat', true, false, 0) "
            "ON CONFLICT (provider, model) DO UPDATE SET label = EXCLUDED.label",
            (slug, model, f"{model} (Claude Code)"),
        )
        if payload.make_default:
            # new agents start on it; existing ones move through PUT /api/agents/{slug}
            cur.execute("UPDATE sn_config.models SET is_default = (provider = %s AND model = %s) "
                        "WHERE modality = 'chat'", (slug, model))
        emit(cur, actor="user:" + owner.username, event_type="config.changed",
             entity_type="cli_provider", entity_id=slug,
             payload={"field": "custom_provider", "host": parts.hostname, "model": model,
                      "has_key": bool(payload.api_key), "default": payload.make_default})
    log.info("custom provider %s -> %s model=%s by=%s", slug, parts.hostname, model, owner.username)
    return {"slug": slug, "model": f"{slug}/{model}"}


@router.post("/cli/{slug}/credential", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def set_cli_credential(slug: str, payload: SecretValue, request: Request):
    """Store a CLI provider's credential (e.g. the claude OAuth token) under the
    secret ref declared in that provider's cli_providers.env_vars. Owner-gated;
    value never logged. Used by the AI Gateway CLI login flow to persist the token."""
    owner = _require_owner(request)
    ref = _store_cli_credential(slug, payload.value, owner.username)
    return {"slug": slug, "ref": ref, "stored": True}


@router.post("/cli/{slug}/login/start", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def cli_login_start(slug: str, request: Request):
    """Begin the CLI provider's OAuth login via the admin-runner (no LLM in the
    loop -- the OAuth code must never transit chat). Returns {result:{auth_url}}.
    Owner-gated; the runner re-checks the owner role from the forwarded identity."""
    owner = _require_owner(request)
    if slug != "claude-cli":
        raise HTTPException(status_code=400,
                            detail=f"{slug} has no Claude OAuth login; store its provider key instead")
    return _runner_op(owner.username, "claude_login_start", {})


@router.post("/cli/{slug}/login/complete", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def cli_login_complete(slug: str, payload: CliCode, request: Request):
    """Finish CLI login: submit the pasted code to the admin-runner, persist the
    returned token server-side under the provider's declared secret ref, then STRIP
    the token so it never reaches the browser. Returns {result:{ok, stored, ...}}."""
    owner = _require_owner(request)
    if slug != "claude-cli":
        raise HTTPException(status_code=400,
                            detail=f"{slug} has no Claude OAuth login; store its provider key instead")
    resp = _runner_op(owner.username, "claude_login_complete", {"code": payload.code})
    result = resp.get("result") or {}
    token = result.pop("token", None)
    if token:
        try:
            _store_cli_credential(slug, token, owner.username)
            result["stored"] = True
            # Immediately project the token into agent-runtime.env + restart the
            # gateway, so a browser Connect makes the credential live with no manual
            # SSH. Best-effort: a projection failure must not fail the login (the
            # token is stored; the idempotent /project endpoint can re-render it).
            try:
                _project_cli_credential(slug, owner.username, token=token)
                result["projected"] = True
            except Exception as pe:
                log.error("cli login complete: projection failed slug=%s: %s", slug, pe)
                result["projected"] = False
        except Exception as e:  # auth succeeded but storage failed -- report, don't 500
            log.error("cli login complete: token store failed slug=%s: %s", slug, e)
            result["stored"] = False
    else:
        result["stored"] = False
    resp["result"] = result
    return resp


@router.post("/cli/{slug}/project", response_model=dict, dependencies=[Depends(requires("gateway_secrets.write"))])
def project_cli_credential(slug: str, request: Request):
    """Re-render the CLI provider's credential from the store (sn_config.secrets) into
    /etc/smart-node/agent-runtime.env and restart the gateway. Idempotent and
    reads from the store -- no OAuth needed -- which is what makes the projection
    re-runnable and honours 'regenerate from DB'. Owner-gated."""
    owner = _require_owner(request)
    if slug != "claude-cli":
        raise HTTPException(status_code=400,
                            detail=f"{slug} credentials are read from the secret store at spawn; no projection is needed")
    result = _project_cli_credential(slug, owner.username, token=None)
    return {"slug": slug, "projected": True, "runner": result.get("result") or result}
