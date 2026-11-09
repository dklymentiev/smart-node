"""Profile projector: materialize ``sn_config.tenant_profile`` into the live
target tables, idempotently, in FK-safe topological order, with a reverse
reconcile pass that tears down params removed from the profile.

Step 2 (this module) is the SKELETON: tier orchestration, per-tier advisory
lock + transaction, ``projection_ledger`` idempotency, event emission, and the
reverse pass. The per-behavior WRITE/TEARDOWN logic is stubbed (see
``BEHAVIOR_WRITERS`` / ``BEHAVIOR_TEARDOWNS``) and filled in Step 3.

Projection contract:
- Projection runs T0..T7 in FK order; each tier is one transaction under a
  transaction-scoped Postgres advisory lock (auto-released on commit) so the
  projector never races the live runtime on the same tables.
- Idempotency via ``projection_ledger``: config-sync / stateful-merge update in
  place by target PK; seed-once / policy-versioned decide create-vs-skip by
  ``natural_key`` verified against the live row (Step 3); secret-write-once
  never rewrites an existing secret.
- The reverse pass diffs ``projection_ledger`` param_keys against the current
  profile; a key present last run but absent now is torn down per its recorded
  behavior, and its ledger rows flip to ``result='retired'``.
- Every row emits ``param.projected|failed|skipped|retired`` to ``sn_core.events``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass

import db
from events_emit import emit

log = logging.getLogger("core-api.projector")

ACTOR = "projector"

# Advisory-lock namespace (arbitrary but stable). Two-int form: (NS, tier_index).
# Transaction-scoped -> released automatically when the tier's txn commits.
_LOCK_NS = 0x5250  # "RP"
_RECONCILE_LOCK_KEY = 99

BEHAVIORS = (
    "config-sync",
    "stateful-merge",
    "seed-once",
    "policy-versioned",
    "secret-write-once",
)

# FK-safe tiers T0..T7. Each entry: (tier_id, [target tables handled in the tier]).
# A profile row is assigned to the tier that owns its target table. Intra-tier
# order for rows that FK each other is handled inside the tier's writer (Step 3).
TIERS: list[tuple[str, list[str]]] = [
    ("T0", ["tenants", "tenant_brand", "aliases", "refs"]),
    ("T1", ["users", "roles", "user_roles"]),
    ("T2", ["departments"]),
    ("T3", ["integrations"]),                          # + secret creds via behavior
    ("T4", ["policies", "budgets"]),
    ("T5", ["agents", "specialists"]),
    ("T6", ["agent_policy_opts", "department_policies"]),
    ("T7", ["flows", "cron"]),                          # capabilities / Solutions
]

# target table -> tier index, for assigning each profile row to its tier.
_TABLE_TIER: dict[str, int] = {
    table: idx for idx, (_tid, tables) in enumerate(TIERS) for table in tables
}


@dataclass
class Outcome:
    param_key: str
    behavior: str
    target_table: str | None
    target_pk: str | None
    natural_key: str | None
    content_hash: str | None
    result: str            # projected | failed | skipped | retired
    detail: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _target_table(row: dict) -> str | None:
    """Derive the target table from a profile row's ``target`` descriptor
    ("table.field" / policy slug / solution id). Skeleton uses the token before
    the first dot; Step 3 refines per behavior."""
    target = row.get("target")
    if not target:
        return None
    return target.split(".", 1)[0]


def _resolve_tenant(cur) -> str | None:
    """Single-tenant-per-node: resolve the one tenant id once (RFC T0)."""
    cur.execute("SELECT id FROM sn_config.tenants ORDER BY id LIMIT 1")
    row = cur.fetchone()
    return str(row["id"]) if row else None


def _load_tier_rows(cur, tables: list[str]) -> list[dict]:
    cur.execute(
        "SELECT * FROM sn_config.tenant_profile WHERE status <> 'retired'"
    )
    rows = cur.fetchall()
    return [r for r in rows if _target_table(r) in tables]


def _write_ledger(cur, o: Outcome) -> None:
    cur.execute(
        """
        INSERT INTO sn_config.projection_ledger
            (param_key, behavior, target_table, target_pk, natural_key,
             content_hash, projected_at, result)
        VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
        ON CONFLICT ON CONSTRAINT projection_ledger_uniq
        DO UPDATE SET behavior     = EXCLUDED.behavior,
                      natural_key  = EXCLUDED.natural_key,
                      content_hash = EXCLUDED.content_hash,
                      projected_at = now(),
                      result       = EXCLUDED.result
        """,
        (o.param_key, o.behavior, o.target_table, o.target_pk,
         o.natural_key, o.content_hash, o.result),
    )


def _emit_row(cur, actor: str, o: Outcome) -> None:
    payload = {
        "behavior": o.behavior,
        "target_table": o.target_table,
        "target_pk": o.target_pk,
        "content_hash": o.content_hash,   # hash(ref) for secrets -- never the secret
    }
    if o.detail:
        payload["detail"] = o.detail
    emit(
        cur,
        actor=actor,
        event_type=f"param.{o.result}",
        entity_type="tenant_profile",
        entity_id=o.param_key,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# behavior dispatch -- STUBS. Step 3 replaces these with real writes/teardowns.
# A writer returns an Outcome; it must not commit (the tier owns the txn).
# ---------------------------------------------------------------------------
def _stub_writer(name: str):
    def writer(cur, row: dict, tenant_id: str | None) -> Outcome:
        return Outcome(
            param_key=row["param_key"],
            behavior=row["behavior"],
            target_table=_target_table(row),
            target_pk=None,
            natural_key=row.get("natural_key"),
            content_hash=None,
            result="skipped",
            detail=f"{name}: writer stub (Step 3)",
        )
    return writer


def _stub_teardown(name: str):
    def teardown(cur, ledger_row: dict) -> str:
        # Step 3: config-sync unset / stateful-merge reset-owned / seed-once
        # tombstone / policy-versioned archive-active / secret-write-once
        # sops-revoke + null token. Skeleton just marks retired.
        return "retired"
    return teardown


def _content_hash(value) -> str:
    """Stable short hash of a value for drift detection / ledger. For secrets the
    caller passes a ref, never the credential."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _ok(row: dict, table: str, pk: str | None, chash: str | None) -> Outcome:
    return Outcome(row["param_key"], row["behavior"], table, pk,
                   row.get("natural_key"), chash, "projected")


