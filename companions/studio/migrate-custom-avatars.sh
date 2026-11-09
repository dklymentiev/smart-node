#!/usr/bin/env bash
# Copy legacy Studio-package avatar uploads into durable node state.
# The caller stops Studio first, so no upload can race this copy.

set -euo pipefail

if [[ $# -ne 4 ]]; then
    echo "usage: $0 LEGACY_DIR STATE_DIR OWNER GROUP" >&2
    exit 2
fi

LEGACY_DIR="$1"
STATE_DIR="$2"
OWNER="$3"
GROUP="$4"

install -d -m 0750 -o "$OWNER" -g "$GROUP" "$STATE_DIR"
[[ -d "$LEGACY_DIR" ]] || exit 0

shopt -s nullglob
for source in "$LEGACY_DIR"/*; do
    [[ -f "$source" && ! -L "$source" ]] || continue
    name="$(basename "$source")"
    [[ "$name" == .* ]] && continue
    destination="$STATE_DIR/$name"
    if [[ -e "$destination" ]]; then
        if [[ ! -f "$destination" || -L "$destination" ]] || ! cmp -s "$source" "$destination"; then
            echo "avatar migration collision: $name differs between package and state" >&2
            exit 1
        fi
        continue
    fi
    temporary="$STATE_DIR/.migrate-${name}.$$"
    trap 'rm -f -- "${temporary:-}"' EXIT
    install -m 0640 -o "$OWNER" -g "$GROUP" "$source" "$temporary"
    mv -- "$temporary" "$destination"
    trap - EXIT
done

# Converge permissions without making the package or Studio service a writer.
find "$STATE_DIR" -mindepth 1 -maxdepth 1 -type f -exec chown "$OWNER:$GROUP" {} +
find "$STATE_DIR" -mindepth 1 -maxdepth 1 -type f -exec chmod 0640 {} +
