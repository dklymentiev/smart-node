"""Postgres connection pool. Peer auth via pg_ident map: OS user
'smart-node-core-api' -> pg user 'smartnode'. No password in config.
"""

import logging
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool, PoolError

log = logging.getLogger("core-api.db")

DB_NAME = "smartnode"

# maxconn must comfortably exceed request concurrency: FastAPI runs sync `def`
# handlers in a ~40-thread pool, so a small pool yields "connection pool
# exhausted" 500s under bursts (the 2026-06-17 wake-storm hit this). PG
# max_connections=100 with few other consumers, so 32 is safe. getconn also
# retries briefly instead of failing instantly -> a transient spike waits.
_POOL_MAXCONN = 32
_GETCONN_RETRIES = 20
_GETCONN_DELAY = 0.1

_pool: ThreadedConnectionPool | None = None


def init_pool():
    global _pool
    if _pool is not None:
        return
    _pool = ThreadedConnectionPool(
        minconn=1,
        maxconn=_POOL_MAXCONN,
        dbname=DB_NAME,
        user="smartnode",  # pg user via pg_ident map; OS user smart-node-core-api -> smartnode
    )
    log.info("postgres pool initialised (db=%s, maxconn=%d)", DB_NAME, _POOL_MAXCONN)


def close_pool():
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def _getconn():
    """Get a pooled connection, briefly retrying on transient exhaustion instead
    of raising immediately (turns a momentary concurrency spike into a short wait)."""
    for attempt in range(_GETCONN_RETRIES):
        try:
            return _pool.getconn()
        except PoolError:
            if attempt == _GETCONN_RETRIES - 1:
                raise
            time.sleep(_GETCONN_DELAY)


@contextmanager
def cursor(commit: bool = False):
    if _pool is None:
        init_pool()
    conn = _getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
            if commit:
                conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
