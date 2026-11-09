#!/bin/bash
#
# versions.sh: answers "what STAGE_VERSION does each named companion carry?"
# from one place. These companions once shipped images named after it; none
# does now, and the names are kept so the runbook check below still has an
# answer to compare.
#
# There is deliberately NO versions file to keep in sync. STAGE_VERSION inside
# each companion script IS the source of truth, because that is the value the
# companion uses to name its tarball and tag its image at install time. Anything
# else -- build scripts, docs, CI -- should read it from here rather than
# hand-copy a literal that will drift.
#
# Do not confuse this with the VERSION file inside a vendored component
# (companions/doci/VERSION, companions/studio/VERSION). That is the upstream
# application's own version and is a different thing: Studio ships app 0.5.0 as
# companion 1.0.0, doci ships app 0.2.0-rc as companion 0.3.1. Tagging an image
# from the app VERSION produces something no loader will ever look for.
#
# Usage:
#   tools/versions.sh            "image version" per line
#   tools/versions.sh --export   shell assignments, for eval
#   tools/versions.sh --check    verify the runbook agrees; exit 1 if it does not

set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(dirname "$HERE")"

die() { echo "[ERROR] $*" >&2; exit 1; }

# image -> the companion script whose STAGE_VERSION governs it
read_stage_version() {
    local companion="$1"
    local file="$ROOT/companions/$companion.sh"
    [[ -r "$file" ]] || die "companion not readable: $file"
    local v
    v=$(grep -m1 '^STAGE_VERSION=' "$file" | cut -d'"' -f2)
    [[ -n "$v" ]] || die "no STAGE_VERSION in $file"
    printf '%s' "$v"
}

PLANNER_VERSION="$(read_stage_version tracking)"
DOCI_VERSION="$(read_stage_version documents)"
MESH_VERSION="$(read_stage_version memory)"

MODE="${1:-print}"
case "$MODE" in
    print)
        echo "planner  $PLANNER_VERSION"
        echo "doci     $DOCI_VERSION"
        echo "mesh     $MESH_VERSION"
        ;;
    --export)
        echo "PLANNER_VERSION=$PLANNER_VERSION"
        echo "DOCI_VERSION=$DOCI_VERSION"
        echo "MESH_VERSION=$MESH_VERSION"
        ;;
    --check)
        # The runbook moved into docs/ when that directory was created. Keep the
        # old root path as a fallback so an older checkout still checks itself.
        RUNBOOK="$ROOT/docs/install-from-source.md"
        [[ -r "$RUNBOOK" ]] || RUNBOOK="$ROOT/INSTALL-FROM-SOURCE.md"
        [[ -r "$RUNBOOK" ]] || die "runbook not readable: $RUNBOOK"
        fails=0
        # The runbook states the values inline; they must match the scripts.
        check_doc() {
            local label="$1" want="$2"
            local got
            got=$(grep -oE "$label \`[0-9][^\`]*\`" "$RUNBOOK" | head -1 | cut -d'`' -f2)
            if [[ -z "$got" ]]; then
                echo "  SKIP  $label -- not stated in the runbook"
            elif [[ "$got" != "$want" ]]; then
                echo "  DRIFT $label: runbook says $got, companion says $want"
                fails=$((fails + 1))
            else
                echo "  ok    $label $want"
            fi
        }
        echo "Comparing ${RUNBOOK#$ROOT/} against companion STAGE_VERSION:"
        check_doc planner "$PLANNER_VERSION"
        check_doc doci    "$DOCI_VERSION"
        check_doc mesh    "$MESH_VERSION"
        if (( fails > 0 )); then
            echo
            echo "$fails version(s) drifted. The companion scripts are authoritative --"
            echo "update the runbook, not the scripts."
            exit 1
        fi
        echo "All stated versions agree."
        ;;
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        ;;
    *)
        die "unknown mode '$MODE' (use --help)"
        ;;
esac
