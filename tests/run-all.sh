#!/usr/bin/env bash
# Smart Node test runner -- the cross-repo "smoke/regression" entry point.
#
# Two levels (see tests/README):
#   1. per-repo unit tests -- each component repo owns its tests/ (pytest). On the
#      node, group-A components are live git working copies under /opt/smart-node,
#      so we run them in place. (CI runs the same per repo on push.)
#   2. cross-node e2e -- integration scenarios that need a running node
#      (tests/e2e/*.sh), e.g. the agent-to-agent comms round-trip.
#
# Run before and after each deploy. Exit 0 = all green, 1 = any failure.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${SN_ROOT:-/opt/smart-node}"
fail=0

echo "== unit tests (per-repo, where present) =="
# extend this list as tests land in each repo
# A NODE CARRIES THE TESTS AND NOT THE RUNNER, AND THAT IS NOT A FAILURE.
# The component venvs are built with what the component needs to RUN; pytest is
# in none of them on any node, so this loop failed on every node it has ever
# been executed on, and with it the whole suite and the publication gate behind
# it. Unit tests are CI's job, per this file's own header. What a node can
# honestly say is "not measured here", and it must SAY it rather than either
# failing or going quiet -- so the skip is printed and counted, and the count
# is visible in the evidence run-e2e.sh writes.
skipped_units=0
for c in gateway dispatcher admin-runner core-api communication; do
  d="$ROOT/$c"
  if [ -d "$d/tests" ]; then
    echo "-- $c --"
    if [ -x "$d/venv/bin/python" ]; then
      py="$d/venv/bin/python"
    else
      py="python3"
    fi
    if ! ( cd "$d" && "$py" -m pytest --version ) >/dev/null 2>&1; then
      echo "   SKIP: no pytest in this component's runtime -- unit tests run in CI, not on a node"
      skipped_units=$((skipped_units + 1))
      continue
    fi
    ( cd "$d" && "$py" -m pytest -q tests ) || fail=1
  else
    echo "-- $c: no tests/ yet (TODO -- add unit tests in canon) --"
  fi
done
[ "$skipped_units" -gt 0 ] && echo "== unit tests: $skipped_units component(s) skipped, no runner on this node =="

echo
echo "== e2e smoke (live node) =="
for t in "$HERE"/e2e/*.sh; do
  [ -e "$t" ] || continue
  echo "-- $(basename "$t") --"
  bash "$t" || fail=1
done

echo "================================"
if [ "$fail" = 0 ]; then echo "ALL GREEN"; else echo "FAILURES (see above)"; fi
exit $fail