# ---------------------------------------------------------------------------
# config-sync -- authoritative overwrite of the profile-owned target.
# Targets are heterogeneous, so config-sync dispatches to a per-target adapter
# by table. Value conventions (invented here, to be aligned with the Vertical
# Pack in Step 5):
#   tenant_brand.<kind> : single row upsert by (tenant_id, kind). A JSON string
#                         value -> body_md; an object/array value -> json_data.
#   aliases             : set-reconcile. value = JSON array of
#                         {alias, target_kind, target_id}; the tenant's alias set
#                         is made to match exactly (upsert listed, delete rest).
# refs is deferred: it has no unique(scope,scope_id,key) key and its scope
# convention is unsettled.
# ---------------------------------------------------------------------------
def _cfg_brand(cur, row: dict, tenant_id: str | None) -> Outcome:
    target = row.get("target") or ""
    kind = target.split(".", 1)[1] if "." in target else None
    if not kind:
        raise ValueError("config-sync tenant_brand target must be 'tenant_brand.<kind>'")
    value = row["value"]
    if isinstance(value, str):
        body_md, json_data = value, "{}"   # json_data is NOT NULL (default {})
    else:
        body_md, json_data = None, json.dumps(value)
    cur.execute(
        """
        INSERT INTO sn_config.tenant_brand (tenant_id, kind, body_md, json_data, updated_at)
        VALUES (%s, %s, %s, %s::jsonb, now())
        ON CONFLICT (tenant_id, kind)
        DO UPDATE SET body_md = EXCLUDED.body_md,
                      json_data = EXCLUDED.json_data,
                      updated_at = now()
        RETURNING id
        """,
        (tenant_id, kind, body_md, json_data),
    )
    return _ok(row, "tenant_brand", str(cur.fetchone()["id"]), _content_hash(value))


