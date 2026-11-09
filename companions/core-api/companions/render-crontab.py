"""Render /etc/cron.d/smart-node-* from sn_core.crons (enabled jobs).

Run by the cron-runner companion (and on demand by the panel after a cron
edit). Output path comes from SN_CRONTAB_OUT (no fallback -- fail loud).
DB connection uses libpq env, same as the app.

    SN_CRONTAB_OUT=/etc/cron.d/smart-node-agents python companions/render-crontab.py
"""

import logging
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.crontab import render_crontab  # noqa: E402

log = logging.getLogger("render-crontab")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

SELECT = (
    'SELECT id, slug, schedule, command, "group", crontab_user, enabled, description '
    "FROM sn_core.crons WHERE enabled = true ORDER BY id"
)


def main():
    out = Path(os.environ["SN_CRONTAB_OUT"])
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT)
            rows = cur.fetchall()
    finally:
        conn.close()
    body = render_crontab(rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    log.info("rendered %d cron jobs -> %s", len(rows), out)


if __name__ == "__main__":
    main()
