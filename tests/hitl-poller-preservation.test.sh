#!/usr/bin/env bash
# Execute the installer's real rsync command against a legacy poller fixture.
# The cron registry still points at these exact installed paths after adoption
# and after rollback, so both scripts must survive an installer update.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
for name in flow_poller.py flow_poller.sh; do
    [[ -s "$repo/companions/hitl/$name" ]] || { echo "missing poller source: $name" >&2; exit 1; }
done

# Run the installer's seed function with a local install command. Ownership is
# assigned by the real installer, which runs as root; this fixture does not.
seed=$(sed -n '/^install_poller() {/,/^}/p' "$repo/companions/hitl.sh")
[[ -n "$seed" ]] || { echo 'installer poller seed missing' >&2; exit 1; }
install() { command install -m "$2" "$7" "$8"; }
eval "$seed"
SCRIPT_DIR="$repo/companions"
DEST="$tmp/fresh"
mkdir -p "$DEST"
install_poller
cmp "$SCRIPT_DIR/hitl/flow_poller.py" "$DEST/flow_poller.py"
cmp "$SCRIPT_DIR/hitl/flow_poller.sh" "$DEST/flow_poller.sh"
[[ -x "$DEST/flow_poller.sh" ]]
printf 'local poller\n' > "$DEST/flow_poller.py"
printf '#!/bin/sh\nexit 9\n' > "$DEST/flow_poller.sh"
chmod 0755 "$DEST/flow_poller.sh"
install_poller
[[ "$(cat "$DEST/flow_poller.py")" == 'local poller' ]]
[[ "$(tail -1 "$DEST/flow_poller.sh")" == 'exit 9' ]]

mkdir -p "$tmp/source/server" "$tmp/install/app/caller"
printf 'native server\n' > "$tmp/source/server/app.py"
printf 'legacy caller\n' > "$tmp/install/app/caller/hitl.py"
printf '#!/bin/sh\nexit 0\n' > "$tmp/install/flow_poller.sh"
printf 'signed answer poller\n' > "$tmp/install/flow_poller.py"
chmod 0755 "$tmp/install/flow_poller.sh"

command=$(sed -n '/^[[:space:]]*rsync -a --delete /{p;q;}' "$repo/companions/hitl.sh")
[[ -n "$command" ]] || { echo 'installer rsync command missing' >&2; exit 1; }
for phase in adoption update; do
    SRC="$tmp/source" DEST="$tmp/install" bash -c "$command"
    [[ -x "$tmp/install/flow_poller.sh" \
       && "$(cat "$tmp/install/flow_poller.py")" == 'signed answer poller' \
       && -f "$tmp/install/app/caller/hitl.py" ]] \
        || { echo "HITL $phase removed the cron answer path" >&2; exit 1; }
done
# Rollback reuses the installed code and current caller state. Pin this here so
# a later rollback refactor cannot erase the cron target after a good adoption.
python3 - "$repo/companions/hitl.sh" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
rollback = source.split("rollback_legacy() {", 1)[1].split("\n}\n", 1)[0]
assert '$DEST' not in rollback and 'rsync' not in rollback
PY
[[ -x "$tmp/install/flow_poller.sh" && -f "$tmp/install/flow_poller.py" ]]
echo 'HITL flow poller survives adoption, updates and rollback'
