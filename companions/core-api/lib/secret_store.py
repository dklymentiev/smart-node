"""The node's one secret store: sn_config.secrets under pgcrypto.

Every credential the node holds lives here, encrypted with the node secret key
(a root-only file, never in the database, never logged): the gateway's
provider keys and CLI token (refs `gateway/<provider>-key`), the integrations
agents and companions use (dotted refs such as `replicate.api_key`), the
Google OAuth tokens. Before, these were two unrelated stores, one in Postgres
for the gateway and one sops file for everyone else, and the same key had to
be put into both by hand.

Values travel in two directions only: in through this module, out through
pgp_sym_decrypt in SQL to the caller that holds the key file (core-api here,
the gateway in its own secrets_store.py). A value is never returned by a
listing and never written to the journal. Each replace keeps the previous
value in sn_config.secret_versions for VERSION_RETENTION_DAYS.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable

log = logging.getLogger("core-api.secret-store")

KEY_FILE = os.environ.get("SN_GATEWAY_SECRET_KEY_FILE", "/etc/smart-node/gateway-secret.key")

# One grammar for both spellings in use. Dotted paths are the integrations'
# (`replicate.api_key`, up to six segments); the gateway names its provider
# keys `gateway/<provider>-key` and the CLI token `gateway/claude-cli-token`.
# Neither spelling is renamed: seeds, the AI Gateway page and the acceptance
# checks all name them as they are.
SEGMENT = r"[a-zA-Z_][a-zA-Z0-9_-]{0,40}"
REF_PATTERN = rf"^{SEGMENT}([./]{SEGMENT}){{0,5}}$"
_REF_RE = re.compile(REF_PATTERN)

VERSION_RETENTION_DAYS = 30


class StoreUnavailable(RuntimeError):
    """The key file is missing or empty: nothing can be encrypted or read."""


def valid_ref(ref: str) -> bool:
    return bool(ref) and _REF_RE.fullmatch(ref) is not None


def namespace_of(ref: str) -> str:
    return re.split(r"[./]", ref, 1)[0]


def master_key() -> str:
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            key = f.read().strip()
    except OSError as e:
        raise StoreUnavailable(f"node secret key unreadable at {KEY_FILE}: {e}") from e
    if not key:
        raise StoreUnavailable(f"node secret key empty at {KEY_FILE}")
    return key


# ---------------------------------------------------------------- writes

def set_value(cur, ref: str, value: str, actor: str, *, provider: str | None = None,
              note: str | None = None) -> bool:
    """Create or replace one secret. Returns True when a value was replaced
    (the old one is kept as a version), False when the ref is new. The value
    is encrypted in SQL and never appears in a log or an event."""
    if not valid_ref(ref):
        raise ValueError(f"invalid secret ref: {ref!r}")
    key = master_key()
    cur.execute(
        "INSERT INTO sn_config.secret_versions (ref, value_enc, replaced_by, reason) "
        "SELECT ref, value_enc, %s, 'replaced' FROM sn_config.secrets WHERE ref = %s",
        (actor, ref))
    replaced = cur.rowcount == 1
    cur.execute(
        "INSERT INTO sn_config.secrets (ref, value_enc, namespace, provider, note, created_by, updated_by) "
        "VALUES (%s, pgp_sym_encrypt(%s, %s), %s, %s, %s, %s, %s) "
        "ON CONFLICT (ref) DO UPDATE SET value_enc = EXCLUDED.value_enc, "
        "  provider = COALESCE(EXCLUDED.provider, sn_config.secrets.provider), "
        "  note = COALESCE(%s, sn_config.secrets.note), "
        "  updated_by = EXCLUDED.updated_by, updated_at = now()",
        # note is NOT NULL: a new secret with none gets ''; a replace with
        # none keeps the note it had (the panel sends no note at all).
        (ref, value, key, namespace_of(ref), provider, note or "", actor, actor, note))
    return replaced


def delete_value(cur, ref: str, actor: str) -> bool:
    """Remove one secret, keeping its last value as a version. False when absent."""
    if not valid_ref(ref):
        raise ValueError(f"invalid secret ref: {ref!r}")
    cur.execute(
        "INSERT INTO sn_config.secret_versions (ref, value_enc, replaced_by, reason) "
        "SELECT ref, value_enc, %s, 'deleted' FROM sn_config.secrets WHERE ref = %s",
        (actor, ref))
    cur.execute("DELETE FROM sn_config.secrets WHERE ref = %s", (ref,))
    return cur.rowcount == 1


def prune_versions(cur, days: int = VERSION_RETENTION_DAYS) -> int:
    cur.execute("DELETE FROM sn_config.secret_versions WHERE replaced_at < now() - make_interval(days => %s)",
                (days,))
    return cur.rowcount


# ---------------------------------------------------------------- reads

def get_value(cur, ref: str) -> str | None:
    """The plaintext of one secret, or None when the ref is absent."""
    if not valid_ref(ref):
        raise ValueError(f"invalid secret ref: {ref!r}")
    cur.execute("SELECT pgp_sym_decrypt(value_enc, %s) AS val FROM sn_config.secrets WHERE ref = %s",
                (master_key(), ref))
    row = cur.fetchone()
    if row is None:
        return None
    val = row["val"]
    if isinstance(val, (bytes, bytearray, memoryview)):
        val = bytes(val).decode("utf-8")
    return str(val)


def get_namespace(cur, prefix: str) -> dict[str, str]:
    """Every leaf under a dotted namespace, decrypted: {leaf: value}. For a
    component that holds a small family of its own secrets (google.*)."""
    cur.execute("SELECT ref, pgp_sym_decrypt(value_enc, %s) AS val FROM sn_config.secrets "
                "WHERE ref LIKE %s ORDER BY ref", (master_key(), prefix + ".%"))
    out = {}
    for row in cur.fetchall() or []:
        val = row["val"]
        if isinstance(val, (bytes, bytearray, memoryview)):
            val = bytes(val).decode("utf-8")
        out[row["ref"][len(prefix) + 1:]] = str(val)
    return out


def exists(cur, ref: str) -> bool:
    cur.execute("SELECT 1 FROM sn_config.secrets WHERE ref = %s", (ref,))
    return cur.fetchone() is not None


def list_items(cur) -> list[dict[str, Any]]:
    """Every ref with its metadata and the journal's last set and last read.
    No value is read: the listing never touches the key."""
    cur.execute(
        "SELECT s.ref, s.namespace, s.provider, s.note, s.created_at, s.created_by, "
        "       s.updated_at, s.updated_by, "
        "       r.ts AS last_read_at, r.actor AS last_read_by "
        "FROM sn_config.secrets s "
        "LEFT JOIN LATERAL ("
        "  SELECT ts, actor FROM sn_core.events "
        "  WHERE event_type = 'secret.read' AND entity_type = 'secret' AND entity_id = s.ref "
        "  ORDER BY ts DESC LIMIT 1) r ON true "
        "ORDER BY s.ref")
    out = []
    for row in cur.fetchall() or []:
        out.append({
            "ref": row["ref"], "namespace": row["namespace"], "provider": row["provider"],
            "note": row["note"] or "",
            "created_at": _iso(row["created_at"]), "created_by": row["created_by"],
            "updated_at": _iso(row["updated_at"]), "updated_by": row["updated_by"],
            "last_read_at": _iso(row["last_read_at"]), "last_read_by": row["last_read_by"],
        })
    return out


def list_versions(cur, ref: str) -> list[dict[str, Any]]:
    """When and by whom a ref's earlier values were replaced or deleted. No values."""
    cur.execute("SELECT id, replaced_at, replaced_by, reason FROM sn_config.secret_versions "
                "WHERE ref = %s ORDER BY replaced_at DESC", (ref,))
    return [{"id": r["id"], "ts": int(r["replaced_at"].timestamp()), "replaced_at": _iso(r["replaced_at"]),
             "replaced_by": r["replaced_by"], "reason": r["reason"]} for r in cur.fetchall() or []]


