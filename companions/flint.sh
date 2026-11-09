#!/bin/bash
#
# flint.sh -- Smart Node installer, Layer C Phase 2 companion: flint.
#
# Runs INSIDE the target node as root. Idempotent. Optional (flint.manifest.json):
# a second agent runtime next to Claude Code. The gateway already knows Flint
# (provider flint-cli, FLINT.md); until now the binary was put on a node by hand.
#
# Installs, all under /opt/smart-node/flint:
#   - node/          Node.js $NODE_VERSION (Flint needs 22.12+, the node itself has 20;
#                    a private copy so nothing else on the node changes)
#   - app-VERSION/   Flint from its public release tag, production dependencies only
#   - /usr/local/bin/flint, the launcher the gateway runs (FLINT_PATH)
# Then the gateway is re-run as a dependent (sn-components dependents flint) and
# turns Flint on because this companion is installed (gateway.sh).
#
# The gateway refuses a release older than 1.16.0 (config.FLINT_MIN_VERSION),
# and so does --verify here.

set -euo pipefail

STAGE_ID="flint"
STAGE_VERSION="0.1.0"

FLINT_VERSION="${SMART_NODE_FLINT_VERSION:-1.16.3}"
FLINT_REPO="https://github.com/dklymentiev/flint-agent"
NODE_VERSION="${SMART_NODE_FLINT_NODE_VERSION:-22.23.3}"
NODE_DIST="https://nodejs.org/dist/v$NODE_VERSION"

BASE="/opt/smart-node/flint"
NODE_DIR="$BASE/node"
APP_DIR="$BASE/app-$FLINT_VERSION"
LAUNCHER="/usr/local/bin/flint"

LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

MODE="run"
case "${1:-}" in
    "")          MODE="run" ;;
    # A re-run with this version: the install checks what is there and does only what is
    # missing, and Flint writes no contracts of its own to refresh.
    --contracts-only) MODE="run" ;;
    --verify)    MODE="verify" ;;
    --status)    MODE="status" ;;
    --uninstall) MODE="uninstall" ;;
    *)           die "unknown arg: $1" ;;
esac

install_node() {
    if [[ -x "$NODE_DIR/bin/node" ]] && [[ "$("$NODE_DIR/bin/node" --version)" == "v$NODE_VERSION" ]]; then
        log "node v$NODE_VERSION already in $NODE_DIR"
        return
    fi
    local arch; case "$(uname -m)" in x86_64) arch=x64 ;; aarch64) arch=arm64 ;; *) die "unsupported arch $(uname -m)" ;; esac
    local tar="node-v$NODE_VERSION-linux-$arch.tar.gz" tmp; tmp=$(mktemp -d)
    log "downloading Node.js v$NODE_VERSION ($arch)"
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 "$NODE_DIST/$tar" -o "$tmp/$tar" || die "could not fetch $NODE_DIST/$tar"
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 "$NODE_DIST/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt" || die "could not fetch the Node checksums"
    (cd "$tmp" && grep " $tar\$" SHASUMS256.txt | sha256sum -c --status) || die "Node.js checksum mismatch for $tar"
    rm -rf "$NODE_DIR" && mkdir -p "$NODE_DIR"
    tar -xzf "$tmp/$tar" -C "$NODE_DIR" --strip-components=1
    rm -rf "$tmp"
    manifest_record file "$NODE_DIR"
}

install_flint() {
    if [[ -f "$APP_DIR/bin/flint.js" ]]; then
        log "flint $FLINT_VERSION already in $APP_DIR"
        return
    fi
    local tmp; tmp=$(mktemp -d)
    log "downloading Flint v$FLINT_VERSION"
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 "$FLINT_REPO/archive/refs/tags/v$FLINT_VERSION.tar.gz" -o "$tmp/flint.tgz" \
        || die "could not fetch Flint v$FLINT_VERSION"
    mkdir -p "$tmp/app" && tar -xzf "$tmp/flint.tgz" -C "$tmp/app" --strip-components=1
    log "installing Flint dependencies (production only)"
    # better-sqlite3 downloads its prebuilt binary from GitHub with a short
    # timeout, and its fallback (a source build) needs make, which a server
    # does not have. One slow answer stopped the whole install (2026-10-30,
    # twice in a row); the same download then took a second. So: try again.
    local n
    for n in 1 2 3; do
        (cd "$tmp/app" && PATH="$NODE_DIR/bin:$PATH" npm ci --omit=dev --no-audit --no-fund >/dev/null) && break
        (( n < 3 )) || die "npm ci failed for Flint v$FLINT_VERSION (3 tries)"
        log "npm ci failed (try $n of 3), trying again in 10 s"
        sleep 10
    done
    rm -rf "$APP_DIR" && mv "$tmp/app" "$APP_DIR"
    chown -R root:root "$APP_DIR" && chmod -R a+rX "$APP_DIR"
    rm -rf "$tmp"
    manifest_record file "$APP_DIR"
}

write_launcher() {
    # NODE_USE_SYSTEM_CA: Flint trusts the node's CA store (an internal router
    # signed by a private CA verifies the same way curl does)
    printf '#!/bin/sh\nexport NODE_USE_SYSTEM_CA=1\nexec %s/bin/node %s/bin/flint.js "$@"\n' \
        "$NODE_DIR" "$APP_DIR" > "$LAUNCHER"
    chmod 0755 "$LAUNCHER"
    manifest_record file "$LAUNCHER"
}

run_verify_checks() {
    verify_check "node v$NODE_VERSION present" bash -c "[[ \"\$('$NODE_DIR/bin/node' --version)\" == v$NODE_VERSION ]]"
    verify_check "flint $FLINT_VERSION present" test -f "$APP_DIR/bin/flint.js"
    verify_check "$LAUNCHER executable" test -x "$LAUNCHER"
    verify_check "flint answers as $FLINT_VERSION" bash -c "[[ \"\$('$LAUNCHER' --version 2>/dev/null)\" == 'flint $FLINT_VERSION' ]]"
}

run_intent() {
    require_root
    log_init "$STAGE_ID"
    log "companion flint -- start (flint $FLINT_VERSION, node $NODE_VERSION)"
    install_node
    install_flint
    # Written by root, by nobody else: the tarballs keep their group-write bits, and the
    # installer's permissions stage refuses a node with undeclared writable paths.
    chmod -R go-w "$NODE_DIR" "$APP_DIR"
    write_launcher
    register_companion "$STAGE_ID" "$STAGE_VERSION" ""
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    if (( VERIFY_ERRORS > 0 )); then
        stage_mark "$STAGE_ID" failed "version=flint@$STAGE_VERSION" "reason=post-run verify failed"
        die "$VERIFY_ERRORS verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done "version=flint@$STAGE_VERSION" "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    log "companion flint -- done; re-run the gateway so it turns Flint on"
}

case "$MODE" in
    run) run_intent ;;
    verify)
        log_init "$STAGE_ID"; VERIFY_ERRORS=0; VERIFY_TOTAL=0; run_verify_checks
        (( VERIFY_ERRORS == 0 )) || { echo "[FAIL] $VERIFY_ERRORS check(s) failed" >&2; exit "$VERIFY_ERRORS"; }
        echo "[OK] $VERIFY_TOTAL/$VERIFY_TOTAL checks passed" ;;
    status) stage_status "$STAGE_ID" ;;
    uninstall)
        require_root; unregister_companion "$STAGE_ID" 2>/dev/null || true
        rm -rf "$BASE" "$LAUNCHER" "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
        log "companion flint -- uninstalled; re-run the gateway to turn Flint off" ;;
esac
