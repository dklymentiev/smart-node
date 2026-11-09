#!/usr/bin/env bash
# The gateway owns the lifetime of the CLI children it spawns: an idle one is
# reclaimed, one with a turn in flight is spared and kept active for the session
# reaper, and every reclaim is journaled. Real child processes, no model.
#
#   tests/gateway-cli-child-lifetime.test.sh                  # tree, PASS
#   tests/gateway-cli-child-lifetime.test.sh --baseline REF   # REF must FAIL
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/companions/gateway/tests/e2e_cli_child_lifetime.py" "$@"