def _cfg_aliases(cur, row: dict, tenant_id: str | None) -> Outcome:
    items = row["value"] or []
    if not isinstance(items, list):
        raise ValueError("config-sync aliases value must be a JSON array")
    wanted: list[str] = []
    for it in items:
        cur.execute(
            """
            INSERT INTO sn_config.aliases (tenant_id, alias, target_kind, target_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, alias)
            DO UPDATE SET target_kind = EXCLUDED.target_kind, target_id = EXCLUDED.target_id
            """,
            (tenant_id, it["alias"], it.get("target_kind"), it.get("target_id")),
        )
        wanted.append(it["alias"])
    if wanted:
        cur.execute(
            "DELETE FROM sn_config.aliases WHERE tenant_id = %s AND NOT (alias = ANY(%s))",
            (tenant_id, wanted),
        )
    else:
        cur.execute("DELETE FROM sn_config.aliases WHERE tenant_id = %s", (tenant_id,))
    return _ok(row, "aliases", f"aliases:tenant={tenant_id}", _content_hash(items))


_CONFIG_SYNC_TARGETS = {
    "tenant_brand": _cfg_brand,
    "aliases": _cfg_aliases,
}


def _config_sync_writer(cur, row: dict, tenant_id: str | None) -> Outcome:
    table = _target_table(row)
    fn = _CONFIG_SYNC_TARGETS.get(table)
    if fn is None:
        return Outcome(row["param_key"], "config-sync", table, None,
                       row.get("natural_key"), None, "skipped",
                       f"config-sync: no adapter for target {table!r}")
    return fn(cur, row, tenant_id)


def _config_sync_teardown(cur, lr: dict) -> str:
    table = lr["target_table"]
    if table == "tenant_brand":
        cur.execute("DELETE FROM sn_config.tenant_brand WHERE id = %s", (lr["target_pk"],))
    elif table == "aliases":
        tid = (lr["target_pk"] or "aliases:tenant=").split("=", 1)[1]
        if tid:
            cur.execute("DELETE FROM sn_config.aliases WHERE tenant_id = %s", (tid,))
    return "retired"


# ---------------------------------------------------------------------------
# seed-once -- create the target row if absent (by natural key) and then NEVER
# rewrite it: the runtime / user owns the row after birth. Ownership ("did the
# projector create this row, or did it pre-exist?") is decided at the FIRST
# projection and is sticky -- re-runs must not downgrade a row we created to
# ``skipped``, else teardown would refuse to tombstone our own row. Teardown
# tombstones (``archived_at``, never hard-delete) and ONLY rows we created; a
# pre-existing row we merely skipped is not ours to retire.
#
# value convention: a JSON object of column -> value for the INSERT; keys are
# whitelisted per target. ``natural_key`` is informational -- the conflict
# columns below are the real identity. A target needs a UNIQUE on its natural
# key; agents/specialists lack one and are deferred (like refs in config-sync).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _SeedSpec:
    conflict: tuple[str, ...]   # UNIQUE columns == natural identity
    cols: frozenset             # value keys allowed into the INSERT
    tenant: bool                # inject tenant_id into the insert + identity


_SEED_ONCE_TARGETS: dict[str, _SeedSpec] = {
    "departments": _SeedSpec(("tenant_id", "slug"),
                             frozenset({"slug", "name", "type_slug"}), True),
    "users": _SeedSpec(("tenant_id", "email"),
                        frozenset({"email", "name", "role", "handle",
                                   "contact_email", "active"}), True),
    "integrations": _SeedSpec(("slug",),
                              frozenset({"slug", "label", "description", "kind",
                                         "status", "install_uri", "companion_slug",
                                         "purpose", "endpoint", "auth_kind"}), False),
}


