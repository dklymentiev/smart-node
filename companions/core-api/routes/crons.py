"""Cron registry CRUD + cron_runs history (sn_core.crons + sn_core.cron_runs).

Each row represents a job line in
/etc/cron.d/smart-node-*. POST is upsert by (schedule, command, crontab_user)
so installer / panel can register jobs idempotently.

cron_runs population from cron-runner.sh is deferred to a follow-up (mapping
JOB slug back to cron_id needs a name/slug column). For v0 Foundation,
cron-runner emits cron.started/cron.completed/cron.failed events into
sn_core.events with entity_type=cron, entity_id=<JOB slug>.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from auth import Principal, get_actor, get_principal
from db import cursor
from events_emit import emit
from lib.admin_runner import runner_op
from lib.crontab import LIVE_NOTE, RECONCILE_MINUTES, is_materialized, read_installed, render_crontab
from permissions import requires
from schemas.cron import Cron, CronCreate, CronUpdate, CronRunCreate, CronRunFinish

log = logging.getLogger("core-api.crons")

router = APIRouter(prefix="/api/crons", tags=["crons"])


SELECT_COLS = (
    'id, slug, schedule, command, script_path, description, "group", log_file, '
    'crontab_user, enabled, disabled_flag, notes, created_at, updated_at'
)


@router.get("", response_model=dict, dependencies=[Depends(requires("crons.read"))])
def list_crons(enabled_only: bool = False, group: str | None = None):
    sql = f"SELECT {SELECT_COLS} FROM sn_core.crons"
    where: list[str] = []
    params: list = []
    if enabled_only:
        where.append("enabled = true")
    if group:
        where.append('"group" = %s')
        params.append(group)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += ' ORDER BY "group", id'
    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    installed = read_installed()
    for r in rows:
        r["materialized"] = is_materialized(r, installed)
    return {"crons": rows, "count": len(rows)}


@router.get("/panel", response_model=dict, dependencies=[Depends(requires("crons.read"))])
def crons_panel(principal: Principal = Depends(get_principal)):
    """Cron view shaped for the Crons page, tenant-scoped.

    DB registry rows (sn_core.crons) map to the panel's "crontabs" list, with
    last-run time from sn_core.cron_runs. systemd "timers" and live next-run
    elapse are host-runtime (systemctl) and are not in the DB -- left empty
    until a host collector reports them, never fabricated.
    """
    sql = (
        'SELECT c.id, c.slug, c.schedule, c.command, c.description, c."group", '
        "       c.crontab_user, c.enabled, "
        "       (SELECT max(r.started_at) FROM sn_core.cron_runs r WHERE r.cron_id = c.id) AS last_started, "
        # BUSY = an open run (finished_at IS NULL). stale-guard: an open row older
        # than 15 min is treated as crashed (wrapper SIGKILLed before /finish), not running.
        "       EXISTS (SELECT 1 FROM sn_core.cron_runs r WHERE r.cron_id = c.id "
        "               AND r.finished_at IS NULL AND r.started_at > now() - interval '15 minutes') AS running "
        "FROM sn_core.crons c "
    )
    params: list = []
    if not principal.cross_tenant:
        sql += "WHERE c.tenant_id = %s "
        params.append(principal.tenant_id)
    sql += 'ORDER BY c."group", c.id'

    with cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    crontabs = [{
        "kind": "crontab",
        "id": r["id"],
        "scope": "smart-node",
        "name": r["slug"] or f"cron-{r['id']}",
        "slug": r["slug"],
        "schedule": r["schedule"],
        "description": r["description"] or "",
        "target": r["command"],
        "command": r["command"],
        "group": r["group"],
        "crontab_user": r["crontab_user"],
        "enabled": r["enabled"],
        "running": r["running"],   # BUSY = open cron_runs row (DB mirror of the wrapper flock)
        "last_iso": r["last_started"].isoformat() if r["last_started"] else None,
        "next_iso": None,   # host-runtime (systemd/cron next-elapse); not in DB
    } for r in rows]

    return {
        "timers": [],       # systemd timers are host-only
        "crontabs": crontabs,
        "summary": {
            "timers_total": 0,
            "crontab_entries": len(crontabs),
            "next_run": None,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/render", response_model=dict, dependencies=[Depends(requires("crons.render"))])
def render_node_crontab(principal: Principal = Depends(get_principal)):
    """Materialize the enabled cron registry to the node crontab via admin-runner.

    Builds the cron.d body from sn_core.crons (enabled rows) with render_crontab,
    then hands it to the privileged admin-runner `materialize_crons` op, which
    validates (fail-closed), backs up the prior file + keeps a last-good, and
    atomically installs /etc/cron.d/smart-node-agents. core-api stays
    unprivileged and NEVER writes /etc/cron.d itself. SN_CRONTAB_OUT, if set, is
    written as a best-effort staging artifact only (never authoritative, never
    fatal). Node-wide => requires a cross-tenant (super-admin) role.
    """
    if not principal.cross_tenant:
        raise HTTPException(
            403,
            "rendering the node crontab requires a cross-tenant role, and registered jobs "
            f"do not need it: smart-node-cron-reconcile.timer renders every {RECONCILE_MINUTES} minutes",
        )

    with cursor(commit=True) as cur:
        cur.execute(
            'SELECT id, slug, schedule, command, "group", crontab_user, enabled, description '
            "FROM sn_core.crons WHERE enabled = true ORDER BY id"
        )
        rows = cur.fetchall()
        body = render_crontab(rows)

        # Best-effort staging artifact for debugging; never authoritative.
        out = os.environ.get("SN_CRONTAB_OUT")
        if out:
            try:
                p = Path(out)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(body)
            except OSError as e:
                log.warning("staging crontab write to %s failed (non-fatal): %s", out, e)

        # Authoritative install via the privileged runner (raises HTTPException
        # on validation/transport failure -> the live cron.d is left untouched).
        result = runner_op(principal.username, "materialize_crons", {"body": body})

        runner_result = result.get("result") or {}
        # Suppress the event when nothing happened: no enabled jobs and the
        # crontab file was not changed. A no-op confirmation has no diagnostic
        # value and obscures real signals in the journal.
        if not (len(rows) == 0 and runner_result.get("unchanged")):
            emit(
                cur, actor=principal.username, event_type="cron.rendered",
                entity_type="cron", entity_id="*",
                payload={"jobs": len(rows), "runner": runner_result},
            )

    return {"ok": True, "jobs": len(rows), "runner": result.get("result")}


@router.get("/{cron_id}", response_model=Cron, dependencies=[Depends(requires("crons.read"))])
def get_cron(cron_id: int):
    with cursor() as cur:
        cur.execute(f"SELECT {SELECT_COLS} FROM sn_core.crons WHERE id=%s", (cron_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"cron {cron_id} not found")
    return Cron(**row, materialized=is_materialized(row, read_installed()))


@router.post("", response_model=Cron, dependencies=[Depends(requires("crons.write"))])
def upsert_cron(payload: CronCreate, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM sn_core.crons "
            "WHERE schedule=%s AND command=%s AND crontab_user=%s",
            (payload.schedule, payload.command, payload.crontab_user),
        )
        r = cur.fetchone()
        existed = r is not None
        if existed:
            cron_id = r["id"]
            cur.execute(
                'UPDATE sn_core.crons SET '
                'script_path=%s, description=%s, "group"=%s, log_file=%s, '
                'enabled=%s, disabled_flag=%s, notes=%s, '
                'slug=COALESCE(%s, slug), updated_at=now() '
                'WHERE id=%s',
                (
                    payload.script_path, payload.description, payload.group,
                    payload.log_file, payload.enabled, payload.disabled_flag,
                    payload.notes, payload.slug, cron_id,
                ),
            )
        else:
            cur.execute(
                'INSERT INTO sn_core.crons '
                '(slug, schedule, command, script_path, description, "group", log_file, '
                ' crontab_user, enabled, disabled_flag, notes) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                (
                    payload.slug, payload.schedule, payload.command, payload.script_path,
                    payload.description, payload.group, payload.log_file,
                    payload.crontab_user, payload.enabled, payload.disabled_flag,
                    payload.notes,
                ),
            )
            cron_id = cur.fetchone()["id"]
            # Derive a stable slug when the caller did not supply one.
            if payload.slug is None:
                cur.execute(
                    "UPDATE sn_core.crons SET slug=%s WHERE id=%s AND slug IS NULL",
                    (f"cron-{cron_id}", cron_id),
                )
        emit(
            cur, actor=actor,
            event_type="cron.updated" if existed else "cron.registered",
            entity_type="cron", entity_id=str(cron_id),
            payload={"schedule": payload.schedule, "command": payload.command[:200]},
        )
        cur.execute(f"SELECT {SELECT_COLS} FROM sn_core.crons WHERE id=%s", (cron_id,))
        row = cur.fetchone()

    cron = Cron(**row, materialized=is_materialized(row, read_installed()), note=LIVE_NOTE)
    code = status.HTTP_200_OK if existed else status.HTTP_201_CREATED
    return JSONResponse(content=cron.model_dump(mode="json"), status_code=code)


@router.put("/{cron_id}", response_model=Cron, dependencies=[Depends(requires("crons.write"))])
def update_cron(cron_id: int, payload: CronUpdate, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    sets: list[str] = []
    params: list = []
    if payload.schedule is not None:
        sets.append("schedule=%s"); params.append(payload.schedule)
    if payload.command is not None:
        sets.append("command=%s"); params.append(payload.command)
    if payload.enabled is not None:
        sets.append("enabled=%s"); params.append(payload.enabled)
    if payload.description is not None:
        sets.append("description=%s"); params.append(payload.description)
    if payload.notes is not None:
        sets.append("notes=%s"); params.append(payload.notes)
    if payload.slug is not None:
        sets.append("slug=%s"); params.append(payload.slug)
    if not sets:
        raise HTTPException(400, "no fields to update")
    sets.append("updated_at=now()")
    params.append(cron_id)
    with cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE sn_core.crons SET {', '.join(sets)} WHERE id=%s RETURNING id",
            params,
        )
        if cur.fetchone() is None:
            raise HTTPException(404, f"cron {cron_id} not found")
        emit(
            cur, actor=actor, event_type="cron.updated",
            entity_type="cron", entity_id=str(cron_id), payload={},
        )
        cur.execute(f"SELECT {SELECT_COLS} FROM sn_core.crons WHERE id=%s", (cron_id,))
        row = cur.fetchone()
    return Cron(**row, materialized=is_materialized(row, read_installed()), note=LIVE_NOTE)


@router.delete("/{cron_id}", response_model=dict, dependencies=[Depends(requires("crons.write"))])
def delete_cron(cron_id: int, request: Request):
    actor = get_actor(request, request.headers.get("remote-user"))
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_core.crons WHERE id=%s RETURNING id", (cron_id,))
        row = cur.fetchone()
        if row is not None:
            emit(
                cur, actor=actor, event_type="cron.deleted",
                entity_type="cron", entity_id=str(cron_id), payload={},
            )
    if row is None:
        raise HTTPException(404, f"cron {cron_id} not found")
    return {"ok": True, "deleted": cron_id}


@router.get("/{cron_id}/runs", response_model=dict, dependencies=[Depends(requires("crons.runs.read"))])
def list_runs(cron_id: int, limit: int = 50, offset: int = 0):
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be 1..500")
    if offset < 0:
        raise HTTPException(400, "offset must be >= 0")
    with cursor() as cur:
        cur.execute(
            "SELECT id, cron_id, started_at, finished_at, exit_code, duration_ms, "
            "output_snippet, created_at "
            "FROM sn_core.cron_runs WHERE cron_id=%s "
            "ORDER BY started_at DESC LIMIT %s OFFSET %s",
            (cron_id, limit, offset),
        )
        rows = cur.fetchall()
    return {"runs": rows, "count": len(rows)}


@router.post("/{cron_id}/runs", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(requires("crons.runs.write"))])
def record_run(cron_id: int, payload: CronRunCreate):
    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM sn_core.crons WHERE id=%s", (cron_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, f"cron {cron_id} not found")
        cur.execute(
            "INSERT INTO sn_core.cron_runs "
            "(cron_id, started_at, finished_at, exit_code, duration_ms, output_snippet) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                cron_id, payload.started_at, payload.finished_at,
                payload.exit_code, payload.duration_ms, payload.output_snippet,
            ),
        )
        run_id = cur.fetchone()["id"]
    return {"id": run_id}

@router.post("/by-job/{job}/runs", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(requires("crons.runs.write"))])
def record_run_by_job(job: str, payload: CronRunCreate):
    """Record a cron_runs row using the cron-runner JOB slug.

    Resolves cron_id by the crons.slug (the stable job name written into the
    crontab by render-crontab). Falls back to the legacy command fuzzy-match
    for old crontab lines that predate the slug column.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM sn_core.crons WHERE slug=%s", (job,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT id FROM sn_core.crons "
                "WHERE command LIKE %s OR command LIKE %s OR command LIKE %s "
                "ORDER BY id LIMIT 1",
                (f"%/cron-runner.sh {job} %", f"%/{job}.sh", f"%/{job}.sh %"),
            )
            row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"no cron matches job '{job}'")
        cron_id = row["id"]
        cur.execute(
            "INSERT INTO sn_core.cron_runs "
            "(cron_id, started_at, finished_at, exit_code, duration_ms, output_snippet) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                cron_id, payload.started_at, payload.finished_at,
                payload.exit_code, payload.duration_ms, payload.output_snippet,
            ),
        )
        run_id = cur.fetchone()["id"]
    return {"id": run_id, "cron_id": cron_id}


