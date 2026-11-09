#!/bin/bash
#
# revendor.sh -- refresh a vendored companion tree from its own repository.
#
# The installer ships snapshots of components that also live in their own repos.
# Nothing enforces that a snapshot tracks its repo, and nothing notices when it
# stops: on 2026-08-03 the vendored communication was 1.0.0 while canon had been
# 1.2.0 now, so every node installed for weeks shipped WITHOUT the mail
# loop guard and without recipient resolution -- a customer emailing the business
# got the back-office configurator's answer, from the wrong agent.
#
# Hand-copying is how that happened and also how adapters/voice was destroyed
# (56147fc: an extraction with --delete removed what the new repo never had).
# So: one command, explicit about what it keeps, and it prints the difference
# before it touches anything.
#
# Usage:
#   tools/revendor.sh <name> [--from DIR] [--apply]
#
#   <name>      companion directory under companions/ (e.g. communication)
#   --from DIR  source repository (required, or set SRC)
#   --apply     actually write. Without it, this only reports.
#
# Exits 0 clean, 1 on a problem.

set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
NAME="${1:-}"; shift || true
SRC=""
APPLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --from)  SRC="$2"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[[ -n "$NAME" ]] || { echo "usage: revendor.sh <name> [--from DIR] [--apply]" >&2; exit 1; }
DST="$ROOT/companions/$NAME"
[[ -n "${SRC:-}" ]] || { echo "no source repository: pass --from DIR" >&2; exit 1; }
[[ -d "$DST" ]] || { echo "no vendored tree: $DST" >&2; exit 1; }
[[ -d "$SRC" ]] || { echo "no source repository: $SRC" >&2; exit 1; }

# Runtime and local state never travel. `tools/` and NOTES-* are vendored-only
# by design (the companion excludes tools/ from the node deploy and ships it to
# /srv/tools separately), so they are KEPT rather than deleted -- excluded paths
# survive --delete.
#
# VENDORED is the marker this project writes ON a vendored tree and it never
# exists upstream, so --delete removed it: the one record of which tag the
# snapshot came from, erased by the tool written to stop snapshots drifting.
# Caught on the planner tree 2026-08-13 before it was applied.
# .pytest_cache appears in any checkout whose tests have been run.
EXCL=(--exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc'
      --exclude '.pytest_cache/' --exclude 'VENDORED'
      --exclude 'state/' --exclude '.env' --exclude '.env.*'
      --exclude 'tools/' --exclude 'NOTES-*.md')

echo "[revendor] $NAME"
echo "  from: $SRC  ($(cat "$SRC/VERSION" 2>/dev/null || echo '?'))"
echo "  into: $DST  ($(cat "$DST/VERSION" 2>/dev/null || echo '?'))"
echo
echo "[revendor] what would change:"
rsync -ain --delete "${EXCL[@]}" "$SRC/" "$DST/" | sed 's/^/  /'
echo

if (( ! APPLY )); then
    echo "[revendor] report only. Re-run with --apply to write."
    exit 0
fi

rsync -a --delete "${EXCL[@]}" "$SRC/" "$DST/" || { echo "[revendor] rsync failed" >&2; exit 1; }
echo "[revendor] applied. Vendored $NAME is now $(cat "$DST/VERSION" 2>/dev/null || echo '?')."
echo "[revendor] the companion script installs only the units it knows about --"
echo "           check companions/$NAME.sh against any units the repo added."