def _seed_once_writer(cur, row: dict, tenant_id: str | None) -> Outcome:
    table = _target_table(row)
    spec = _SEED_ONCE_TARGETS.get(table)
    if spec is None:
        return Outcome(row["param_key"], "seed-once", table, None,
                       row.get("natural_key"), None, "skipped",
                       f"seed-once: no adapter for target {table!r}")
    value = row["value"]
    if not isinstance(value, dict):
        raise ValueError("seed-once value must be a JSON object of columns")
    data = {k: v for k, v in value.items() if k in spec.cols}
    if spec.tenant:
        data["tenant_id"] = tenant_id
    if not data:
        raise ValueError("seed-once value has no known columns for target")

    cols = list(data)
    cur.execute(
        f"INSERT INTO sn_config.{table} ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT ({', '.join(spec.conflict)}) DO NOTHING RETURNING id",
        [data[c] for c in cols],
    )
    created = cur.fetchone()
    if created is not None:                       # we created (or re-seeded) it
        return _ok(row, table, str(created["id"]), _content_hash(value))

    # target already present -> never rewrite. Ownership is whatever the first
    # projection recorded: a prior 'projected' ledger row means the projector
    # created it, so it stays ours; otherwise it pre-existed and we only skip.
    cur.execute(
        "SELECT 1 FROM sn_config.projection_ledger "
        "WHERE param_key = %s AND target_table = %s AND result = 'projected' LIMIT 1",
        (row["param_key"], table),
    )
    owned = cur.fetchone() is not None
    where = " AND ".join(f"{c} = %s" for c in spec.conflict)
    cur.execute(f"SELECT id FROM sn_config.{table} WHERE {where}",
                [data[c] for c in spec.conflict])
    existing = cur.fetchone()
    pk = str(existing["id"]) if existing else None
    if owned:
        return _ok(row, table, pk, _content_hash(value))
    return Outcome(row["param_key"], "seed-once", table, pk,
                   row.get("natural_key"), _content_hash(value), "skipped",
                   "seed-once: target pre-existed, left as-is")


def _seed_once_teardown(cur, lr: dict) -> str:
    # Only retire rows WE created (ledger result 'projected'); a pre-existing row
    # we skipped is not ours. Tombstone via archived_at, never hard-delete.
    if lr.get("result") == "projected" and lr["target_table"] in _SEED_ONCE_TARGETS \
            and lr.get("target_pk"):
        cur.execute(
            f"UPDATE sn_config.{lr['target_table']} SET archived_at = now() "
            f"WHERE id = %s AND archived_at IS NULL",
            (lr["target_pk"],),
        )
    return "retired"


# ---------------------------------------------------------------------------
# policy-versioned -- the profile is authoritative for a node-level policy.
# Node-level identity is (slug, tenant_id IS NULL, agent_id IS NULL) among
# non-archived rows (see routes/policies.py); the read side (compose-prompt,
# agents.py) selects by slug + archived_at, never by tenant. So we write
# tenant_id/agent_id NULL. On a content change the active head is archived and a
# fresh row appended with a bumped patch version, preserving history; identical
# content re-projects to 'skipped' (idempotent, and it adopts a matching head we
# did not create). The ledger keeps ONE live row per param_key -- prior version
# rows are retired in-writer -- so teardown/drift see a single current pk.
# ---------------------------------------------------------------------------
_POLICY_CONTENT_KEYS = ("category", "title", "body_md", "severity", "enabled")


def _bump_patch(version: str) -> str:
    parts = (version or "0.1.0").split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except (ValueError, IndexError):
        return f"{version}.1"


def _policy_content(src: dict) -> dict:
    """Normalise a policy's semantic content so a value that omits defaulted
    fields hashes equal to the row Postgres would store (avoids phantom bumps)."""
    return {
        "category": src.get("category"),
        "title": src.get("title"),
        "body_md": src.get("body_md"),
        "severity": src.get("severity") or "warn",
        "enabled": src.get("enabled") if src.get("enabled") is not None else True,
    }


