#!/usr/bin/env bash
#
# vendor-drift.sh -- say out loud when a vendored companion no longer matches the
# upstream commit it claims to come from.
#
# WHY THIS EXISTS. companions/<name>/ is a vendored snapshot of a first-party
# repository, and <name>.sh syncs it onto the node with `rsync -a --delete`. The
# contract that implies -- upstream is the source of truth, the vendored copy is
# a copy -- is currently false for every companion measured on 2026-08-14:
#
#     voice          1.0.0 vs 1.0.0    10 paths differ   <- the version LIES
#     brin           1.6.2 vs 1.5.1    27
#     core-api       0.18.0            34
#     gateway        2.6.0 vs 2.5.1    16
#     communication  1.8.0 vs 1.7.0    11
#     admin-runner                     13
#     Studio         0.5.0 vs 0.5.1     9   <- forked in BOTH directions
#
# The danger is not untidiness. Anyone doing the obvious thing before a release --
# refresh the snapshot from upstream -- silently reverts every fix that only ever
# landed in the vendored copy. `rsync --delete` gives no conflict and no warning,
# and the tests pass, because the reverted code is older code that used to work.
#
# WHAT THIS DOES NOT DO, deliberately: it does not forbid divergence. Four
# companions are legitimately ahead of upstream right now and one (Studio) is
# ahead in some files and behind in others, so a checker that fails on any
# difference would fail always and be switched off within a week. Divergence
# becomes DECLARED instead: the VENDORED marker names the upstream commit and
# lists the paths that are knowingly ahead, and this fails on anything else.
#
# The marker gains one optional section, appended to the existing keys:
#
#     repo: private first-party repository
#     branch: feat/voice-ui
#     commit: 944ed0e7d61cc8f82036c8381d7d97b3f4bf6bf6
#     version: 1.0.0
#     drift: backend/main.py           -- local fixes that never went upstream
#     drift: path/to/file             -- vendored-only
#
# A `drift:` line is a claim a human made on purpose. An undeclared difference is
# the thing this exists to catch.
#
# Usage:
#   vendor-drift.sh              report; exit 1 if anything is undeclared
#   vendor-drift.sh --list       list what each companion declares, no checking
#   vendor-drift.sh --quiet      only failures
#
# WHERE IT CAN RUN. The upstream repositories are first-party and not public, so
# this needs local clones. The map below is the convention on the build host; set
# SN_VENDOR_MAP to a file of "companion<TAB>path" lines to override. A companion
# whose clone is absent is reported as SKIPPED WITH A REASON and is never counted
# as a pass -- a check that goes quiet when it cannot look is the failure this
# repository keeps finding.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPANIONS="$REPO_ROOT/companions"
CLONE_BASE="${SN_VENDOR_CLONE_BASE:-$(dirname "$REPO_ROOT")}"

MODE="check"
QUIET=0
for a in "$@"; do
    case "$a" in
        --list)  MODE="list" ;;
        --quiet) QUIET=1 ;;
        --help|-h)
            sed -n '2,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
    esac
done

say()  { [ "$QUIET" = 1 ] || echo "$*"; }
fail_n=0
skip_n=0
ok_n=0
archived_n=0

# companion -> clone directory name. Convention on this host: smart-node-<name>,
# except core-api, whose repository is the node core itself.
clone_for() {
    case "$1" in
        core-api) echo "$CLONE_BASE/smart-node-core" ;;
        *)        echo "$CLONE_BASE/smart-node-$1" ;;
    esac
}

marker_get() {  # marker_get <file> <key>
    sed -n "s/^$2:[[:space:]]*//p" "$1" | head -1
}

declared_paths() {  # every `drift:` path in a marker, one per line
    sed -n 's/^drift:[[:space:]]*//p' "$1" | awk '{print $1}'
}

echo "[vendor-drift] companions under $COMPANIONS"
echo "[vendor-drift] upstream clones under $CLONE_BASE"
echo

