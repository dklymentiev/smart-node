#!/usr/bin/env bash
# The flow sweep timer is installed by the dispatcher companion, the sweep module
# by the workflows companion, which runs after it. On an update from an engine
# without the sweep, the unit must be skipped until the module is there: a failed
# first run leaves the unit failed for a minute and the validation at the end of
# the update reports the node as broken.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
disp="$repo/companions/dispatcher.sh"

engine=$(sed -n 's/^FLOW_SWEEP_ENGINE="\(.*\)"$/\1/p' "$disp")
[[ -n "$engine" ]] || { echo "dispatcher.sh names no engine sweep module" >&2; exit 1; }
unit=$(sed -n '/^install_flow_sweep() {/,/^}/p' "$disp")
grep -q '^ConditionPathExists=\$FLOW_SWEEP_ENGINE$' <<<"$unit" \
    || { echo "the flow sweep unit runs before the engine has a sweep module" >&2; exit 1; }

# The path must be where the workflows companion puts the engine source.
src=$(sed -n 's/^REIN_SRC_DIR="\$INSTALL_DIR\/\(.*\)"$/\1/p' "$repo/companions/workflows.sh")
install_dir=$(sed -n 's/^INSTALL_DIR="\(.*\)"$/\1/p' "$repo/companions/workflows.sh" | head -1)
[[ -n "$src" && -n "$install_dir" ]] || { echo "cannot read the engine source directory from workflows.sh" >&2; exit 1; }
[[ "$engine" == "$install_dir/$src/rein/sweep.py" ]] \
    || { echo "dispatcher.sh waits for $engine, workflows.sh installs $install_dir/$src/rein/sweep.py" >&2; exit 1; }
[[ -f "$repo/companions/rein/rein/sweep.py" ]] \
    || { echo "the vendored engine has no rein/sweep.py" >&2; exit 1; }
echo "flow sweep unit waits for the engine's sweep module"