def _policy_versioned_writer(cur, row: dict, tenant_id: str | None) -> Outcome:
    if _target_table(row) != "policies":
        return Outcome(row["param_key"], "policy-versioned", _target_table(row), None,
                       row.get("natural_key"), None, "skipped",
                       "policy-versioned: only 'policies' is supported")
    value = row["value"]
    if not isinstance(value, dict):
        raise ValueError("policy-versioned value must be a JSON object")
    slug = value.get("slug") or row.get("natural_key")
    if not slug:
        raise ValueError("policy-versioned requires a slug (value.slug / natural_key)")
    content = _policy_content(value)
    chash = _content_hash(content)

    cur.execute(
        "SELECT id, version, category, title, body_md, severity, enabled "
        "FROM sn_config.policies "
        "WHERE slug = %s AND tenant_id IS NULL AND agent_id IS NULL "
        "AND archived_at IS NULL ORDER BY id DESC",
        (slug,),
    )
    active = cur.fetchall()
    head = active[0] if active else None
    if head and _content_hash(_policy_content(head)) == chash:
        return Outcome(row["param_key"], "policy-versioned", "policies",
                       str(head["id"]), slug, chash, "skipped",
                       "policy-versioned: unchanged")

    new_version = _bump_patch(head["version"]) if head else (value.get("version") or "0.1.0")
    if active:  # archive every active row for the slug -> single active head
        cur.execute(
            "UPDATE sn_config.policies SET archived_at = now(), updated_by = %s "
            "WHERE slug = %s AND tenant_id IS NULL AND agent_id IS NULL "
            "AND archived_at IS NULL",
            (ACTOR, slug),
        )
    cur.execute(
        """
        INSERT INTO sn_config.policies
            (tenant_id, agent_id, slug, category, title, body_md, severity,
             enabled, version, created_by, updated_by)
        VALUES (NULL, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (slug, content["category"], content["title"], content["body_md"],
         content["severity"], content["enabled"], new_version, ACTOR, ACTOR),
    )
    new_pk = str(cur.fetchone()["id"])
    cur.execute(
        "UPDATE sn_config.projection_ledger SET result = 'retired' "
        "WHERE param_key = %s AND target_table = 'policies' AND target_pk <> %s "
        "AND result <> 'retired'",
        (row["param_key"], new_pk),
    )
    return _ok(row, "policies", new_pk, chash)


def _policy_versioned_teardown(cur, lr: dict) -> str:
    # Profile no longer wants the policy -> archive its current active version.
    if lr.get("target_pk"):
        cur.execute(
            "UPDATE sn_config.policies SET archived_at = now(), updated_by = %s "
            "WHERE id = %s AND archived_at IS NULL",
            (ACTOR, lr["target_pk"]),
        )
    return "retired"


# ---------------------------------------------------------------------------
# stateful-merge -- merge profile-owned fields into an EXISTING target row,
# leaving runtime-owned state intact. Fail-closed: a column / JSON-path is only
# written if sn_config.merge_ownership records owner='profile'; anything
# unregistered (or 'runtime') is never touched. The target is located, never
# created -- an absent row re-projects to 'skipped' (seed it via seed-once /
# config-sync first). Invariant enforcement is delegated to the target's own DB
# CHECKs (a violating merge fails the row's SAVEPOINT, not the tier).
#
# value convention:
#   {"match": {<col>: <val>, ...},          # identity of the existing row
#    "set":   {<col|col.json.path>: <val>}} # fields to merge (ownership-gated)
# A dotted key is a JSON-path into the jsonb column named by its first segment
# (e.g. "metadata.tier" -> jsonb_set(metadata,'{tier}',...)); a bare key is a
# scalar column. merge_ownership registers the FULL key string.
#
# Teardown ("reset-owned"): every profile-owned key for the target_table is
# reset on the row -- scalar columns to their column DEFAULT, JSON-paths removed
# (`#-`). A profile-owned scalar with no default resets to NULL, so only mark
# defaulted / nullable columns as profile-owned. runtime-owned state is left.
# ---------------------------------------------------------------------------
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _safe_ident(name: str) -> str:
    """Whitelist an SQL identifier segment -- table / column / json key come
    from the (semi-trusted) profile value and are interpolated into SQL."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"stateful-merge: unsafe identifier {name!r}")
    return name


def _merge_owner(cur, table: str, key: str) -> str:
    cur.execute(
        "SELECT owner FROM sn_config.merge_ownership "
        "WHERE target_table = %s AND column_or_jsonpath = %s",
        (table, key),
    )
    r = cur.fetchone()
    return r["owner"] if r else "runtime"   # fail-closed: unregistered => runtime


def _split_path(key: str) -> tuple[str, list[str]]:
    """'metadata.a.b' -> ('metadata', ['a','b']); 'status' -> ('status', [])."""
    col, *path = key.split(".")
    _safe_ident(col)
    for p in path:
        _safe_ident(p)
    return col, path


def _stateful_merge_writer(cur, row: dict, tenant_id: str | None) -> Outcome:
    table = _target_table(row)
    if not table:
        raise ValueError("stateful-merge requires a target table")
    _safe_ident(table)
    value = row["value"]
    if not isinstance(value, dict):
        raise ValueError("stateful-merge value must be an object")
    match = value.get("match") or {}
    payload = value.get("set") or {}
    if not match:
        raise ValueError("stateful-merge requires a 'match' identity")

    # partition requested fields by ownership (fail-closed)
    owned = [(k, v) for k, v in payload.items() if _merge_owner(cur, table, k) == "profile"]
    denied = [k for k in payload if (k, payload[k]) not in owned]
    detail = f"skipped non-profile keys: {denied}" if denied else None

    where = " AND ".join(f"{_safe_ident(c)} = %s" for c in match)
    cur.execute(f"SELECT id FROM sn_config.{table} WHERE {where}", list(match.values()))
    found = cur.fetchall()
    if not found:
        return Outcome(row["param_key"], "stateful-merge", table, None,
                       row.get("natural_key"), None, "skipped",
                       "stateful-merge: target absent (seed it first)")
    if len(found) > 1:
        raise ValueError("stateful-merge: 'match' is ambiguous (>1 row)")
    pk = str(found[0]["id"])
    if not owned:
        return Outcome(row["param_key"], "stateful-merge", table, pk,
                       row.get("natural_key"), None, "skipped",
                       detail or "stateful-merge: no profile-owned fields")

    assigns: list[str] = []
    params: list = []
    json_cols: dict[str, list[tuple[list[str], object]]] = {}
    for key, v in owned:
        col, path = _split_path(key)
        if path:
            json_cols.setdefault(col, []).append((path, v))
        else:
            assigns.append(f"{col} = %s")
            params.append(v)
    for col, entries in json_cols.items():
        expr = f"coalesce({col}, '{{}}'::jsonb)"
        for path, v in entries:
            expr = f"jsonb_set({expr}, %s, %s::jsonb, true)"
            params.append("{" + ",".join(path) + "}")
            params.append(json.dumps(v))
        assigns.append(f"{col} = {expr}")
    params.append(pk)
    cur.execute(f"UPDATE sn_config.{table} SET {', '.join(assigns)} WHERE id = %s", params)
    return Outcome(row["param_key"], "stateful-merge", table, pk,
                   row.get("natural_key"), _content_hash(dict(owned)), "projected", detail)


def _stateful_merge_teardown(cur, lr: dict) -> str:
    table, pk = lr.get("target_table"), lr.get("target_pk")
    if not table or not pk:
        return "retired"
    _safe_ident(table)
    cur.execute(
        "SELECT column_or_jsonpath FROM sn_config.merge_ownership "
        "WHERE target_table = %s AND owner = 'profile'",
        (table,),
    )
    keys = [r["column_or_jsonpath"] for r in cur.fetchall()]
    if not keys:
        return "retired"
    assigns: list[str] = []
    params: list = []
    json_cols: dict[str, list[list[str]]] = {}
    for key in keys:
        col, path = _split_path(key)
        if path:
            json_cols.setdefault(col, []).append(path)
        else:
            assigns.append(f"{col} = DEFAULT")
    for col, paths in json_cols.items():
        expr = col
        for path in paths:
            expr = f"({expr}) #- %s"
            params.append("{" + ",".join(path) + "}")
        assigns.append(f"{col} = {expr}")
    params.append(pk)
    cur.execute(f"UPDATE sn_config.{table} SET {', '.join(assigns)} WHERE id = %s", params)
    return "retired"


# ---------------------------------------------------------------------------
# secret-write-once -- bind a write-once secret REFERENCE onto an existing
# integration; the projector never touches the sops+age store (routes/secrets.py
# is the only sanctioned write path) and never sees plaintext -- the profile
# value carries only a secret-ref (dotted path, e.g. "replicate.api_key"). If the
# integration already has an auth_secret_ref it is kept (write-once, never
# rewritten). Ownership is sticky (like seed-once) so re-runs don't downgrade a
# binding we created. content_hash = hash(ref), never hash(secret). Teardown
# nulls only a binding we created; the encrypted secret material is left for
# routes/secrets.py to revoke.
# ---------------------------------------------------------------------------
_SECRET_REF_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,40}(\.[a-zA-Z_][a-zA-Z0-9_-]{0,40}){0,5}$")