for dir in "$COMPANIONS"/*/; do
    name="$(basename "$dir")"
    marker="${dir%/}/VENDORED"

    # Sidecars and image payloads are not vendored applications.
    case "$name" in
        _shared|*-image|*-ops|srv-skeleton) continue ;;
    esac

    if [[ ! -f "$marker" ]]; then
        # Not every companion directory is a vendored app; but one that has an
        # upstream clone and no marker is exactly the gateway's situation and is
        # reported, not skipped.
        if [[ -d "$(clone_for "$name")" ]]; then
            echo "  FAIL $name -- has an upstream clone but NO VENDORED marker, so"
            echo "       nothing records which commit it came from or what is"
            echo "       deliberately ahead of it"
            fail_n=$((fail_n + 1))
        fi
        continue
    fi

    commit="$(marker_get "$marker" commit)"
    clone="$(clone_for "$name")"

    # A component whose upstream was archived has no second copy to drift from:
    # this tree IS the source. That is a deliberate state, so it is
    # REPORTED rather than skipped -- a checker that quietly stops covering eight
    # of twelve components is worse than one that fails.
    archived="$(marker_get "$marker" archived-upstream)"
    if [[ -n "$archived" ]]; then
        archived_n=$((archived_n + 1))
        say "  source $name -- this tree is the source; $archived archived $(marker_get "$marker" archived-on)"
        continue
    fi

    if [[ "$MODE" == "list" ]]; then
        echo "  $name  commit=${commit:0:12}  declared:"
        declared_paths "$marker" | sed 's/^/      /' || true
        continue
    fi

    if [[ -z "$commit" ]]; then
        echo "  FAIL $name -- VENDORED marker has no commit: line, so there is"
        echo "       nothing to compare against"
        fail_n=$((fail_n + 1))
        continue
    fi

    if [[ ! -d "$clone/.git" ]]; then
        echo "  SKIP $name -- no upstream clone at $clone (cannot check; NOT a pass)"
        skip_n=$((skip_n + 1))
        continue
    fi

    if ! git -C "$clone" cat-file -e "${commit}^{commit}" 2>/dev/null; then
        echo "  FAIL $name -- the marker names commit ${commit:0:12}, which does not"
        echo "       exist in $clone. The record points at nothing."
        fail_n=$((fail_n + 1))
        continue
    fi

    tmp="$(mktemp -d)"
    if ! git -C "$clone" archive "$commit" 2>/dev/null | tar -x -C "$tmp" 2>/dev/null; then
        echo "  FAIL $name -- could not extract upstream tree at ${commit:0:12}"
        rm -rf "$tmp"; fail_n=$((fail_n + 1)); continue
    fi

    # Paths that differ between the vendored copy and the commit it claims.
    # `diff -rq` speaks two shapes and they need different unpacking:
    #   Files <a>/p and <b>/p differ      -> p
    #   Only in <a>/sub: name             -> sub/name   (NOT "sub: name")
    # Getting the second wrong is how a real path turns into an unmatchable
    # string that no drift: line can ever declare.
    mapfile -t differing < <(
        diff -rq --exclude=.git --exclude=__pycache__ --exclude=venv \
             --exclude=node_modules --exclude=VENDORED --exclude=.pytest_cache \
             --exclude='*.egg-info' --exclude='*.pyc' \
             "$dir" "$tmp" 2>/dev/null \
        | awk -v a="${dir%/}" '
            /^Files /   { sub(/^Files /, ""); sub(/ and .* differ$/, "");
                          sub("^" a "/", ""); print; next }
            /^Only in /  { sub(/^Only in /, "");
                           i = index($0, ": ");
                           d = substr($0, 1, i - 1); n = substr($0, i + 2);
                           if (index(d, a) != 1) next;      # only-in-upstream: not our drift
                           sub("^" a "/?", "", d);
                           print (d == "" ? n : d "/" n); next }
          ' \
        | grep -v '^$' | sort -u
    )

    mapfile -t declared < <(declared_paths "$marker" | sort -u)

    undeclared=()
    for p in "${differing[@]}"; do
        found=0
        for d in "${declared[@]:-}"; do
            [[ -z "$d" ]] && continue
            # a declared directory covers everything under it
            if [[ "$p" == "$d" || "$p" == "$d"/* ]]; then found=1; break; fi
        done
        [[ $found -eq 0 ]] && undeclared+=("$p")
    done

    if [[ ${#undeclared[@]} -eq 0 ]]; then
        say "  ok   $name -- matches ${commit:0:12}${declared[0]:+ plus ${#declared[@]} declared path(s)}"
        ok_n=$((ok_n + 1))
    else
        echo "  FAIL $name -- ${#undeclared[@]} path(s) differ from ${commit:0:12} and are NOT declared:"
        printf '         %s\n' "${undeclared[@]}" | head -20
        [[ ${#undeclared[@]} -gt 20 ]] && echo "         ... and $(( ${#undeclared[@]} - 20 )) more"
        echo "       Either push them upstream and re-vendor, or add a drift: line"
        echo "       to $marker saying why they stay."
        fail_n=$((fail_n + 1))
    fi
    rm -rf "$tmp"
done

[[ "$MODE" == "list" ]] && exit 0

echo
echo "[vendor-drift] $ok_n ok, $fail_n undeclared, $skip_n not checkable here, $archived_n source-of-truth"
if (( skip_n > 0 )); then
    echo "[vendor-drift] the skips are not passes: those companions were not compared"
fi
if (( archived_n > 0 )); then
    echo "[vendor-drift] the $archived_n source-of-truth components have no upstream on purpose"
    echo "[vendor-drift] There is nothing to drift from; edit them here."
fi
exit $(( fail_n > 0 ? 1 : 0 ))
