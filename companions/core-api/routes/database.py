"""Database browser (admin-only, read-only).

GET /api/database/tables                  -- list BASE TABLEs in the core schemas
GET /api/database/table/{schema}/{name}   -- columns + first N rows of one table

super-admin only. Raw data exposure, so it is gated on role and the schema is
allow-listed; the table name is validated against information_schema before it
is ever interpolated into a query (no SQL injection surface).
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import Principal, get_principal
from db import cursor
from permissions import requires

log = logging.getLogger("core-api.database")
router = APIRouter()

ALLOWED_SCHEMAS = ("sn_config", "sn_core", "public")


def _require_admin(p: Principal) -> None:
    if p.role != "super-admin":
        raise HTTPException(status_code=403, detail="super-admin only")


def _jsonsafe(v):
    """Make a raw psycopg2 value JSON-serialisable for the viewer."""
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (Decimal, UUID)):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<binary " + str(len(bytes(v))) + " bytes>"
    return str(v)


@router.get("/api/database/tables",
            dependencies=[Depends(requires("database.read"))])
def list_tables(principal: Principal = Depends(get_principal)):
    _require_admin(principal)
    with cursor() as cur:
        cur.execute(
            """
            SELECT t.table_schema AS schema,
                   t.table_name   AS name,
                   COALESCE(c.reltuples, 0)::bigint AS est_rows
            FROM information_schema.tables t
            LEFT JOIN pg_namespace n ON n.nspname = t.table_schema
            LEFT JOIN pg_class c ON c.relname = t.table_name AND c.relnamespace = n.oid
            WHERE t.table_schema = ANY(%s) AND t.table_type = 'BASE TABLE'
            ORDER BY t.table_schema, t.table_name
            """,
            (list(ALLOWED_SCHEMAS),),
        )
        rows = cur.fetchall()
    return {"tables": [dict(r) for r in rows]}


# Time columns that mean "when this row happened", in order of preference.
_NEWEST_FIRST = ("created_at", "received_at", "ts", "inserted_at", "started_at", "updated_at")
# Above this many rows (by the planner's estimate) the total is not counted exactly.
_EXACT_COUNT_MAX = 200_000


@router.get("/api/database/table/{schema}/{name}",
            dependencies=[Depends(requires("database.read"))])
def table_rows(
    schema: str,
    name: str,
    limit: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
):
    _require_admin(principal)
    if schema not in ALLOWED_SCHEMAS:
        raise HTTPException(status_code=400, detail="schema not allowed")
    with cursor() as cur:
        # validate the table exists in an allowed schema BEFORE interpolating
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s AND table_type = 'BASE TABLE'",
            (schema, name),
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="table not found")
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, name),
        )
        columns = [r["column_name"] for r in cur.fetchall()]

        # Show the NEWEST rows, not whatever the heap returns first: sort by the
        # first time column the table has, else by a single-column primary key.
        order_by = next((c for c in _NEWEST_FIRST if c in columns), None)
        if order_by is None:
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = (quote_ident(%s) || '.' || quote_ident(%s))::regclass "
                "AND i.indisprimary AND array_length(i.indkey, 1) = 1",
                (schema, name),
            )
            pk = cur.fetchone()
            order_by = pk["attname"] if pk else None

        # How many rows in total. An exact count(*) on a large table is a full
        # scan on every click, so past a threshold report the planner's estimate
        # and say it is one.
        cur.execute(
            "SELECT c.reltuples::bigint AS est FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            (schema, name),
        )
        est = int((cur.fetchone() or {}).get("est") or -1)
        if est >= _EXACT_COUNT_MAX:   # est is -1 when the table was never analysed: count it
            total, total_estimated = est, True
        else:
            cur.execute('SELECT count(*) AS n FROM "%s"."%s"' % (schema, name))
            total, total_estimated = int(cur.fetchone()["n"]), False

        # schema + name + order_by come from the catalog above, never from the
        # request as such; quote the identifiers and interpolate. LIMIT is bound.
        order_sql = ' ORDER BY "%s" DESC NULLS LAST' % order_by if order_by else ""
        cur.execute('SELECT * FROM "%s"."%s"%s LIMIT %%s' % (schema, name, order_sql), (limit,))
        raw = cur.fetchall()
    rows = [{k: _jsonsafe(v) for k, v in dict(r).items()} for r in raw]
    return {"schema": schema, "table": name, "columns": columns, "rows": rows, "count": len(rows),
            "limit": limit, "order_by": order_by, "total": total, "total_estimated": total_estimated}
