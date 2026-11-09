"""Resolve `secret:<ref>` values from the node's one secret store
(sn_config.secrets, pgcrypto).

The gateway is the consumer of provider credentials (API keys, the claude CLI
OAuth token). Config (cli_providers.env_vars) holds `secret:<ref>` references;
the plaintext lives encrypted in sn_config.secrets and is decrypted in SQL
with the node secret key read from a root-only key file. The key is NEVER
stored in the DB and never logged.

Every load of a key is journalled as secret.read by component:gateway: once
when a value is first used by this process and again whenever it changes
(a rotation), not on every cached use. The same event the API writes for a
person or an agent, so the Secrets page's "last read" means the same thing
for every reader.
"""
import hashlib
import json
import logging
import os
import time

import db

logger = logging.getLogger("gateway.secrets_store")

_KEY_FILE = os.environ.get("SN_GATEWAY_SECRET_KEY_FILE", "/etc/smart-node/gateway-secret.key")
_PREFIX = "secret:"

# Short TTL cache for provider keys so a key changed in AI Gateway settings takes
# effect within seconds without a gateway restart, without a DB hit per request.
_KEY_CACHE = {}
_KEY_TTL = 30.0
# ref -> digest of the value last journalled as read by this process.
_SEEN = {}


def _master_key() -> str:
    with open(_KEY_FILE, "r", encoding="utf-8") as f:
        key = f.read().strip()
    if not key:
        raise RuntimeError(f"node secret key is empty at {_KEY_FILE}")
    return key


def _journal_read(ref: str, value: str) -> None:
    """secret.read for this ref, once per distinct value this process sees."""
    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
    if _SEEN.get(ref) == digest:
        return
    _SEEN[ref] = digest
    try:
        db.query(
            "INSERT INTO sn_core.actors (id, display_name, type, layer) "
            "VALUES ('component:gateway', 'component:gateway', 'component', 'system') "
            "ON CONFLICT (id) DO NOTHING")
        db.query(
            "INSERT INTO sn_core.events (actor, event_type, entity_type, entity_id, payload) "
            "VALUES ('component:gateway', 'secret.read', 'secret', %s, %s::jsonb)",
            (ref, json.dumps({"how": "gateway"})))
    except Exception as e:  # noqa: BLE001 -- the key is in use either way
        logger.warning("secret read not journalled for %s: %s", ref, e)


def resolve_value(value):
    """Return value unchanged unless it is a `secret:<ref>`, in which case decrypt
    it from the store. Raises if the referenced ref is absent (a real
    misconfig -- no silent fallback)."""
    if not isinstance(value, str) or not value.startswith(_PREFIX):
        return value
    ref = value[len(_PREFIX):]
    rows = db.query(
        "SELECT pgp_sym_decrypt(value_enc, %s) AS val "
        "FROM sn_config.secrets WHERE ref = %s",
        (_master_key(), ref), fetch=True, as_dict=True,
    )
    if not rows:
        raise RuntimeError(f"secret ref not found in the store: {ref}")
    val = rows[0]["val"]
    if isinstance(val, (bytes, bytearray, memoryview)):
        val = bytes(val).decode("utf-8")
    _journal_read(ref, val)
    return val


def resolve_env(env_vars: dict) -> dict:
    """Resolve every `secret:` value in an env_vars dict to plaintext."""
    return {k: resolve_value(v) for k, v in (env_vars or {}).items()}


def get_provider_key(provider_id: str):
    """Return the stored API key for an LLM provider (ref gateway/<id>-key), or
    None if no key is stored. Cached for _KEY_TTL seconds. The store is the source
    of truth -- there is no env fallback."""
    ref = f"gateway/{provider_id}-key"
    now = time.monotonic()
    cached = _KEY_CACHE.get(ref)
    if cached and cached[1] > now:
        return cached[0]
    try:
        val = resolve_value(_PREFIX + ref)
    except RuntimeError:
        val = None  # not stored -> provider unconfigured (caller fails loud)
    _KEY_CACHE[ref] = (val, now + _KEY_TTL)
    return val
