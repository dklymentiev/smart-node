#!/usr/bin/env bash
#
# Every flow in the tree lints clean, and the tests each flow carries pass.
#
# The work is done by companions/flows/tools/flow-test.sh, which ships to the
# node so an owner can run the same check there. This is the repo's handle on it,
# so a flow that stops behaving fails the suite here as well.
#
#   tests/flows.test.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# The stub runs drive the real Rein engine; without its python the flows cannot run here.
REIN_PY="${SN_REIN_PY:-/opt/smart-node/workflows/venv/bin/python}"
command -v "$REIN_PY" >/dev/null || { echo "cannot run: no Rein engine python at $REIN_PY (set SN_REIN_PY and SN_REIN_CWD)" >&2; exit 2; }
exec "$REPO/companions/flows/tools/flow-test.sh" --quiet
