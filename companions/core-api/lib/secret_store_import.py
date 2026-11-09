"""Import the sops-era store into sn_config.secrets, once.

Until this release the integrations' secrets lived in a sops+age file
(/etc/smart-node/integrations.sops.yaml) that core-api decrypted with the node
age key on every call. The one store is the database now. On startup, if the
file is there and has not been imported, every leaf of it becomes a row
(bookkeeping keys and the old snapshots excluded), and the import is written
to the journal as secret.store.imported. That event is the marker: a later
start sees it and leaves the file alone, so a ref the operator deleted since
is not resurrected from a stale file. The file itself is not touched here;
removing it is the operator's step, named in the upgrade notes.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Iterator

import yaml

from db import cursor
from events_emit import emit
from lib import secret_store

log = logging.getLogger("core-api.secret-store.import")

SECRETS_FILE = os.environ.get("SN_SECRETS_FILE", "/etc/smart-node/integrations.sops.yaml")
AGE_KEY_FILE = os.environ.get("SN_AGE_KEY_FILE", "/etc/smart-node/age-key.txt")
SOPS_BIN = os.environ.get("SN_SOPS_BIN", "/usr/local/bin/sops")
SOPS_TIMEOUT = 20
META_KEYS = {"sops", "_meta", "_snaps"}
ACTOR = "component:core-api"
EVENT = "secret.store.imported"


def _decrypt(path: str) -> dict:
    env = dict(os.environ, SOPS_AGE_KEY_FILE=AGE_KEY_FILE)
    r = subprocess.run([SOPS_BIN, "-d", path], capture_output=True, env=env, timeout=SOPS_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError("sops decrypt failed rc=%s: %s" % (r.returncode, r.stderr.decode("utf-8", "replace")[:300]))
    data = yaml.safe_load(r.stdout) or {}
    if not isinstance(data, dict):
        raise RuntimeError("sops file is not a mapping")
    return data


def leaves(data: dict, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Every (ref, value) of the decrypted file, bookkeeping keys skipped."""
    for key, val in data.items():
        if not prefix and key in META_KEYS:
            continue
        ref = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict):
            yield from leaves(val, ref)
        elif val is not None and not isinstance(val, (list,)):
            yield ref, str(val)


def already_imported(cur, path: str) -> bool:
    cur.execute("SELECT 1 FROM sn_core.events WHERE event_type = %s AND entity_id = %s LIMIT 1",
                (EVENT, path))
    return cur.fetchone() is not None


def import_file(path: str = SECRETS_FILE, decrypt=_decrypt) -> dict | None:
    """Import once. Returns the summary written to the journal, or None when
    there was nothing to do (no file, or imported before). Never raises: a
    failed import is logged and retried at the next start."""
    if not os.path.isfile(path):
        return None
    try:
        with cursor() as cur:
            if already_imported(cur, path):
                return None
    except Exception as e:  # noqa: BLE001
        log.warning("sops import skipped, journal unreadable: %s", e)
        return None
    try:
        data = decrypt(path)
    except Exception as e:  # noqa: BLE001
        log.error("sops import skipped, file unreadable (%s); will retry at next start", e)
        return None
    imported, kept, invalid = [], [], []
    try:
        with cursor(commit=True) as cur:
            for ref, value in leaves(data):
                if not secret_store.valid_ref(ref):
                    invalid.append(ref)
                    continue
                if secret_store.exists(cur, ref):
                    kept.append(ref)          # the store's value wins over the file's
                    continue
                secret_store.set_value(cur, ref, value, ACTOR, note="imported from the sops file")
                imported.append(ref)
            summary = {"file": path, "imported": imported, "kept": kept, "invalid": invalid}
            emit(cur, actor=ACTOR, event_type=EVENT, entity_type="secret_store", entity_id=path,
                 payload={"imported": len(imported), "kept": len(kept), "invalid": invalid,
                          "refs": imported})
    except Exception as e:  # noqa: BLE001
        log.error("sops import failed, nothing recorded; will retry at next start: %s", e)
        return None
    log.warning("sops store imported into sn_config.secrets: %d ref(s) imported, %d kept, %d invalid. "
                "The file %s is no longer read; remove it when the panel shows every ref.",
                len(imported), len(kept), len(invalid), path)
    return summary
