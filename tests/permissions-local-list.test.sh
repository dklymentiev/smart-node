#!/usr/bin/env bash
#
# permissions-local-list.test.sh -- the node-local declarations list.
#
# Runs bootstrap/permissions.sh for real against a scratch tree, as root inside
# an unprivileged user namespace (unshare -r), with the scan roots, the
# node-local roots and the node-local list pointed at the scratch tree.
#
#   1. without a local list, a group-writable state dir is refused (exit 2)
#   2. a local entry declares it: the stage passes and counts it as local
#   3. a local entry outside the node-local roots is refused
#   4. a local entry for a path the release declares is ignored, and the
#      release's mode is still applied
#   5. a local list writable beyond root is refused whole
#   6. a local entry with .. is refused
#   7. permissions.list declares /etc/smart-node/permissions.local.list 0644
#
# Exit: 0 all pass, 1 any fail, 2 setup error (no user namespaces: skipped).

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0
ok()  { echo "  PASS $*"; PASS=$((PASS+1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL+1)); }

# Never as real root: the stage would then really change group memberships on
# this machine. Inside the namespace those changes fail harmlessly.
if (( EUID == 0 )); then
    echo "SKIP: run as a normal user (the stage runs as namespace root)"
    exit 2
fi
if ! unshare -r true 2>/dev/null; then
    echo "SKIP: unprivileged user namespaces are not available here"
    exit 2
fi

umask 022
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
mkdir -p "$W/bin" "$W/state" "$W/code" "$W/etc"
cp "$REPO/bootstrap/permissions.sh" "$W/bin/permissions.sh"

# A release list with one declared dir, and nothing else.
cat > "$W/bin/permissions.list" <<EOF
$W/state/declared   -   -   0750
EOF

run() {
    SMART_NODE_STATE_DIR="$W/stages" \
    SN_PERMISSIONS_SCAN_ROOTS="$W/state $W/code" \
    SN_PERMISSIONS_LOCAL_ROOTS="$W/state" \
    SN_PERMISSIONS_LOCAL_LIST="$W/etc/local.list" \
    SMART_NODE_GROUP=root SMART_NODE_AGENT_GROUP=root \
        unshare -r bash "$W/bin/permissions.sh" 2>&1 | grep -v "^usermod: cannot lock" > "$W/out"
    return "${PIPESTATUS[0]}"
}

mkdir -p "$W/state/declared" "$W/state/flow-work"
chmod 2770 "$W/state/flow-work"

# 1
run; rc=$?
if (( rc == 2 )) && grep -q "REFUSE $W/state/flow-work" "$W/out"; then
    ok "1 undeclared group-writable state dir is refused"
else
    bad "1 expected refusal (rc=$rc)"; cat "$W/out"
fi

# 2
printf '%s\n' "$W/state/flow-work  -  -  2770  cover  # a flow writes here" > "$W/etc/local.list"
chmod 0644 "$W/etc/local.list"
run; rc=$?
if (( rc == 0 )) && grep -q "local=1" "$W/out"; then
    ok "2 a local entry declares it and the stage passes"
else
    bad "2 expected pass with local=1 (rc=$rc)"; cat "$W/out"
fi

# 3
mkdir -p "$W/code/x"; chmod 0775 "$W/code/x"
printf '%s\n' "$W/state/flow-work  -  -  2770  cover" "$W/code/x  -  -  0775" > "$W/etc/local.list"
run; rc=$?
if (( rc == 2 )) && grep -q "REFUSE local list: '$W/code/x' is outside" "$W/out"; then
    ok "3 a local entry outside the node-local roots is refused"
else
    bad "3 expected refusal of the code path (rc=$rc)"; cat "$W/out"
fi
chmod 0755 "$W/code/x"

# 4
chmod 0777 "$W/state/declared"
printf '%s\n' "$W/state/flow-work  -  -  2770  cover" "$W/state/declared  -  -  0777" > "$W/etc/local.list"
run; rc=$?
m=$(stat -c %a "$W/state/declared")
if (( rc == 0 )) && grep -q "ignore local list: '$W/state/declared' is declared by the release" "$W/out" && [[ "$m" == 750 ]]; then
    ok "4 a local entry cannot loosen a release path (mode back to 750)"
else
    bad "4 expected ignore and mode 750 (rc=$rc mode=$m)"; cat "$W/out"
fi

# 5
printf '%s\n' "$W/state/flow-work  -  -  2770  cover" > "$W/etc/local.list"
chmod 0664 "$W/etc/local.list"
run; rc=$?
if (( rc == 2 )) && grep -q "must be a root-owned file writable by root only" "$W/out"; then
    ok "5 a group-writable local list is refused whole"
else
    bad "5 expected refusal of the list (rc=$rc)"; cat "$W/out"
fi
chmod 0644 "$W/etc/local.list"

# 6
printf '%s\n' "$W/state/../code/x  -  -  0775" > "$W/etc/local.list"
run; rc=$?
if (( rc == 2 )) && grep -q "is not a plain absolute path" "$W/out"; then
    ok "6 a local entry with .. is refused"
else
    bad "6 expected refusal of the .. path (rc=$rc)"; cat "$W/out"
fi

# 7
grep -qE '^/etc/smart-node/permissions.local.list[[:space:]]+root[[:space:]]+root[[:space:]]+0644' "$REPO/bootstrap/permissions.list" \
    && ok "7 permissions.list declares the local list root 0644" || bad "7 local list not declared"

echo "permissions-local-list: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
