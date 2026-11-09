"""Pure parsing + health mapping for the host service collector.

The host-side companion (companions/collect-services.py) shells out to
`docker ps` and `systemctl list-units`; these functions turn that raw output
into the rows written to sn_core.node_services. Kept DB- and subprocess-free so
they can be unit-tested without a host (mirrors lib/crontab.py).

A "service row" is a dict: {name, health, status, version, detail}.
  health: 'ok' | 'warn' | 'fail'
  version: image:tag for containers, '' for systemd units
"""

import json

# A sweep older than this is treated as "no collector reporting" by the
# endpoint -- counts go null rather than showing stale numbers as if live.
# Default collector cadence is every 2 min; 5 min tolerates one missed run.
STALE_AFTER_SECS = 300


# --------------------------------------------------------------------- docker

# docker `State` -> health. A smart-node container that is not running is a
# problem worth surfacing, so exited/dead are 'fail', transient states 'warn'.
_CONTAINER_HEALTH = {
    "running": "ok",
    "restarting": "warn",
    "paused": "warn",
    "created": "warn",
    "removing": "warn",
    "exited": "fail",
    "dead": "fail",
}


def parse_docker_ps(stdout: str) -> list[dict]:
    """Parse `docker ps -a --format '{{json .}}'` (one JSON object per line).

    Unparseable lines are skipped rather than aborting the whole sweep.
    """
    rows: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        state = (c.get("State") or "").lower()
        rows.append({
            "name": c.get("Names") or c.get("ID") or "?",
            "health": _CONTAINER_HEALTH.get(state, "warn"),
            "status": c.get("Status") or state or "",
            "version": c.get("Image") or "",
            "detail": "",
        })
    return rows


# -------------------------------------------------------------------- systemd

def _systemd_health(active: str, sub: str) -> str:
    active, sub = active.lower(), sub.lower()
    if active == "failed":
        return "fail"
    if active == "active":
        return "ok" if sub in ("running", "exited", "listening", "active") else "warn"
    # activating / deactivating / reloading / inactive
    return "warn"


def parse_systemctl(stdout: str) -> list[dict]:
    """Parse `systemctl list-units --type=service --all --no-legend --plain`.

    Columns are whitespace-separated: UNIT LOAD ACTIVE SUB DESCRIPTION.., the
    description being the free-text remainder. Lines that don't split into the
    five expected fields are skipped.
    """
    rows: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit, _load, active, sub = parts[0], parts[1], parts[2], parts[3]
        desc = parts[4] if len(parts) > 4 else ""
        rows.append({
            "name": unit,
            "health": _systemd_health(active, sub),
            "status": f"{active} ({sub})",
            "version": "",
            "detail": desc,
        })
    return rows