def _resolve_cron_id_by_job(cur, job: str) -> int:
    """Resolve a cron_id from the cron-runner JOB slug (slug first, then legacy
    command fuzzy-match). Raises 404 if nothing matches."""
    cur.execute("SELECT id FROM sn_core.crons WHERE slug=%s", (job,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "SELECT id FROM sn_core.crons "
            "WHERE command LIKE %s OR command LIKE %s OR command LIKE %s "
            "ORDER BY id LIMIT 1",
            (f"%/cron-runner.sh {job} %", f"%/{job}.sh", f"%/{job}.sh %"),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no cron matches job '{job}'")
    return row["id"]


@router.post("/by-job/{job}/runs/start", response_model=dict, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(requires("crons.runs.write"))])
def start_run_by_job(job: str):
    """Open a cron_runs row (status = BUSY) and return its run_id.

    The cron-runner wrapper calls this at START, then passes the run_id to the
    matching /runs/{id}/finish at END. An open row (finished_at IS NULL) is the
    DB mirror of the wrapper's flock -- the queryable 'agent is running' signal.
    """
    with cursor(commit=True) as cur:
        cron_id = _resolve_cron_id_by_job(cur, job)
        cur.execute(
            "INSERT INTO sn_core.cron_runs (cron_id, started_at) VALUES (%s, now()) RETURNING id",
            (cron_id,),
        )
        run_id = cur.fetchone()["id"]
        emit(
            cur, actor=f"cron:{job}", event_type="cron.started",
            entity_type="cron", entity_id=job, payload={"run_id": run_id, "cron_id": cron_id},
        )
    return {"run_id": run_id, "cron_id": cron_id}


@router.post("/runs/{run_id}/finish", response_model=dict, dependencies=[Depends(requires("crons.runs.write"))])
def finish_run(run_id: int, payload: CronRunFinish):
    """Close an open cron_runs row (status = FREE) with the run outcome.

    Called by the cron-runner wrapper at END -- which always regains control
    even when the wrapped agent times out or crashes, so the BUSY->FREE
    transition is reliable (run.sh self-reporting would dangle on crash)."""
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sn_core.cron_runs SET finished_at = now(), exit_code = %s, "
            "duration_ms = %s, output_snippet = %s "
            "WHERE id = %s AND finished_at IS NULL RETURNING cron_id",
            (payload.exit_code, payload.duration_ms, payload.output_snippet or "", run_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"open run {run_id} not found (already finished or absent)")
        cron_id = row["cron_id"]
        ev = "cron.failed" if (payload.exit_code or 0) != 0 else "cron.completed"
        emit(
            cur, actor="cron", event_type=ev, entity_type="cron",
            entity_id=str(cron_id),
            payload={"run_id": run_id, "exit_code": payload.exit_code, "duration_ms": payload.duration_ms},
        )
    return {"ok": True, "run_id": run_id, "cron_id": cron_id}
