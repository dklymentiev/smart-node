"""Postgres pool. Peer auth via pg_ident map (OS user -> pg role); no password.
Mirrors core-api/db.py so the two services read alike.
"""
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

_pool: ThreadedConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    _pool = ThreadedConnectionPool(1, 8, dbname=config.DB_NAME, user=config.DB_USER)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def _live_conn():
    """A pooled connection that still reaches the server.

    The pool keeps connections open between requests. When the cluster is
    restarted under it -- the documents stage does that during an install --
    every pooled connection is dead, and the next request failed with "server
    closed the connection unexpectedly": on a clean install that was the
    request creating the first agent, so the node came up with none. A dead
    connection is closed and replaced; a server that is really down still
    raises, from the fresh connect.
    """
    conn = _pool.getconn()
    try:
        with conn.cursor() as probe:
            probe.execute("SELECT 1")
        conn.rollback()
        return conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        _pool.putconn(conn, close=True)
        return _pool.getconn()


@contextmanager
def cursor(commit: bool = False):
    if _pool is None:
        init_pool()
    conn = _live_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
            if commit:
                conn.commit()
    except Exception:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        _pool.putconn(conn, close=bool(conn.closed))
