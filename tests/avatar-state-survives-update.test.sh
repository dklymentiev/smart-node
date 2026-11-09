#!/usr/bin/env bash
# Uploaded avatars are state, not package files; updates must preserve them.
# Run another checkout with SMART_NODE_SOURCE_ROOT=/path/to/tree.

set -uo pipefail

SELF_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${SMART_NODE_SOURCE_ROOT:-$SELF_ROOT}"
STUDIO_SERVER="$ROOT/companions/studio/src/server.js"
STUDIO_INSTALLER="$ROOT/companions/studio.sh"
STUDIO_UNIT="$ROOT/companions/studio/systemd/smart-node-studio.service"
CORE_ROUTE="$ROOT/companions/core-api/routes/agents.py"
CORE_INSTALLER="$ROOT/companions/core-api.sh"
MIGRATE="$ROOT/companions/studio/migrate-custom-avatars.sh"

pass=0
fail=0
ok() { echo "ok - $1"; pass=$((pass + 1)); }
bad() { echo "not ok - $1"; fail=$((fail + 1)); }
has() { if grep -Eq -- "$2" "$1" 2>/dev/null; then ok "$3"; else bad "$3"; fi; }
section_has() {
    local section
    section=$(sed -n "$2" "$1" 2>/dev/null)
    if grep -Eq -- "$3" <<<"$section"; then ok "$4"; else bad "$4"; fi
}
lacks_section() {
    local section
    section=$(sed -n "$2" "$1" 2>/dev/null)
    if grep -Eq -- "$3" <<<"$section"; then bad "$4"; else ok "$4"; fi
}

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT

# Reproduce the actual 1.12.43 failure shape without touching a node: the
# package sync deletes a file written inside the package while the DB-equivalent
# pointer, represented outside that tree, survives.
mkdir -p "$TMPD/source/public/avatars/custom" "$TMPD/install/public/avatars/custom"
printf 'irreplaceable-picture\n' > "$TMPD/install/public/avatars/custom/http-agent.webp"
printf 'custom/http-agent.webp\n' > "$TMPD/agent-avatar-pointer"
rsync -a --delete "$TMPD/source/" "$TMPD/install/"
if [[ ! -e "$TMPD/install/public/avatars/custom/http-agent.webp" \
      && "$(cat "$TMPD/agent-avatar-pointer")" == "custom/http-agent.webp" ]]; then
    ok "RED fixture reproduces package replacement deleting bytes but not the pointer"
else
    bad "RED fixture reproduces package replacement deleting bytes but not the pointer"
fi

has "$STUDIO_SERVER" 'SN_AVATAR_STATE_DIR' \
    "Studio resolves custom avatars from a declared state directory"
has "$STUDIO_SERVER" "app\.use\('/avatars/custom'.*express\.static" \
    "the legacy /avatars/custom URL is served from state"
has "$CORE_ROUTE" '@router\.post\("/\{slug\}/avatar"' \
    "core-api owns the uploaded-avatar write endpoint"
has "$CORE_ROUTE" 'SN_AVATAR_STATE_DIR' \
    "core-api writes avatar bytes only in the declared state directory"
section_has "$CORE_ROUTE" '/@router.post("\/{slug}\/avatar"/,/^@router\./p' \
    'Depends\(requires\("agents.update"\)\)' \
    "the avatar upload writer requires a resolved principal"

# The Studio route may validate and proxy, but it must no longer open a file in
# its package. Limit inspection to this route, through the next app route.
lacks_section "$STUDIO_SERVER" "/app.post('\/api\/agents\/:slug\/avatar'/,/^app\./p" \
    'createWriteStream|cwebp|CUSTOM_DIR' \
    "Studio upload route no longer writes package or state files"
section_has "$STUDIO_SERVER" "/app.post('\/api\/agents\/:slug\/avatar'/,/^app\./p" \
    'CORE_API_URL.*api/agents.*avatar' \
    "Studio proxies avatar bytes to the core-api writer"

has "$STUDIO_INSTALLER" 'migrate-custom-avatars\.sh' \
    "Studio installer invokes the legacy-file migration"
has "$STUDIO_INSTALLER" "--exclude='public/avatars/custom/'" \
    "package sync never deletes the legacy user-upload directory"
if [[ -x "$MIGRATE" ]]; then
    mkdir -p "$TMPD/legacy" "$TMPD/state"
    printf 'legacy-picture\n' > "$TMPD/legacy/alex.webp"
    if "$MIGRATE" "$TMPD/legacy" "$TMPD/state" "$(id -un)" "$(id -gn)" \
        && cmp -s "$TMPD/legacy/alex.webp" "$TMPD/state/alex.webp"; then
        rm -rf "$TMPD/install"
        mkdir -p "$TMPD/install/public/avatars/custom"
        cp "$TMPD/legacy/alex.webp" "$TMPD/install/public/avatars/custom/alex.webp"
        rsync -a --delete "$TMPD/source/" "$TMPD/install/"
        if [[ -f "$TMPD/state/alex.webp" ]]; then
            ok "legacy avatar survives the same package replacement in state"
        else
            bad "legacy avatar survives the same package replacement in state"
        fi
    else
        bad "legacy avatar survives the same package replacement in state"
    fi

    printf 'different-picture\n' > "$TMPD/legacy/alex.webp"
    if "$MIGRATE" "$TMPD/legacy" "$TMPD/state" "$(id -un)" "$(id -gn)" \
            >/dev/null 2>&1; then
        bad "migration refuses a same-name collision with different bytes"
    elif grep -q '^legacy-picture$' "$TMPD/state/alex.webp"; then
        ok "migration refuses a same-name collision with different bytes"
    else
        bad "migration refuses a same-name collision with different bytes"
    fi
else
    bad "legacy avatar survives the same package replacement in state"
    bad "migration refuses a same-name collision with different bytes"
fi

has "$STUDIO_UNIT" 'Environment=SN_AVATAR_STATE_DIR=/var/lib/smart-node/agents/avatars/custom' \
    "Studio receives the canonical avatar state path"
has "$STUDIO_UNIT" 'ReadOnlyPaths=/var/lib/smart-node/agents/avatars/custom' \
    "Studio has read-only systemd access to uploaded avatars"
if grep -q '^AVATAR_STATE_DIR="/var/lib/smart-node/agents/avatars/custom"$' "$CORE_INSTALLER" \
    && grep -q '^Environment=SN_AVATAR_STATE_DIR=\$AVATAR_STATE_DIR$' "$CORE_INSTALLER"; then
    ok "core-api receives the canonical avatar state path"
else
    bad "core-api receives the canonical avatar state path"
fi
has "$CORE_INSTALLER" 'ReadWritePaths=.*(/var/lib/smart-node/agents/avatars/custom|\$AVATAR_STATE_DIR)' \
    "only the core-api service contract grants application write access"

echo "$pass passed, $fail failed"
(( fail == 0 ))
