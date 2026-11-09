#!/usr/bin/env bash
# The shipped configurator must match the reviewed brief, and its tool catalogue
# must describe contacts and task wakes honestly. --baseline checks a git ref.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASELINE=""
[[ "${1:-}" != --baseline ]] || BASELINE="${2:?ref required}"
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1"
    else cat "$REPO/$1"; fi
}
failed=0
check() {
    local label="$1"; shift
    if "$@"; then echo "  ok: $label"; else echo "  FAIL: $label"; failed=1; fi
}
contains() { grep -qF -- "$1" <<< "$2"; }
lacks() { ! contains "$@"; }
brief_hash="$(read_src companions/srv-skeleton/agents/_specialists/configurator.md | sha256sum | cut -d ' ' -f 1)"
check "canonical brief equals reviewed v6 plus process-building and business-or-personal and the role library, with the app named Studio, neutral pronouns and no internal product names" test "$brief_hash" = 0c562b155b1f1ef7a1cf3ecb18b18f63dadc1cda8101be857fca5256b1e51b6c
catalogue="$(read_src node-versioning/capabilities.md)"
check "stale contacts claim absent" lacks "no primitive in the release yet" "$catalogue"
check "contacts grant call supplied" contains 'PUT /api/agents/{slug}/contacts/{contact-id}' "$catalogue"
usage="$(read_src companions/tracking.sh)"
check "unconditional task wake promise absent" lacks "Assign it a" "$usage"
check "agent-filed task caveat supplied" contains "A task filed by an agent" "$usage"
exit "$failed"
