"""Seed sn_config.specialists from the agnostic specialist files shipped in core.

Files live next to the agent template (e.g. /srv/agents/_specialists/*.md) and
are the INITIAL population only. After install the database is the source of
truth and the panel edits the rows; this seed is idempotent (skips a slug that
already exists). The installer's explicit --refresh-configurator replaces only
the release-owned configurator body; other edited specialists are preserved.

Each file is `<slug>.md` with YAML front-matter:

    ---
    slug: sysadmin
    name: System Administrator
    archetype: specialist
    ---
    <markdown body = the specialist prompt>

Directory comes from SN_SPECIALISTS_SEED (no fallback -- fail loud if unset).
DB connection uses libpq env (PGHOST/PGPASSWORD/...), same as the app.
"""

import argparse
import logging
import os
from pathlib import Path

import psycopg2
import yaml

log = logging.getLogger("seed-specialists")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ALLOWED_ARCHETYPES = {"head", "specialist", "reviewer", "autonomous", "shared"}


def parse_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path.name}: missing YAML front-matter")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    slug = meta.get("slug")
    name = meta.get("name")
    archetype = meta.get("archetype", "specialist")
    if not slug or not name:
        raise ValueError(f"{path.name}: front-matter needs slug + name")
    if archetype not in ALLOWED_ARCHETYPES:
        raise ValueError(f"{path.name}: bad archetype {archetype!r}")
    return {"slug": slug, "name": name, "archetype": archetype, "body_md": body.strip() + "\n"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-configurator", action="store_true",
                        help="installer only: refresh the release-owned configurator body")
    args = parser.parse_args()
    seed_dir = Path(os.environ["SN_SPECIALISTS_SEED"])
    if not seed_dir.is_dir():
        raise SystemExit(f"seed dir not found: {seed_dir}")

    specs = [parse_file(p) for p in sorted(seed_dir.glob("*.md"))]
    log.info("found %d specialist seed files in %s", len(specs), seed_dir)

    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    inserted = skipped = 0
    try:
        with conn, conn.cursor() as cur:
            for s in specs:
                # Idempotent: skip a slug that already exists and is live.
                cur.execute(
                    "SELECT 1 FROM sn_config.specialists "
                    "WHERE slug=%s AND archived_at IS NULL",
                    (s["slug"],),
                )
                if cur.fetchone():
                    # The configurator is the node's release-owned setup contract.
                    # Refresh it only during install/update, never on an ordinary
                    # service restart. Other specialist bodies remain user state.
                    if args.refresh_configurator and s["slug"] == "configurator":
                        cur.execute(
                            "UPDATE sn_config.specialists "
                            "SET body_md=%s, updated_by='installer', updated_at=now() "
                            "WHERE slug=%s AND archived_at IS NULL "
                            "AND body_md IS DISTINCT FROM %s",
                            (s["body_md"], s["slug"], s["body_md"]),
                        )
                        log.info("configurator: %d body refreshed", cur.rowcount)
                    skipped += 1
                    continue
                cur.execute(
                    "INSERT INTO sn_config.specialists "
                    "(slug, name, body_md, archetype, created_by, updated_by) "
                    "VALUES (%s, %s, %s, %s, 'seed', 'seed')",
                    (s["slug"], s["name"], s["body_md"], s["archetype"]),
                )
                inserted += 1
        log.info("seed-specialists: %d inserted, %d already present", inserted, skipped)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