def version_value(cur, ref: str, ts: int) -> str | None:
    """The value a ref had before the replacement stamped `ts` (epoch seconds)."""
    cur.execute("SELECT pgp_sym_decrypt(value_enc, %s) AS val FROM sn_config.secret_versions "
                "WHERE ref = %s AND floor(extract(epoch FROM replaced_at)) = %s "
                "ORDER BY replaced_at DESC LIMIT 1", (master_key(), ref, ts))
    row = cur.fetchone()
    if row is None:
        return None
    val = row["val"]
    if isinstance(val, (bytes, bytearray, memoryview)):
        val = bytes(val).decode("utf-8")
    return str(val)


# ---------------------------------------------------------------- who uses, who reads

def used_by(cur, refs: Iterable[str]) -> dict[str, list[str]]:
    """Where each ref is wired in: a CLI provider's env var (`secret:<ref>` in
    cli_providers.env_vars) or an API provider's key (`gateway/<slug>-key`)."""
    wanted = set(refs)
    out: dict[str, list[str]] = {r: [] for r in wanted}
    cur.execute("SELECT slug, env_vars FROM sn_config.cli_providers")
    for row in cur.fetchall() or []:
        for name, val in (row["env_vars"] or {}).items():
            if isinstance(val, str) and val.startswith("secret:") and val[7:] in wanted:
                out[val[7:]].append(f"cli provider {row['slug']} ({name})")
    cur.execute("SELECT slug FROM sn_config.providers")
    for row in cur.fetchall() or []:
        ref = f"gateway/{row['slug']}-key"
        if ref in wanted:
            out[ref].append(f"provider {row['slug']}")
    return out


def readers(agent_grants: dict[str, tuple[str, list[str]]], refs: Iterable[str]) -> dict[str, list[str]]:
    """Which agents may resolve each ref: those with a secrets read grant and
    the ref, or its namespace, in their secret_refs. `agent_grants` maps slug
    to (mode, rules), as routes.secrets reads grants.json."""
    out: dict[str, list[str]] = {}
    for ref in refs:
        who = []
        for slug, (mode, rules) in sorted(agent_grants.items()):
            if "r" in mode and ref_allowed(ref, rules):
                who.append(slug)
        out[ref] = who
    return out


def ref_allowed(ref: str, allowed: list[str]) -> bool:
    """Exact refs and dotted namespaces ending in .*; no implicit wildcard."""
    for rule in allowed:
        rule = rule.strip()
        if rule == ref:
            return True
        if rule.endswith(".*") and ref.startswith(rule[:-1]):
            return True
    return False


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None
