#!/usr/bin/env python3
"""Remove the one-node direct-store workaround superseded by the API-backed document store."""

import glob
import json
import os
from pathlib import Path
import subprocess
import tempfile


DATA_ROOT = Path(os.environ.get("SN_DATA_ROOT", "/srv/data"))
STORE = Path(os.environ.get("SN_ATTACHMENT_DIR", "/srv/data/attachments"))
HOME_GLOB = os.environ.get(
    "SN_AGENT_SETTINGS_GLOB",
    "/var/lib/smart-node/agents/agent-*/.claude/settings.json",
)
DIRECT_READ = "Read(/srv/data/attachments/**)"
DIRECT_DIR = "/srv/data/attachments"


def _acl(path: Path) -> list[str]:
    result = subprocess.run(
        ["getfacl", "-cp", "--", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not inspect ACL on %s" % path)
    return result.stdout.splitlines()


def _remove_acl(path: Path, spec: str) -> None:
    subprocess.run(
        ["setfacl", "-x", spec, "--", str(path)], check=True,
    )


def _retire_entry(path: Path, *, access: bool, default: bool) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    lines = _acl(path)
    removed = 0
    if access and any(line.startswith("group:smart-node-agents:") for line in lines):
        _remove_acl(path, "g:smart-node-agents")
        removed += 1
    if (default and path.is_dir()
            and any(line.startswith("default:group:smart-node-agents:") for line in lines)):
        _remove_acl(path, "d:g:smart-node-agents")
        removed += 1
    return removed


def retire_acl() -> None:
    removed = _retire_entry(DATA_ROOT, access=True, default=False)
    removed += _retire_entry(STORE, access=True, default=False)

    desks = STORE / "desks"
    if desks.is_dir() and not desks.is_symlink():
        for current, directories, files in os.walk(desks, followlinks=False):
            current_path = Path(current)
            directories[:] = [name for name in directories
                               if not (current_path / name).is_symlink()]
            removed += _retire_entry(current_path, access=True, default=True)
            for name in files:
                removed += _retire_entry(
                    current_path / name, access=True, default=False,
                )
    if removed:
        print("retired %s direct attachment-store ACL entries" % removed)


def retire_settings(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        return
    changed = False
    for key, value in (("allow", DIRECT_READ), ("additionalDirectories", DIRECT_DIR)):
        entries = permissions.get(key)
        if not isinstance(entries, list) or value not in entries:
            continue
        permissions[key] = [entry for entry in entries if entry != value]
        if not permissions[key]:
            permissions.pop(key)
        changed = True
    if not changed:
        return
    if not permissions:
        data.pop("permissions")
    if not data:
        path.unlink()
    else:
        st = path.stat()
        fd, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.chmod(temporary, st.st_mode & 0o777)
            os.chown(temporary, st.st_uid, st.st_gid)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    print("retired direct attachment-store settings from %s" % path)


def main() -> None:
    retire_acl()
    for name in glob.glob(HOME_GLOB):
        retire_settings(Path(name))


if __name__ == "__main__":
    main()
