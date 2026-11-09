"""Render a cron.d file from sn_core.crons rows.

This is the DB -> filesystem projection for cron (the reconciliation principle):
the panel edits sn_core.crons; the controller re-renders /etc/cron.d/smart-node-*
so the crontab is never hand-edited as source. Each line invokes the existing
cron-runner.sh wrapper (per-job lock + run logging).

Pure function `render_crontab` is kept import-friendly so it can be unit-tested
without a database; `companions/render-crontab.py` wraps it with the DB query.
"""

RUNNER = "/usr/local/lib/smart-node/cron-runner.sh"

# The file admin-runner's materialize_crons installs (CRON_TARGET there). It is
# world-readable, so core-api can tell whether a registered job has reached it
# without any privilege.
CRON_TARGET = "/etc/cron.d/smart-node-agents"

# smart-node-cron-reconcile.timer (OnUnitActiveSec in companions/core-api.sh)
# re-renders the registry this often, so a registered job goes live by itself.
RECONCILE_MINUTES = 5

LIVE_NOTE = (
    f"The job goes live in the node crontab by itself within {RECONCILE_MINUTES} minutes "
    "(smart-node-cron-reconcile.timer). Do not call /api/crons/render; "
    "GET /api/crons/<id> shows materialized=true once it is there."
)

HEADER = (
    "# Smart Node crons -- GENERATED from sn_core.crons by core-api. DO NOT EDIT.\n"
    "# Edit jobs in the panel; the controller re-renders this file.\n"
)


def job_name(row: dict) -> str:
    """Stable job slug for the cron-runner (matches ^[a-z][a-z0-9-]*$).

    Prefers the crons.slug; falls back to cron-<id> for rows without one.
    """
    return row.get("slug") or f"cron-{row['id']}"


def cron_line(row: dict, runner: str = RUNNER) -> str:
    """The cron.d line one row renders to."""
    user = row.get("crontab_user") or "root"
    return f"{row['schedule']} {user} {runner} {job_name(row)} {row['command']}"


def read_installed(path: str = CRON_TARGET) -> str | None:
    """Text of the installed cron.d file, None when it cannot be read.

    None stays None all the way to the caller: an unreadable file is not a
    job that is missing from it.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def is_materialized(row: dict, installed: str | None, runner: str = RUNNER) -> bool | None:
    """Whether the installed file already matches this row.

    An enabled row is materialized when its line is in the file; a disabled
    row when its line is not. None when the file could not be read.
    """
    if installed is None:
        return None
    present = cron_line(row, runner) in installed.splitlines()
    return present if row.get("enabled", True) else not present


def render_crontab(rows: list[dict], runner: str = RUNNER) -> str:
    """Render enabled cron rows into a cron.d file body.

    cron.d line format: `<schedule> <user> <command>`. The command is routed
    through cron-runner.sh with a per-job name so runs/locks are tracked.
    """
    lines = [HEADER.rstrip("\n")]
    for r in rows:
        if not r.get("enabled", True):
            continue
        if r.get("description"):
            lines.append(f"# {r['description']}")
        lines.append(cron_line(r, runner))
    return "\n".join(lines) + "\n"
