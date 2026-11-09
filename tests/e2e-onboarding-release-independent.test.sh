#!/usr/bin/env bash
#
# The onboarding e2e scenario does not depend on a release number.
#
# It used to demand a baseline captured on exactly 1.12.47 and a pinned hash of the
# configurator source, so a full e2e run needed a stand built from a release that
# had been superseded many times over. It now compares the stand before and after
# the update against the configurator source of the checkout under test.
#
# WHAT IS CHECKED. tests/e2e/release_candidate.py, onboarding(), with the node-only
# parts (the database read, the release stamp, /srv/agents) replaced by fixtures:
#
#   passes     an update delivered this checkout's body, identities kept
#   passes     a baseline that already had the body: noted, not failed
#   fails      the database still holds the old body
#   fails      the agent's CLAUDE.md still holds the old body
#   fails      the baseline was captured after the update (same commit)
#   fails      the agent or specialist identity changed
#   fails      no baseline file was named
#
# Nothing on the host is touched.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

python3 -I - <<'PY'
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("rc", "tests/e2e/release_candidate.py")
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

real_path = Path
tmp = real_path(tempfile.mkdtemp())
source = (rc.REPO / "companions/srv-skeleton/agents/_specialists/configurator.md").read_text()
expected = source.split("---", 2)[2].strip() + chr(10)
OLD = "old instructions" + chr(10)


def fake_path(*parts):
    if parts and parts[0] == "/srv/agents":
        return real_path(tmp, *parts[1:])
    return real_path(*parts)


rc.Path = fake_path
failed = []


def scenario(name, want_pass, db_body=expected, prompt_body=expected, base_commit="old1",
             base_body=OLD, ids_same=True, with_env=True):
    ident = {"id": 7, "slug": "john", "specialist_id": 3}
    base = dict(ident, title="John", release="1.12.1", commit=base_commit,
                body_sha256=hashlib.sha256(base_body.encode()).hexdigest())
    if not ids_same:
        base["specialist_id"] = 99
    bfile = tmp / "baseline.json"
    bfile.write_text(json.dumps(base))
    (tmp / "john").mkdir(exist_ok=True)
    (tmp / "john" / "CLAUDE.md").write_text("preamble" + chr(10) + prompt_body)
    rc.john = lambda: dict(ident, title="John", body_md=db_body)
    rc.installed_release = lambda: {"release": "1.13.0", "commit": "new2"}
    if with_env:
        os.environ["E2E_ONBOARDING_BASELINE"] = str(bfile)
    else:
        os.environ.pop("E2E_ONBOARDING_BASELINE", None)
    try:
        rc.onboarding()
        passed = True
    except AssertionError:
        passed = False
    ok = passed == want_pass
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        failed.append(name)


scenario("an update delivered this checkout's body", True)
scenario("a baseline that already had the body is noted, not failed", True, base_body=expected)
scenario("database still holds the old body", False, db_body=OLD)
scenario("CLAUDE.md still holds the old body", False, prompt_body=OLD)
scenario("baseline captured after the update", False, base_commit="new2")
scenario("identity changed", False, ids_same=False)
scenario("no baseline named", False, with_env=False)
sys.exit(1 if failed else 0)
PY
echo "PASS: onboarding e2e scenario is release independent"