def _secret_write_once_writer(cur, row: dict, tenant_id: str | None) -> Outcome:
    table = _target_table(row)
    if table != "integrations":
        return Outcome(row["param_key"], "secret-write-once", table, None,
                       row.get("natural_key"), None, "skipped",
                       "secret-write-once: only 'integrations' is supported")
    value = row["value"]
    if isinstance(value, str):
        ref, auth_kind, auth_header, slug = value, None, None, None
    elif isinstance(value, dict):
        ref = value.get("ref")
        auth_kind = value.get("auth_kind")
        auth_header = value.get("auth_header_name")
        slug = value.get("slug")
    else:
        raise ValueError("secret-write-once value must be a secret-ref string or object")
    slug = slug or row.get("natural_key")
    if not ref or not _SECRET_REF_RE.match(ref):
        raise ValueError("secret-write-once requires a valid dotted secret ref")
    if not slug:
        raise ValueError("secret-write-once requires an integration slug (natural_key)")

    cur.execute(
        "SELECT id, auth_secret_ref FROM sn_config.integrations WHERE slug = %s",
        (slug,),
    )
    found = cur.fetchall()
    if not found:
        return Outcome(row["param_key"], "secret-write-once", "integrations", None,
                       slug, _content_hash(ref), "skipped",
                       "secret-write-once: integration absent (seed it first)")
    if len(found) > 1:
        raise ValueError("secret-write-once: slug is ambiguous (>1 row)")
    pk = str(found[0]["id"])
    existing = found[0]["auth_secret_ref"]

    if existing:                                    # write-once: never rewrite
        cur.execute(
            "SELECT 1 FROM sn_config.projection_ledger "
            "WHERE param_key = %s AND target_table = 'integrations' "
            "AND result = 'projected' LIMIT 1",
            (row["param_key"],),
        )
        owned = cur.fetchone() is not None
        if existing == ref:
            detail = None if owned else "secret-write-once: already bound (pre-existing)"
        else:
            detail = f"secret-write-once: write-once kept {existing!r}, refused {ref!r}"
        return Outcome(row["param_key"], "secret-write-once", "integrations", pk,
                       slug, _content_hash(existing),
                       "projected" if owned else "skipped", detail)

    sets, params = ["auth_secret_ref = %s"], [ref]
    if auth_kind is not None:
        sets.append("auth_kind = %s")
        params.append(auth_kind)
    if auth_header is not None:
        sets.append("auth_header_name = %s")
        params.append(auth_header)
    params.append(pk)
    cur.execute(
        f"UPDATE sn_config.integrations SET {', '.join(sets)}, updated_at = now() "
        f"WHERE id = %s",
        params,
    )
    return Outcome(row["param_key"], "secret-write-once", "integrations", pk,
                   slug, _content_hash(ref), "projected")


