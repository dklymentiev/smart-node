#!/usr/bin/env bash
# An unchanged companion is not reinstalled on an update. The decision needs the same
# STAGE_VERSION AND the same source hash, and the node record must be read as root
# (companions.d is root:smart-node 2750: read as admin it failed silently and every
# update ran every companion in full).
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
f="$repo/orchestrator/install-on.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail() { echo "FAIL: $*"; exit 1; }

# the record is read with sudo
grep -q "sudo -n jq -r '.stage_version" "$f" || fail "stage_version is not read with sudo"
# the skip needs the hash as well as the version
grep -q '"\$src_hash" == "\$node_hash"' "$f" || fail "skip does not compare the source hash"
# a full install forgets the old hash first; a finished one records the new hash
grep -q "rm -f '\$srcfile'" "$f" || fail "a full install does not drop the old hash"
grep -q "install -m 0644 -o root -g root /dev/stdin '\$srcfile'" "$f" || fail "the hash is not recorded after install"

# the hash function, taken out of the installer and run on a small tree
eval "$(awk '/^companion_source_hash\(\) \{/{on=1} on{print} on && /^\}/{exit}' "$f")"
ROOT_DIR="$tmp/root"; COMPANIONS_DIR="$ROOT_DIR/companions"
mkdir -p "$COMPANIONS_DIR/side/sub"
echo 'STAGE_VERSION="1"' > "$COMPANIONS_DIR/x.sh"
echo a > "$COMPANIONS_DIR/side/one"
echo b > "$COMPANIONS_DIR/side/sub/two"

h1="$(companion_source_hash "$COMPANIONS_DIR/x.sh" "side" "K=1" "")"
h2="$(companion_source_hash "$COMPANIONS_DIR/x.sh" "side" "K=1" "")"
[ "$h1" = "$h2" ] || fail "the same source gave two hashes"

echo c > "$COMPANIONS_DIR/side/sub/two"
h3="$(companion_source_hash "$COMPANIONS_DIR/x.sh" "side" "K=1" "")"
[ "$h1" != "$h3" ] || fail "a changed sidecar file did not change the hash"

h4="$(companion_source_hash "$COMPANIONS_DIR/x.sh" "side" "K=2" "")"
[ "$h3" != "$h4" ] || fail "a changed environment string did not change the hash"

echo 'STAGE_VERSION="1" # edited' > "$COMPANIONS_DIR/x.sh"
h5="$(companion_source_hash "$COMPANIONS_DIR/x.sh" "side" "K=2" "")"
[ "$h4" != "$h5" ] || fail "a changed companion script did not change the hash"

# a different checkout of the same source gives the same hash
cp -r "$ROOT_DIR" "$tmp/other"
ROOT_DIR="$tmp/other"; COMPANIONS_DIR="$ROOT_DIR/companions"
h6="$(companion_source_hash "$COMPANIONS_DIR/x.sh" "side" "K=2" "")"
[ "$h5" = "$h6" ] || fail "the hash depends on where the checkout lives"

echo "companion-skip: PASS"
