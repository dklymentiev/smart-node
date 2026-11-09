#!/usr/bin/env bash
#
# local-layer.sh -- the node-local settings layer.
#
# WHY THIS EXISTS. A release owns its configuration: an update rewrites the env
# files the installer generates (core-api.env, operator.env, fast-channels.env,
# hitl.env, ...) and replaces the companion trees under /opt/smart-node with
# rsync --delete. A value an operator sets by hand in one of those files is gone
# after the next update, silently: the service starts on the release's value.
# Seen on a live node on 2026-10-13: FAST_CHANNELS, a runner lease and the HITL
# single sign-on settings all lived in release-owned files and each update put
# them back by hand.
#
# THE RULE. Two layers per service, each with one owner:
#
#   release  the unit's own EnvironmentFile lines. The installer writes them
#            and may rewrite them on every update. Nobody edits them by hand.
#   node     /etc/smart-node/local/<unit>.env. Only the operator writes it.
#            The installer never creates, rewrites or deletes anything in
#            /etc/smart-node/local.
#
# Every smart-node-*.service gets the drop-in zz-local.conf:
#
#   [Service]
#   EnvironmentFile=-/etc/smart-node/local/%N.env
#
# systemd reads drop-ins after the unit file and a later assignment of a
# variable wins, so a value in the node layer overrides the release's. The
# leading "-" makes the file optional: a unit with no local settings starts as
# it always did.
#
# This stage writes only its own drop-in, only under smart-node-*.service.d/,
# and never touches another drop-in (an operator's 50-something.conf stays).
#
# Usage (root, on the node; the installer runs it before the permissions stage):
#
#   local-layer.sh            create the directory, add or refresh the drop-ins
#   local-layer.sh --verify   exit 1 when a unit lacks the drop-in
#
# Test seams: SN_SYSTEMD_DIR, SN_LOCAL_DIR, SN_NO_DAEMON_RELOAD=1.
set -euo pipefail

SYSTEMD_DIR="${SN_SYSTEMD_DIR:-/etc/systemd/system}"
LOCAL_DIR="${SN_LOCAL_DIR:-/etc/smart-node/local}"
# systemd reads a unit's drop-ins in lexicographic order and the LAST
# EnvironmentFile wins. Release drop-ins carry names like auth.conf or
# fast-channels.conf; "zz-" sorts after every digit and letter, so the node
# layer is read last whatever the release adds.
DROPIN="zz-local.conf"
MODE="${1:-apply}"
[[ "$MODE" == apply || "$MODE" == --verify ]] || { echo "usage: local-layer.sh [--verify]" >&2; exit 2; }

body() {
    cat <<EOF
# Node-local settings (bootstrap/local-layer.sh). Loaded after the release's own
# EnvironmentFile lines, so a value in ${LOCAL_DIR}/<unit>.env wins.
# The release never writes ${LOCAL_DIR}; an update keeps this file and that one.
[Service]
EnvironmentFile=-${LOCAL_DIR}/%N.env
EOF
}

units() {
    local f
    shopt -s nullglob
    for f in "$SYSTEMD_DIR"/smart-node-*.service; do
        [[ -f "$f" && ! -L "$f" ]] && basename "$f"
    done
    shopt -u nullglob
}

missing=0 changed=0 total=0
for u in $(units); do
    total=$((total + 1))
    d="$SYSTEMD_DIR/$u.d"
    if [[ "$MODE" == --verify ]]; then
        if [[ "$(cat "$d/$DROPIN" 2>/dev/null)" != "$(body)" ]]; then
            echo "MISSING $u: no $DROPIN" >&2
            missing=$((missing + 1))
        fi
        continue
    fi
    if [[ "$(cat "$d/$DROPIN" 2>/dev/null)" != "$(body)" ]]; then
        install -d -m 0755 "$d"
        body > "$d/$DROPIN.tmp"
        chmod 0644 "$d/$DROPIN.tmp"
        mv -f "$d/$DROPIN.tmp" "$d/$DROPIN"
        changed=$((changed + 1))
    fi
done

if [[ "$MODE" == --verify ]]; then
    [[ -d "$LOCAL_DIR" ]] || { echo "MISSING $LOCAL_DIR" >&2; missing=$((missing + 1)); }
    echo "local layer: $total unit(s), $missing missing"
    exit $(( missing > 0 ))
fi

# The directory is the operator's. Create it once; never touch what is inside.
if [[ ! -d "$LOCAL_DIR" ]]; then
    install -d -m 0700 -o root -g root "$LOCAL_DIR"
fi
if (( changed )) && [[ "${SN_NO_DAEMON_RELOAD:-0}" != 1 ]]; then
    systemctl daemon-reload
fi
echo "local layer: $total unit(s), $changed drop-in(s) written, $LOCAL_DIR kept"
