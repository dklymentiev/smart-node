#!/usr/bin/env bash
#
# classes-check.sh -- the class declaration and the contract must agree.
#
# node-versioning/classes.json says WHERE each class lives (a schema-qualified
# registry). docs/node/entities.md says HOW to change it (the call, the gate, the
# proof). Neither repeats the other's column, so the only way they can rot is by
# describing DIFFERENT SETS of things -- a class with no contract row, or a
# contract row belonging to no class. That is what this catches.
#
# It also catches the defect that started all of this: a registry that names a
# table nothing creates, or that names the empty twin of a real table.
#
# Not a generator. entities.md carries a "Proved by" column and a section called
# "Four that trip people" -- knowledge a person wrote, which a generator would
# delete on its first run.
#
# Exit 0 = they agree. Exit 1 = they do not, and every disagreement is named.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLASSES="$ROOT/node-versioning/classes.json"
ENT="$ROOT/docs/node/entities.md"
SCHEMA="$ROOT/bootstrap/B02-sn-core-schema.sql"

fail=0
note() { printf '%-22s %s\n' "$1" "$2"; fail=1; }

for f in "$CLASSES" "$ENT" "$SCHEMA"; do
    [[ -f "$f" ]] || { printf 'MISSING  %s\n' "$f"; exit 1; }
done

command -v jq >/dev/null 2>&1 || { printf 'jq is required and is not installed\n'; exit 1; }

if ! jq empty "$CLASSES" 2>/dev/null; then
    printf 'classes.json is not valid JSON\n'
    exit 1
fi

# ---------------------------------------------------------------- the two sets
# Contract rows: the bolded Thing in the first cell of a table row. Continuation
# rows have an empty first cell and are deliberately skipped -- they are further
# calls for the thing above, not things of their own.
mapfile -t contract_rows < <(grep -oE '^\| \*\*[^*]+\*\*' "$ENT" | sed 's/^| \*\*//; s/\*\*$//' | sort -u)
mapfile -t claimed      < <(jq -r '.classes[] | select(.contract != null) | .contract' "$CLASSES" | sort -u)

for row in "${contract_rows[@]}"; do
    grep -qxF -- "$row" <<< "$(printf '%s\n' "${claimed[@]}")" \
        || note "CONTRACT ROW ORPHAN" "entities.md has '$row' and no class claims it"
done

for c in "${claimed[@]}"; do
    grep -qxF -- "$c" <<< "$(printf '%s\n' "${contract_rows[@]}")" \
        || note "CLASS WITHOUT A ROW" "a class claims contract '$c', entities.md has no such row"
done

# A contract row may be claimed once. Twice means two classes think they are the
# same thing, which is how one home per artifact stops being true.
dupes="$(jq -r '.classes[] | select(.contract != null) | .contract' "$CLASSES" | sort | uniq -d)"
[[ -n "$dupes" ]] && while read -r d; do
    note "CONTRACT CLAIMED TWICE" "$d"
done <<< "$dupes"

# ------------------------------------------------------- registries are real
# A table registry must be schema-qualified AND created by the schema that runs
# at install. bootstrap/B02-sn-core-schema.sql is that schema; companions/core-api
# /migrations is reference only and there is no migration runner.
while read -r cls reg; do
    [[ "$reg" == *.* ]] || { note "REGISTRY NOT QUALIFIED" "$cls -> '$reg' has no schema"; continue; }
    grep -qE "CREATE TABLE (IF NOT EXISTS )?${reg}[[:space:](]" "$SCHEMA" \
        || note "REGISTRY NOT CREATED" "$cls -> $reg is declared and B02 does not create it"
done < <(jq -r '.classes[] | select(.kind == "table") | "\(.class) \(.registry)"' "$CLASSES")

# ------------------------------------------------------------- no twin as home
# The whole point. A class may never point at the empty twin of a live table.
while read -r twin; do
    hit="$(jq -r --arg t "$twin" '.classes[] | select(.registry == $t) | .class' "$CLASSES")"
    [[ -n "$hit" ]] && note "REGISTRY IS A TWIN" "$hit -> $twin is listed as a legacy duplicate, not a home"
done < <(jq -r '.legacy_duplicates[].twin' "$CLASSES")

# A class in the gap state must not claim a registry: nothing may offer it yet.
while read -r cls; do
    note "GAP WITH A REGISTRY" "$cls is status gap and names a registry"
done < <(jq -r '.classes[] | select(.status == "gap" and .registry != null) | .class' "$CLASSES")

# ------------------------------------------------------- filesystem_homes lint
# every declared /work class home carries a writer_group and a reader_groups
# list, or explicitly declares itself root-owned (writer_group = null AND owner=root
# AND group=root). Any other combination is refused: a class home that has no writer
# group and is not root-owned cannot be resolved to a chgrp target at install time.
#
# Group name shape: grp-role-<capability>, grp-prj-<slug>, or the fixed marker grp-agents.
# The runtime-only smart-node-agents alias for grp-agents is not accepted here: this
# file is the source of truth, and the group migration renames the group.
homes_len="$(jq '.filesystem_homes | length // 0' "$CLASSES")"
if [[ "$homes_len" -eq 0 ]]; then
    note "FILESYSTEM_HOMES EMPTY" "classes.json declares no filesystem_homes; the installer requires the /work path table"
fi

declare -A seen_path
while read -r path wg owner group; do
    [[ -z "$path" ]] && continue
    if [[ -n "${seen_path[$path]:-}" ]]; then
        note "HOME DECLARED TWICE" "$path"
    else
        seen_path["$path"]=1
    fi
    [[ "$path" =~ ^/work/ ]] \
        || note "HOME OUTSIDE /work" "$path (filesystem_homes covers /work only)"
    if [[ "$wg" == "null" ]]; then
        # root-owned home: must declare owner=root AND group=root explicitly
        [[ "$owner" == "root" && "$group" == "root" ]] \
            || note "HOME NO WRITER GROUP" "$path has no writer_group and is not root:root (owner=$owner group=$group)"
    else
        # named writer group: shape check
        case "$wg" in
            grp-role-*|grp-prj-*|grp-agents) : ;;
            *) note "WRITER GROUP SHAPE" "$path writer_group='$wg' does not match grp-role-*, grp-prj-* or grp-agents" ;;
        esac
    fi
done < <(jq -r '.filesystem_homes[] | "\(.path)\t\(.writer_group)\t\(.owner // "-")\t\(.group // "-")"' "$CLASSES")

# reader_groups entries share the same shape
while read -r path rg; do
    [[ -z "$rg" || "$rg" == "null" ]] && continue
    case "$rg" in
        grp-role-*|grp-prj-*|grp-agents) : ;;
        *) note "READER GROUP SHAPE" "$path reader_groups entry '$rg' does not match grp-role-*, grp-prj-* or grp-agents" ;;
    esac
done < <(jq -r '.filesystem_homes[] | .path as $p | (.reader_groups // [])[] | "\($p)\t\(.)"' "$CLASSES")

# ------------------------------------------------------------------- verdict
if [[ "$fail" -eq 0 ]]; then
    n="$(jq '.classes | length' "$CLASSES")"
    r="${#contract_rows[@]}"
    printf 'OK  %s classes declared, %s contract rows, and they agree\n' "$n" "$r"
else
    printf '\nclasses.json and docs/node/entities.md describe different systems.\n'
    printf 'Fix the one that is wrong about the NODE, not the one that is easier to edit.\n'
fi

exit "$fail"