def _secret_write_once_teardown(cur, lr: dict) -> str:
    # Null only a binding we created; never touch the sops store.
    if lr.get("result") == "projected" and lr.get("target_pk"):
        cur.execute(
            "UPDATE sn_config.integrations "
            "SET auth_secret_ref = NULL, auth_kind = 'none', auth_header_name = NULL, "
            "updated_at = now() WHERE id = %s AND auth_secret_ref IS NOT NULL",
            (lr["target_pk"],),
        )
    return "retired"


BEHAVIOR_WRITERS = {b: _stub_writer(b) for b in BEHAVIORS}
BEHAVIOR_WRITERS["config-sync"] = _config_sync_writer
BEHAVIOR_WRITERS["seed-once"] = _seed_once_writer
BEHAVIOR_WRITERS["policy-versioned"] = _policy_versioned_writer
BEHAVIOR_WRITERS["stateful-merge"] = _stateful_merge_writer
BEHAVIOR_WRITERS["secret-write-once"] = _secret_write_once_writer
BEHAVIOR_TEARDOWNS = {b: _stub_teardown(b) for b in BEHAVIORS}
BEHAVIOR_TEARDOWNS["config-sync"] = _config_sync_teardown
BEHAVIOR_TEARDOWNS["seed-once"] = _seed_once_teardown
BEHAVIOR_TEARDOWNS["policy-versioned"] = _policy_versioned_teardown
BEHAVIOR_TEARDOWNS["stateful-merge"] = _stateful_merge_teardown
BEHAVIOR_TEARDOWNS["secret-write-once"] = _secret_write_once_teardown


