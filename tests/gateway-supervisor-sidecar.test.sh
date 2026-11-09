#!/usr/bin/env bash
# The gateway companion copies the shared supervisor from a path relative to its
# own directory. The installer stages the companion in a private directory, so
# the sidecar must land at that same relative path there, or the companion
# stops on a missing file during install and update.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"

read_path=$(sed -n "s#.*install -m 0644 \"\$SCRIPT_DIR/\([^\"]*supervisor\.py\)\".*#\1#p" "$repo/companions/gateway.sh")
[[ -n "$read_path" ]] || { echo "gateway.sh no longer copies the supervisor from SCRIPT_DIR" >&2; exit 1; }
[[ -f "$repo/companions/$read_path" ]] || { echo "source tree lacks companions/$read_path" >&2; exit 1; }

spec=$(grep -E "^[[:space:]]*run_companion gateway " "$repo/orchestrator/install-on.sh" \
       | grep -oE "[^ \"]+supervisor\.py:[^ \"]+")
[[ -n "$spec" ]] || { echo "install-on.sh ships no supervisor sidecar with the gateway" >&2; exit 1; }
[[ -f "$repo/${spec%%:*}" ]] || { echo "sidecar source missing: ${spec%%:*}" >&2; exit 1; }
[[ "${spec##*:}" == "$read_path" ]] || {
    echo "gateway.sh reads $read_path but the installer stages ${spec##*:}" >&2; exit 1; }
echo "gateway supervisor sidecar is staged where the companion reads it"
