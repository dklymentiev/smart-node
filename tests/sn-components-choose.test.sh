#!/usr/bin/env bash
# Choosing modules at install: the list comes from the manifests, core is never
# offered, the default runtime starts ticked, and a node with no runtime is refused.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/companions"
m() { printf '%s' "$2" > "$T/companions/$1.manifest.json"; }
m core-api '{"name":"core-api","title":"Core API","provides":"the API","requires":[],"core":true}'
m claude '{"name":"claude","title":"Claude Code","provides":"a runtime","requires":[],"runtime":true,"default":true}'
m flint '{"name":"flint","title":"Flint","provides":"another runtime","requires":[],"runtime":true}'
m memory '{"name":"memory","title":"Memory","provides":"long-term memory","requires":[]}'
choose() { printf '%b' "$1" | SN_SRC="$T" python3 "$ROOT/tools/sn-components" choose 2>"$T/screen"; }
fail=0
check() { if [ "$2" = "$3" ]; then echo "[OK]   $1"; else echo "[FAIL] $1: got '$2', wanted '$3'"; fail=1; fi; }

check "Enter takes the default runtime" "$(choose '\n')" "claude"
grep -q "Memory" "$T/screen" && grep -q "long-term memory" "$T/screen"; check "words come from the manifest" "$?" 0
grep -q "Core API" "$T/screen"; check "core is not offered" "$?" 1
check "numbers toggle" "$(choose '3\n\n')" "claude,memory"
check "several at once, and off again" "$(choose '1 2\n\n')" "flint"
check "no runtime is refused, then asked again" "$(choose '1\n\n1\n\n')" "claude"
grep -q "at least one" "$T/screen"; check "and says why" "$?" 0
check "a wrong number changes nothing" "$(choose 'x 9\n\n')" "claude"
check "no answer at all still ends" "$(choose '')" "claude"

# this release: Flint is what a node gets unless Claude Code is chosen too
real=$(SN_SRC="$ROOT" python3 "$ROOT/tools/sn-components" plan "" | sed -n 's/^order: *//p')
case " $real " in *" flint "*) r=0 ;; *) r=1 ;; esac; check "the default runtime is Flint" "$r" 0
case " $real " in *" agent-runtimes "*) r=1 ;; *) r=0 ;; esac; check "Claude Code is not installed unasked" "$r" 0
both=$(SN_SRC="$ROOT" python3 "$ROOT/tools/sn-components" plan "agent-runtimes,flint" | sed -n 's/^order: *//p')
case " $both " in *" agent-runtimes "*" "*|*" flint "*) r=0 ;; *) r=1 ;; esac; check "Claude Code can still be chosen" "$r" 0
grep -q '"title": "Claude Code runtime"' "$ROOT/companions/agent-runtimes.manifest.json"; check "the list calls it Claude Code runtime" "$?" 0
f=$(echo " $real " | sed 's/ flint .*//' | wc -w); g=$(echo " $real " | sed 's/ gateway .*//' | wc -w)
[ "$f" -lt "$g" ]; check "Flint comes before the gateway that looks for it" "$?" 0
exit "$fail"
