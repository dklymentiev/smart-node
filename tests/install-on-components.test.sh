#!/usr/bin/env bash
# install-on.sh --components: only the chosen modules, their requirements and the core
# are installed; with no choice nothing changes for an unattended install.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="$ROOT/orchestrator/install-on.sh"
fail=0
check() { if [ "$2" = "$3" ]; then echo "[OK]   $1"; else echo "[FAIL] $1: got '$2', wanted '$3'"; fail=1; fi; }

grep -q -- '--components) *COMPONENTS="\$2"; shift 2' "$S"; check "the flag is parsed" "$?" 0
grep -q -- '^#   --components' "$S"; check "the flag is documented" "$?" 0

# the decision itself, taken out of the script
eval "$(sed -n '/^component_wanted() {/,/^}/p' "$S")"
declare -F component_wanted >/dev/null; check "there is one place that decides" "$?" 0
log() { :; }
WANTED_COMPONENTS=""
component_wanted memory; check "no choice: everything, as before" "$?" 0
WANTED_COMPONENTS=" core-api flint "
component_wanted flint; check "a chosen one runs" "$?" 0
component_wanted memory; check "another does not" "$?" 1

# requirements and core come from the manifests
order=$(SN_SRC="$ROOT" python3 "$ROOT/tools/sn-components" plan tracking,documents | sed -n 's/^order: *//p')
case " $order " in *" documents "*"tracking "*) r=0 ;; *) r=1 ;; esac; check "with both, Documents goes before Tasks" "$r" 0
order=$(SN_SRC="$ROOT" python3 "$ROOT/tools/sn-components" plan tracking | sed -n 's/^order: *//p')
case " $order " in *" documents "*) r=1 ;; *) r=0 ;; esac; check "Tasks alone does not bring Documents" "$r" 0
case " $order " in *" core-api "*) r=0 ;; *) r=1 ;; esac; check "the core always comes" "$r" 0

# every install step goes through the same gate
n=$(awk '/^run_companion\(\) \{/,/^}/' "$S" | grep -c 'component_wanted "\$name"')
check "run_companion asks it" "$n" 1
grep -q 'run_companion flint' "$S"; check "Flint can be installed at all" "$?" 0
# a re-run passes --contracts-only to a companion whose version is unchanged
for c in $(grep -oE '^ *run_companion [a-z-]+' "$S" | awk '{print $2}' | sort -u); do
  grep -q -- '--contracts-only' "$ROOT/companions/$c.sh"; check "$c takes --contracts-only" "$?" 0
done
f=$(grep -n 'run_companion flint' "$S" | head -1 | cut -d: -f1); g=$(grep -n '^ *run_companion gateway' "$S" | head -1 | cut -d: -f1)
[ "$f" -lt "$g" ]; check "Flint is in place before the gateway looks for it" "$?" 0
grep -q 'chmod -R go-w' "$ROOT/companions/flint.sh"; check "Flint leaves nothing group-writable" "$?" 0
# "how will your AI work?": one question, the modules follow from the answer
grep -q -- '--ai) *AI_WAY="\$2"; shift 2' "$S"; check "--ai is parsed" "$?" 0
grep -q -- '^#   --ai WAY' "$S"; check "--ai is documented" "$?" 0
C() { SN_SRC="$ROOT" python3 "$ROOT/tools/sn-components" "$@"; }
key=$(C for-way key)
case ",$key," in *",agent-runtimes,"*) r=1 ;; *) r=0 ;; esac; check "an API key brings no Claude Code" "$r" 0
case ",$key," in *",memory,"*) r=0 ;; *) r=1 ;; esac; check "an API key keeps the other modules" "$r" 0
claude=$(C for-way agent-runtimes)
case ",$claude," in *",agent-runtimes,"*) r=0 ;; *) r=1 ;; esac; check "a Claude subscription brings Claude Code" "$r" 0
order=$(C plan "$key" | sed -n 's/^order: *//p')
case " $order " in *" flint "*) r=0 ;; *) r=1 ;; esac; check "Flint is on every node" "$r" 0
C for-way nonsense >/dev/null 2>&1; check "an unknown way is refused" "$?" 1
w=$(printf '9\n1\n' | C ways 2>/dev/null); check "the question takes a number and ignores a wrong one" "$w" "agent-runtimes"
w=$(C ways </dev/null 2>/dev/null); check "no one answering: no answer" "$w" ""
grep -q 'component_wanted flint' "$S"; check "Flint goes through the same gate as every module" "$?" 0
# a module whose script refuses to run without another one says so in its manifest, so an
# install that follows the plan (the web installer, the laptop's local install) puts it after
for f in "$ROOT"/companions/*.sh; do
  n=$(basename "$f" .sh); [ -f "$ROOT/companions/$n.manifest.json" ] || continue
  for dep in $(grep -oE 'ERROR: [a-z-]+ is not installed' "$f" | awk '{print $2}' | sort -u); do
    python3 -c "import json,sys; sys.exit(0 if sys.argv[2] in json.load(open(sys.argv[1]))['requires'] else 1)" \
      "$ROOT/companions/$n.manifest.json" "$dep"
    check "$n requires $dep in its manifest" "$?" 0
  done
done
exit "$fail"
