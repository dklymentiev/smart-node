#!/usr/bin/env python3
"""classes-verify.py -- does this node still look like its own declaration?

/etc/smart-node/classes.json says where every class of thing lives on a node of
this release. This asks the node whether that is still true, and says where it
is not.

It exists because the declaration was wrong three times in one day and nobody
noticed: the Schedule class pointed at sn_config.crons (no such table), Desk at
sn_config.desks (no such table), and Workflow at sn_config.flows -- an EMPTY
table sitting beside the live sn_core.workflows. A same-named empty twin answers
every existence check, which is why this checks more than existence.

WHAT IT CHECKS, in order, because the order is the point:

  0. The stamp. classes.json carries the release it shipped with. If that does
     not match /etc/smart-node/release, every later check is comparing this node
     against a different release's truth, and a PASS would be a lie. This is a
     hard failure and it comes first.
  1. Every table registry exists.
  2. No registry is the twin of a live table (the sn_config.flows shape). Any
     table name that exists in more than one schema and is not declared in
     legacy_duplicates is reported: an undeclared twin is how the last three
     defects happened.
  3. Every file registry exists, is not empty, and -- for the sops store -- is
     actually encrypted rather than a plaintext file at the right path.
  4. Service registries name another component's database; it either exists on
     this cluster or it does not, and saying so is better than declaring the
     class unverifiable.
  5. A class marked `gap` must NOT have acquired a registry quietly.

Runs on the node host, so: standard library only, and postgres is reached
through psql as the postgres user. There is no psycopg on a host.

Exit 0 clean, 1 findings, 2 could not run (missing file, no psql). A missing
declaration is exit 2, never a quiet 0 -- "nothing to check" is not "OK".
"""

import json
import os
import subprocess
import sys

CLASSES = os.environ.get("SN_CLASSES_FILE", "/etc/smart-node/classes.json")
RELEASE = os.environ.get("SN_RELEASE_FILE", "/etc/smart-node/release")
# the installer records each installed module here, one <name>.json
COMPANIONS = os.environ.get("SN_COMPANIONS_DIR", "/etc/smart-node/companions.d")
DB = os.environ.get("SN_DB", "smartnode")

findings = []
checked = 0


def finding(kind, detail):
    findings.append((kind, detail))


def psql(sql, db=DB):
    """One value per line, tab separated. Returns None when psql itself fails."""
    try:
        r = subprocess.run(
            ["sudo", "-n", "-u", "postgres", "psql", "-tA", "-F", "\t", "-d", db, "-c", sql],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.strip().splitlines() if ln]


def main():
    global checked

    if not os.path.isfile(CLASSES):
        print(f"CANNOT RUN  {CLASSES} is not there.")
        print("            A node with no declaration cannot be compared with one.")
        return 2
    try:
        with open(CLASSES, encoding="utf-8") as f:
            decl = json.load(f)
    except (OSError, ValueError) as e:
        print(f"CANNOT RUN  {CLASSES} is unreadable: {e}")
        return 2

    # --- 0. the stamp, before anything is compared -----------------------
    stamped = str(decl.get("release", "unstamped"))
    running = "unknown"
    if os.path.isfile(RELEASE):
        with open(RELEASE, encoding="utf-8") as f:
            for line in f:
                if line.startswith("release="):
                    running = line.split("=", 1)[1].strip()
                    break
    if stamped in ("unstamped", ""):
        finding("STAMP MISSING",
                "classes.json says no release; it was shipped without being stamped")
    elif running == "unknown":
        finding("STAMP UNCOMPARABLE",
                f"{RELEASE} does not say what release this node runs")
    elif stamped != running:
        finding("STAMP MISMATCH",
                f"classes.json describes {stamped}, this node runs {running} -- "
                "everything below compares the node with another release")
    checked += 1

    if any(k.startswith("STAMP") for k, _ in findings):
        report(decl)
        return 1

    if psql("SELECT 1") is None:
        print("CANNOT RUN  psql as the postgres user did not answer")
        return 2

    # --- 1 & 5. registries ------------------------------------------------
    for c in decl.get("classes", []):
        name, kind = c.get("class"), c.get("kind")
        reg, status = c.get("registry"), c.get("status")

        if status == "gap":
            checked += 1
            if reg:
                finding("GAP ACQUIRED A HOME",
                        f"{name} is declared a gap and now names {reg}")
            continue

        if kind == "table":
            checked += 1
            out = psql(f"SELECT to_regclass('{reg}') IS NOT NULL")
            if out is None or out[0] != "t":
                finding("REGISTRY MISSING", f"{name} -> {reg} does not exist")

        elif kind == "file":
            checked += 1
            if not os.path.isfile(reg):
                finding("FILE MISSING", f"{name} -> {reg} is not there")
            elif os.path.getsize(reg) == 0:
                finding("FILE EMPTY", f"{name} -> {reg} exists and is empty")
            elif reg.endswith(".sops.yaml"):
                # A path check passes on a plaintext file sitting in the right
                # place, which is the failure that matters for a secret store.
                try:
                    with open(reg, encoding="utf-8", errors="replace") as f:
                        head = f.read(4096)
                    if "sops:" not in head and "ENC[" not in head:
                        finding("FILE NOT ENCRYPTED",
                                f"{name} -> {reg} carries no sops marker")
                except OSError as e:
                    finding("FILE UNREADABLE", f"{name} -> {reg}: {e}")

        elif kind == "service":
            # A service class lives in a module (its "component"); on a node
            # without that module there is no store to find, and its absence
            # is the node's choice, not drift (modular core, 2026-10-30).
            comp = c.get("component")
            if comp and not os.path.isfile(os.path.join(COMPANIONS, comp + ".json")):
                continue
            checked += 1
            out = psql(f"SELECT 1 FROM pg_database WHERE datname = '{reg}'")
            if not out:
                finding("SERVICE STORE MISSING",
                        f"{name} -> database '{reg}' is not on this cluster")

    # --- 2. undeclared twins ---------------------------------------------
    declared_twins = {d.get("twin") for d in decl.get("legacy_duplicates", [])}
    declared_homes = {d.get("home") for d in decl.get("legacy_duplicates", [])}
    rows = psql(
        "SELECT table_name, string_agg(table_schema, ',' ORDER BY table_schema) "
        "FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' "
        "AND table_schema IN ('public','sn_core','sn_config') "
        "GROUP BY table_name HAVING count(*) > 1"
    ) or []
    for row in rows:
        checked += 1
        name, schemas = (row.split("\t") + [""])[:2]
        pair = {f"{s}.{name}" for s in schemas.split(",") if s}
        if pair & declared_twins and pair & declared_homes:
            continue
        finding("UNDECLARED TWIN",
                f"{name} exists in {schemas} and legacy_duplicates does not say "
                "which is the home")

    report(decl)
    return 1 if findings else 0


def report(decl):
    stamped = decl.get("release", "unstamped")
    if not findings:
        print(f"OK  {checked} checks, release {stamped}: the node matches its declaration")
        return
    print(f"FINDINGS  {len(findings)} of {checked} checks, declaration release {stamped}\n")
    for kind, detail in findings:
        print(f"  {kind:<22} {detail}")
    print("\nA declaration that disagrees with the node is not a small thing: it is what")
    print("an agent reads to decide where something lives.")


if __name__ == "__main__":
    sys.exit(main())
