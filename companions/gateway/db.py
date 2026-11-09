"""Database access for AI Gateway -- Smart Node session-first foundation.

Connects to the smartnode postgres database via local socket + peer auth
(unix user smart-node-gateway -> postgres role smartnode via pg_ident map).

The DSN is configured in /opt/smart-node/gateway/.env as SMARTNODE_DB_DSN
(loaded by python-dotenv at app startup).

This module provides raw database access for session orchestration.
Higher-level Session helpers live in session_service.py; this module owns connections + raw
SQL helpers.

Conventions:
    - psycopg2 (matches existing companion convention -- communication/common/db.py)
    - Autocommit by default; explicit transactions via with_transaction()
    - Caller closes connections via context manager
"""
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import Json, RealDictCursor

DSN = os.environ.get("SMARTNODE_DB_DSN", "")
if not DSN:
    raise RuntimeError(
        "SMARTNODE_DB_DSN env var missing -- add to /opt/smart-node/gateway/.env"
    )


@contextmanager
def get_connection(autocommit=True):
    """Yield a fresh psycopg2 connection. Caller doesn't manage close."""
    conn = psycopg2.connect(DSN)
    try:
        conn.autocommit = autocommit
        yield conn
    finally:
        conn.close()


def query(sql, args=None, fetch=False, as_dict=False):
    """One-shot SQL on a fresh autocommit connection.

    Args:
        sql: query string with %s placeholders
        args: tuple/list of values, or None
        fetch: True to return rows; False (default) for write statements
        as_dict: True returns RealDictRow rows; False returns tuples

    Returns:
        list of rows if fetch else None
    """
    cursor_factory = RealDictCursor if as_dict else None
    with get_connection(autocommit=True) as conn:
        cur = conn.cursor(cursor_factory=cursor_factory)
        cur.execute(sql, args or ())
        return cur.fetchall() if fetch else None


@contextmanager
def with_transaction():
    """Explicit transaction context. Commits on success, rolls back on exception."""
    with get_connection(autocommit=False) as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# Smoke check at import time -- catches DSN typos early without crashing app.
def _smoke():
    """Verify connectivity. Returns (current_user, server_version) or raises."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_user, current_database(), version()")
        return cur.fetchone()


if __name__ == "__main__":
    user, db, ver = _smoke()
    print(f"OK -- connected as {user} to db={db}")
    print(f"server: {ver}")
    sessions = query("SELECT count(*) FROM sn_core.ai_sessions", fetch=True)
    pricing = query("SELECT count(*) FROM sn_config.model_pricing", fetch=True)
    print(f"sn_core.ai_sessions:    {sessions[0][0]} rows")
    print(f"sn_config.model_pricing: {pricing[0][0]} rows")
