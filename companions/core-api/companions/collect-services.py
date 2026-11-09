"""Host service collector: docker + systemd status -> sn_core.node_services.

Runs ON THE HOST (not in the core-api container) as a cron job, because
`docker ps` and `systemctl` are host-runtime and invisible from inside the
container. Registered as the disabled 'collect-services' companion (migration
013); enable it on the Crons page and project the crontab to start it.

Each kind (container / systemd) is collected and committed independently: if
docker is absent but systemd works, systemd still updates. A failed collection
records a sweep row with ok=false + error and leaves the previous rows in place
(the endpoint ages them out via STALE_AFTER_SECS).

DB connection uses libpq env, same as companions/render-crontab.py.

    python companions/collect-services.py
"""

import logging
import os
import subprocess
import sys

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.node_services import parse_docker_ps, parse_systemctl  # noqa: E402

log = logging.getLogger("collect-services")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# systemd units to report. Node infra is smart-node-*; override only when the
# operator wants a wider/narrower scope (explicit env, no silent fallback value).
SYSTEMD_PATTERN = (
    os.environ["SN_SERVICES_SYSTEMD_GLOB"]
    if "SN_SERVICES_SYSTEMD_GLOB" in os.environ
    else "smart-node*"
)

_TIMEOUT = 30


def _run(cmd: list[str]) -> str:
    """Run a host command, return stdout. Raises on non-zero / missing binary."""
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited {res.returncode}: {res.stderr.strip()[:200]}")
    return res.stdout


def collect_containers() -> list[dict]:
    return parse_docker_ps(_run(["docker", "ps", "-a", "--format", "{{json .}}"]))


def collect_systemd() -> list[dict]:
    out = _run([
        "systemctl", "list-units", "--type=service", "--all",
        "--no-legend", "--plain", "--no-pager", SYSTEMD_PATTERN,
    ])
    return parse_systemctl(out)


def _replace_kind(cur, kind: str, rows: list[dict]) -> None:
    """Swap in the freshly collected rows for one kind and stamp the sweep."""
    cur.execute("DELETE FROM sn_core.node_services WHERE kind = %s", (kind,))
    if rows:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO sn_core.node_services "
            "(kind, name, health, status, version, detail, collected_at) VALUES %s",
            [
                (kind, r["name"], r["health"], r["status"], r["version"], r["detail"])
                for r in rows
            ],
            template="(%s, %s, %s, %s, %s, %s, now())",
        )
    _stamp_sweep(cur, kind, ok=True, error="", count=len(rows))


def _stamp_sweep(cur, kind: str, ok: bool, error: str, count: int) -> None:
    cur.execute(
        "INSERT INTO sn_core.node_service_sweeps (kind, collected_at, ok, error, count) "
        "VALUES (%s, now(), %s, %s, %s) "
        "ON CONFLICT (kind) DO UPDATE SET "
        "collected_at = now(), ok = EXCLUDED.ok, error = EXCLUDED.error, count = EXCLUDED.count",
        (kind, ok, error[:500], count),
    )


def _sweep(conn, kind: str, collector) -> None:
    """Collect one kind in its own transaction. On failure, record the sweep as
    not-ok and keep the previous rows (don't wipe data on a transient error)."""
    try:
        rows = collector()
    except Exception as ex:  # missing binary, timeout, permission, parse blowup
        log.warning("%s collection failed: %s", kind, ex)
        with conn, conn.cursor() as cur:
            _stamp_sweep(cur, kind, ok=False, error=str(ex), count=0)
        return
    with conn, conn.cursor() as cur:
        _replace_kind(cur, kind, rows)
    log.info("%s: %d service(s)", kind, len(rows))


def main() -> None:
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        _sweep(conn, "container", collect_containers)
        _sweep(conn, "systemd", collect_systemd)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
