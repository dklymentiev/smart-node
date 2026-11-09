"""Render /srv/tools/REGISTRY.md from sn_core.tools (DB -> filesystem projection).

Output path from SN_TOOLS_OUT (no fallback -- fail loud). DB via libpq env.

    SN_TOOLS_OUT=/srv/tools/REGISTRY.md python companions/render-tools.py
"""

import logging
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.tools_index import render_tools_index  # noqa: E402

log = logging.getLogger("render-tools")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

SELECT = (
    "SELECT name, kind, title, description, status, integration_slug "
    "FROM sn_core.tools ORDER BY integration_slug, name"
)


def main():
    out = Path(os.environ["SN_TOOLS_OUT"])
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SELECT)
            rows = cur.fetchall()
    finally:
        conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_tools_index(rows))
    log.info("rendered %d tools -> %s", len(rows), out)


if __name__ == "__main__":
    main()