def _project_row(cur, row: dict, tenant_id: str | None, actor: str) -> Outcome:
    behavior = row.get("behavior")
    writer = BEHAVIOR_WRITERS.get(behavior)
    # A per-row SAVEPOINT so a failing row rolls back cleanly (leaving the txn
    # usable) instead of poisoning the whole tier.
    cur.execute("SAVEPOINT sp_row")
    try:
        if writer is None:
            raise ValueError(f"unknown behavior {behavior!r}")
        o = writer(cur, row, tenant_id)
        cur.execute("RELEASE SAVEPOINT sp_row")
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT sp_row")
        log.exception("projector: writer failed for %s", row.get("param_key"))
        o = Outcome(row["param_key"], behavior, _target_table(row), None,
                    row.get("natural_key"), None, "failed", str(exc))
    _write_ledger(cur, o)
    _emit_row(cur, actor, o)
    return o


# ---------------------------------------------------------------------------
# reverse reconcile / teardown pass (RFC B2)
# ---------------------------------------------------------------------------
def _reconcile(cur, actor: str) -> list[Outcome]:
    cur.execute("SELECT param_key FROM sn_config.tenant_profile WHERE status <> 'retired'")
    live = {r["param_key"] for r in cur.fetchall()}
    cur.execute(
        "SELECT param_key, behavior, target_table, target_pk, natural_key, result "
        "FROM sn_config.projection_ledger WHERE result <> 'retired'"
    )
    outcomes: list[Outcome] = []
    for lr in cur.fetchall():
        if lr["param_key"] in live:
            continue
        teardown = BEHAVIOR_TEARDOWNS.get(lr["behavior"])
        detail = None if teardown else f"no teardown for behavior {lr['behavior']!r}"
        try:
            result = teardown(cur, lr) if teardown else "failed"
        except Exception as exc:
            log.exception("projector: teardown failed for %s", lr["param_key"])
            result, detail = "failed", str(exc)
        cur.execute(
            """
            UPDATE sn_config.projection_ledger
               SET result = %s, projected_at = now()
             WHERE param_key = %s
               AND target_table IS NOT DISTINCT FROM %s
               AND target_pk IS NOT DISTINCT FROM %s
            """,
            (result, lr["param_key"], lr["target_table"], lr["target_pk"]),
        )
        o = Outcome(lr["param_key"], lr["behavior"], lr["target_table"],
                    lr["target_pk"], lr["natural_key"], None, result, detail)
        _emit_row(cur, actor, o)
        outcomes.append(o)
    return outcomes


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------
def project(actor: str = ACTOR) -> dict:
    """Run a full projection: forward tiers T0..T7 then the reverse pass.
    Idempotent and re-runnable forward. Returns a per-result summary."""
    all_outcomes: list[Outcome] = []

    for idx, (tier_id, tables) in enumerate(TIERS):
        with db.cursor(commit=True) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_NS, idx))
            tenant_id = _resolve_tenant(cur)
            rows = _load_tier_rows(cur, tables)
            for row in rows:
                all_outcomes.append(_project_row(cur, row, tenant_id, actor))
            if rows:
                log.info("projector %s: %d row(s)", tier_id, len(rows))

    with db.cursor(commit=True) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_NS, _RECONCILE_LOCK_KEY))
        all_outcomes.extend(_reconcile(cur, actor))

    summary: dict[str, int] = {}
    for o in all_outcomes:
        summary[o.result] = summary.get(o.result, 0) + 1

    with db.cursor(commit=True) as cur:
        emit(cur, actor=actor, event_type="projection.run",
             entity_type="node", entity_id=None, payload={"summary": summary})

    log.info("projector: run complete %s", summary)
    return {"summary": summary, "count": len(all_outcomes)}


if __name__ == "__main__":  # companion / CLI use
    logging.basicConfig(level=logging.INFO)
    print(project())
