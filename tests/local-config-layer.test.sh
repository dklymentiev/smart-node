#!/usr/bin/env bash
#
# The node-local settings layer survives an update.
#
# WHY THIS EXISTS. A release rewrites the env files it generates and replaces
# its trees with rsync --delete. A value an operator set by hand in one of them
# was gone after the next update without a word (FAST_CHANNELS, a runner lease,
# the HITL sign-on settings, seen on a live node on 2026-10-13). The rule is two
# layers: the release's files, and /etc/smart-node/local/<unit>.env, which only
# the operator writes and which every smart-node unit loads last.
#
# THE PROPERTIES:
#   1. every smart-node-*.service gets zz-local.conf naming /etc/smart-node/local/%N.env, optional (-)
#   2. a unit that is not smart-node-* is not touched
#   3. a file in the local directory is never rewritten, by content or by time
#   4. another drop-in of the same unit is never touched
#   5. a second run changes nothing
#   6. --verify fails when a unit lacks the drop-in, and passes after a run
#   7. the installer runs the stage before the permissions stage, and stops when it fails
#   8. B01 installs the stage beside permissions.sh, and the installer stages it
#   9. permissions.list declares /etc/smart-node/local owner-only
#
#   tests/local-config-layer.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
STAGE="$REPO/bootstrap/local-layer.sh"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "local config layer"

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export SN_SYSTEMD_DIR="$T/systemd" SN_LOCAL_DIR="$T/local" SN_NO_DAEMON_RELOAD=1
mkdir -p "$SN_SYSTEMD_DIR/smart-node-runner.service.d" "$SN_LOCAL_DIR"
for u in smart-node-runner smart-node-core-api smart-node-hitl other-thing; do
    printf '[Service]\nEnvironmentFile=/etc/smart-node/x.env\nExecStart=/bin/true\n' > "$SN_SYSTEMD_DIR/$u.service"
done
printf '[Service]\nEnvironment=FOO=1\n' > "$SN_SYSTEMD_DIR/smart-node-runner.service.d/50-operator.conf"
printf 'RUNNER_LEASE_SECONDS=3700\n' > "$SN_LOCAL_DIR/smart-node-runner.env"
touch -d '2026-01-01 00:00' "$SN_LOCAL_DIR/smart-node-runner.env" "$SN_SYSTEMD_DIR/smart-node-runner.service.d/50-operator.conf"
local_before=$(stat -c '%Y %s' "$SN_LOCAL_DIR/smart-node-runner.env")
foreign_before=$(stat -c '%Y %s' "$SN_SYSTEMD_DIR/smart-node-runner.service.d/50-operator.conf")

bash "$STAGE" --verify >/dev/null 2>&1 && bad "6 --verify fails before the first run" || ok "6 --verify fails before the first run"
bash "$STAGE" >/dev/null || bad "the stage runs"

n=0
for u in smart-node-runner smart-node-core-api smart-node-hitl; do
    f="$SN_SYSTEMD_DIR/$u.service.d/zz-local.conf"
    grep -qx "EnvironmentFile=-$SN_LOCAL_DIR/%N.env" "$f" 2>/dev/null && n=$((n + 1))
done
[[ $n == 3 ]] && ok "1 every smart-node unit loads its local file, optional" || bad "1 drop-in on $n of 3 units"
[[ ! -e "$SN_SYSTEMD_DIR/other-thing.service.d" ]] && ok "2 a unit that is not smart-node-* is untouched" || bad "2 other-thing got a drop-in"
[[ "$(stat -c '%Y %s' "$SN_LOCAL_DIR/smart-node-runner.env")" == "$local_before" ]] \
    && ok "3 the local file is not rewritten" || bad "3 the local file changed"
[[ "$(stat -c '%Y %s' "$SN_SYSTEMD_DIR/smart-node-runner.service.d/50-operator.conf")" == "$foreign_before" ]] \
    && ok "4 another drop-in is untouched" || bad "4 the operator's drop-in changed"

snap() { find "$T" -type f -printf '%p %T@ %s\n' | sort; }
s1=$(snap); bash "$STAGE" >/dev/null; s2=$(snap)
[[ "$s1" == "$s2" ]] && ok "5 a second run changes nothing" || bad "5 a second run changed files"
bash "$STAGE" --verify >/dev/null 2>&1 && ok "6 --verify passes after a run" || bad "6 --verify fails after a run"

INSTALL="$REPO/orchestrator/install-on.sh"
l_layer=$(grep -n 'local-layer.sh' "$INSTALL" | grep -v -E '^\s*[0-9]+:\s*#|scp |rm -f' | head -1 | cut -d: -f1)
l_perm=$(grep -n '=== permissions (apply the declared surface) ===' "$INSTALL" | cut -d: -f1)
if [[ -n "$l_layer" && -n "$l_perm" && "$l_layer" -lt "$l_perm" ]] \
   && sed -n "${l_layer},$((l_layer + 4))p" "$INSTALL" | grep -q 'die '; then
    ok "7 the installer runs the stage before the permissions stage and stops when it fails"
else
    bad "7 install-on.sh does not run local-layer.sh before permissions (layer line ${l_layer:-none}, permissions ${l_perm:-none})"
fi

grep -q 'local-layer.sh' "$REPO/bootstrap/B01-core.sh" && grep -q 'scp .*local-layer.sh' "$INSTALL" \
    && ok "8 B01 installs the stage and the installer stages it" || bad "8 local-layer.sh is not staged and installed"

grep -qE '^/etc/smart-node/local[[:space:]]+root[[:space:]]+root[[:space:]]+0700' "$REPO/bootstrap/permissions.list" \
    && ok "9 permissions.list declares /etc/smart-node/local owner-only" || bad "9 /etc/smart-node/local not declared"

echo
(( FAILED )) && { echo "FAIL ($FAILED)"; exit 1; }
echo "PASS"
