#!/usr/bin/env bash
#
# run-source.sh -- run every tests/*.test.sh against the working tree.
#
# These tests read source from the git repository (git show REF:path) and
# need the repository, not a live node. This is the source-level regression
# runner; for on-node integration tests see tests/run-all.sh.
#
# Used as a gate by tools/release.sh. Exit 0 = all pass, 1 = any fail,
# 2 = no failures but some tests could not run on this machine.
#
# WHY THIS EXISTS. The source-level tests were written to catch regressions
# on commit. Nothing invoked them -- not a cron, not the release path. So a
# later edit to a guarded property would reintroduce the bug and every gate
# would stay green. This closes the hole at the cheapest moment: in the
# repository, before a tag.
#
#   tests/run-source.sh         run all source tests against working tree
#   tests/run-source.sh --list  print the tests that would run; exit 0
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

shopt -s nullglob
tests=("$HERE"/*.test.sh)
shopt -u nullglob

if [[ "${1:-}" == "--list" ]]; then
    for t in "${tests[@]}"; do printf '%s\n' "$(basename "$t")"; done
    exit 0
fi

if [[ ${#tests[@]} -eq 0 ]]; then
    echo "[run-source] no tests/*.test.sh found" >&2
    exit 0
fi

# The tests import the product's own modules (fastapi, httpx, psycopg2, requests) and some use
# pytest. A system python3 on a node has none of them, and the tests then fail on an import,
# not on the code. Use SN_TEST_PYTHON if given, else the first of python3 and the usual venvs
# that has all of them, and put its directory first on PATH for every test below.
NEED_MODULES='import fastapi, httpx, psycopg2, requests, pytest'
pick_python() {
    local c
    for c in "${SN_TEST_PYTHON:-}" python3 "${SN_TEST_VENV:+$SN_TEST_VENV/bin/python}" \
             "$HOME/.venvs/smart-node-pytest/bin/python"; do
        [[ -n "$c" ]] && "$c" -c "$NEED_MODULES" 2>/dev/null && { command -v "$c" || echo "$c"; return 0; }
    done
    return 1
}
if py="$(pick_python)"; then
    # The venv's own bin directory, not a symlink to its python: a venv python only finds
    # its packages when it is started from inside the venv.
    PATH="$(dirname "$py"):$PATH"; export PATH
else
    echo "[run-source] no python has fastapi, httpx, psycopg2, requests and pytest; tests that import them will fail (set SN_TEST_PYTHON)" >&2
fi

fail=0
cannot=()

echo "[run-source] source-level regression tests (${#tests[@]} test(s), working tree)"
echo

# Exit 2 is the tests' "cannot run here" (a missing venv, initdb, a /work checkout). It is
# reported apart from a failure so a contributor can tell a broken change from a missing
# tool, and it still keeps the run from reading as a pass.
for t in "${tests[@]}"; do
    name="$(basename "$t")"
    echo "=== $name ==="
    bash "$t"; rc=$?
    if [[ $rc -eq 0 ]]; then
        echo "  [run-source] $name: ok"
    elif [[ $rc -eq 2 ]]; then
        echo "  [run-source] $name: CANNOT RUN here (see above)"
        cannot+=("$name")
    else
        echo "  [run-source] $name: FAIL"
        fail=1
    fi
    echo
done

if [[ "$fail" -ne 0 ]]; then
    echo "[run-source] FAILURES (see above)"
    exit 1
fi
if [[ ${#cannot[@]} -gt 0 ]]; then
    echo "[run-source] no failures, but ${#cannot[@]} test(s) could not run here: ${cannot[*]}"
    exit 2
fi
echo "[run-source] ALL PASS"
exit 0
