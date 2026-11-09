#!/usr/bin/env bash
# The source release must carry the private bridge, delivery worker, and the
# optional installer entrypoint together. The Python tests exercise identity
# and retry semantics without a live node.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
bash -n "$repo/companions/hitl.sh" "$repo/orchestrator/install-on.sh"
python3 -m unittest discover -s "$repo/companions/hitl/tests" -q
grep -q 'run_companion hitl "hitl-upstream hitl _shared"' "$repo/orchestrator/install-on.sh"
! rg -q 'recipient_actor_id' "$repo/companions/planner/src/planner"
! rg -q 'external-wait|external-resume' "$repo/companions/hitl/mcp_hitl.py" "$repo/companions/hitl/deliver.py"
grep -q 'User=\$DELIVERY_USER' "$repo/companions/hitl.sh"
grep -q 'HITL_QUEUE=' "$repo/companions/hitl.sh"
# The bridge checks delivered/<id>.json, so it must be able to traverse that
# directory without being able to list or write it.
grep -q 'usermod -aG "\$DELIVERY_GROUP" "\$SERVICE_USER"' "$repo/companions/hitl.sh"
grep -q 'install -d -m 2710 -o "\$DELIVERY_USER" -g "\$DELIVERY_GROUP" "\$QUEUE/delivered"' "$repo/companions/hitl.sh"
grep -q 'requires("hitl.delivery")' "$repo/companions/core-api/routes/hitl.py"
python3 - "$repo/companions/hitl.sh" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
guard = source.index('existing HITL installation detected')
stop = source.index('systemctl stop "$UNIT"')
replace = source.index('rsync -a --delete')
assert guard < stop < replace, 'legacy HITL guard must run before replacing code'
PY
# A ReadWritePaths entry for a directory an ExecStartPre creates stops the unit
# with 226/NAMESPACE: the namespace is built first. The socket directory must
# be declared before the units start, never created from the unit itself.
if grep -q 'ExecStartPre=.*run/smart-node/hitl' "$repo/companions/hitl.sh"; then exit 1; fi
grep -q 'd /run/smart-node/hitl 2750' "$repo/companions/hitl.sh"
python3 - "$repo/companions/hitl.sh" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
assert source.rindex('    install_socket_dir\n') < source.index('systemctl enable "$UNIT"'), 'socket dir must exist before the units start'
PY
# A stray literal backslash-n after the tmpfiles call hands it an extra
# argument and the directory is never made; bash -n cannot see that.
if grep -q 'tmpfiles --create.*[\\]n' "$repo/companions/hitl.sh"; then exit 1; fi
echo 'HITL source integration: PASS'
