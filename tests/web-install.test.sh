#!/usr/bin/env bash
# tools/web-install: the page asks how the AI will work, starts the install with the
# answer and draws its progress from the install's log. A fake install writes the markers
# both installers write; nothing is installed.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
check() { if [ "$2" = "$3" ]; then echo "[OK]   $1"; else echo "[FAIL] $1: got '$2', wanted '$3'"; fail=1; fi; }
W=$(mktemp -d); L="$W/log"; K="$W/link"; PORT=$((20000 + RANDOM % 20000))
U="http://127.0.0.1:$PORT"
J() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

start() {  # $1 = the fake install
    python3 "$ROOT/tools/web-install/server.py" --port "$PORT" --token T --run "$1" --log "$L" --link-file "$K" >"$W/out" 2>&1 &
    P=$!
    for _ in $(seq 50); do curl -s -o /dev/null "$U/ui/tokens.css" && return; sleep 0.1; done
}
progress() { curl -s "$U/api/progress?t=T"; }
wait_state() { for _ in $(seq 60); do s=$(progress | J 'd["state"]'); [ "$s" = "$1" ] && return; sleep 0.2; done; }

# 1. the install that ends well
: > "$L"
start "echo 'LOCAL Layer A skipped' >> $L; for b in B01 B02 B03 B04 B05 B06; do echo \"== \$b verify exit=0\" >> $L; done; echo 'http://studio.x/welcome/?token=abc&way=api' > $K"
check "the page needs the token" "$(curl -s -o /dev/null -w '%{http_code}' "$U/api/ways")" 403
check "the page is served with it" "$(curl -s -o /dev/null -w '%{http_code}' "$U/?t=T")" 200
check "the icons are Studio's own Lucide" "$(curl -s -o /dev/null -w '%{http_code}' "$U/vendor/lucide.min.js")" 200
check "the UI kit is linked, not copied" "$(curl -s -o /dev/null -w '%{http_code}' "$U/ui/tokens.css")" 200
grep -q -- '--sn-' "$ROOT/tools/web-install/index.html"; check "the page uses the kit's tokens" "$?" 0
ways=$(curl -s "$U/api/ways?t=T" | J '",".join(w["id"] for w in d)')
check "the answers come from the manifests" "$ways" "agent-runtimes,key"
rt=$(curl -s "$U/api/ways?t=T" | J '",".join((w["runtime"] or {}).get("title", "-") for w in d)')
check "a subscription shows what it adds; a key adds nothing (Flint is in the base)" "$rt" "Claude Code runtime,-"
check "the state before an answer" "$(progress | J 'd["state"]')" idle
check "an unknown answer is refused" "$(curl -s -X POST -d '{"way":"x"}' "$U/api/start?t=T" | J 'd["started"]')" False
check "an answer starts the install" "$(curl -s -X POST -d '{"way":"key"}' "$U/api/start?t=T" | J 'd["started"]')" True
check "a second start is refused" "$(curl -s -X POST -d '{"way":"key"}' "$U/api/start?t=T" | J 'd["started"]')" False
wait_state done
d=$(progress)
check "the install ends done" "$(echo "$d" | J 'd["state"]')" done
check "the base steps are done" "$(echo "$d" | J '",".join(s["status"] for s in d["steps"][:7])')" "done,done,done,done,done,done,done"
check "the modules of the answer are listed" "$(echo "$d" | J '"flint" in [s["id"] for s in d["steps"]] and "agent-runtimes" not in [s["id"] for s in d["steps"]]')" True
check "the first-run link is handed over" "$(echo "$d" | J 'd["link"]')" "http://studio.x/welcome/?token=abc&way=api"
kill $P 2>/dev/null; wait $P 2>/dev/null

# 2. the install that stops
: > "$L"; rm -f "$K"
start "echo '== B01 verify exit=0' >> $L; echo '== B02 run exit=3 in 2s' >> $L; exit 3"
curl -s -X POST -d '{"way":"agent-runtimes"}' "$U/api/start?t=T" >/dev/null
wait_state failed
d=$(progress)
check "a stopped install says so" "$(echo "$d" | J 'd["state"]')" failed
check "the step that stopped is marked" "$(echo "$d" | J '[s["id"] for s in d["steps"] if s["status"]=="failed"][0]')" B02
check "no link after a stop" "$(echo "$d" | J 'd["link"]')" ""
kill $P 2>/dev/null; wait $P 2>/dev/null
# 3. Simple / Full / Custom: the page sends the modules; what they need comes with them
: > "$L"; rm -f "$K"
start "sleep 47.31"
mods=$(curl -s "$U/api/modules?t=T" | J '",".join(m["id"] for m in d)')
case ",$mods," in *",documents,"*",tracking,"*|*",tracking,"*",documents,"*) r=0 ;; *) r=1 ;; esac
check "the modules a person may leave out are offered" "$r" 0
case ",$mods," in *",flint,"*|*",agent-runtimes,"*) r=1 ;; *) r=0 ;; esac
check "core and runtimes are not offered" "$r" 0
check "every module has its icon" "$(curl -s "$U/api/modules?t=T" | J 'all(m["icon"] != "box" for m in d)')" True
base=$(curl -s "$U/api/base?t=T" | J '",".join(x["title"] for x in d)')
case ",$base," in *",Flint runtime,"*) r=0 ;; *) r=1 ;; esac; check "the base is listed as always installed" "$r" 0
check "an unknown module is refused" "$(curl -s -X POST -d '{"way":"key","modules":["nope"]}' "$U/api/start?t=T" | J 'd["started"]')" False
check "Custom: only Tasks" "$(curl -s -X POST -d '{"way":"key","modules":["tracking"]}' "$U/api/start?t=T" | J 'd["started"]')" True
ids=$(progress | J '",".join(s["id"] for s in d["steps"])')
case ",$ids," in *",documents,"*) r=1 ;; *) r=0 ;; esac; check "Tasks alone leaves Documents out" "$r" 0
case ",$ids," in *",memory,"*) r=1 ;; *) r=0 ;; esac; check "what was left out is not installed" "$r" 0
# 4. the stop button
check "a running install can be stopped" "$(curl -s -X POST "$U/api/stop?t=T" | J 'd["stopped"]')" True
sleep 0.5
check "it says it is stopped" "$(progress | J 'd["state"]')" stopped
pgrep -f "^sleep 47.31$" >/dev/null; check "the install's process is gone" "$?" 1
check "a stopped install cannot be stopped again" "$(curl -s -X POST "$U/api/stop?t=T" | J 'd["stopped"]')" False
kill $P 2>/dev/null; wait $P 2>/dev/null

# 5. without --run the page starts install-on.sh itself, and what was chosen reaches it.
# Simple once installed every module: the choice was drawn on the page and never passed.
F="$W/tree"; mkdir -p "$F/tools/web-install" "$F/orchestrator" "$F/companions"
cp "$ROOT/tools/web-install/server.py" "$ROOT/tools/web-install/index.html" "$F/tools/web-install/"
cp "$ROOT/tools/sn-components" "$F/tools/"; cp "$ROOT"/companions/*.manifest.json "$F/companions/"
printf '#!/bin/sh\necho "$@" > "%s"\n' "$W/args" > "$F/orchestrator/install-on.sh"; chmod +x "$F/orchestrator/install-on.sh"
installed() {  # $1 = the start request; prints the --components install-on.sh was given
    rm -f "$W/args"
    python3 "$F/tools/web-install/server.py" --port "$PORT" --token T --link-file "$K" >"$W/out" 2>&1 &
    P=$!
    for _ in $(seq 50); do curl -s -o /dev/null "$U/api/ways?t=T" && break; sleep 0.1; done
    curl -s -X POST -d "$1" "$U/api/start?t=T" >/dev/null
    for _ in $(seq 50); do [ -s "$W/args" ] && break; sleep 0.1; done
    kill $P 2>/dev/null; wait $P 2>/dev/null
    sed -n 's/.*--components \([^ ]*\).*/\1/p' "$W/args"
}
core=$(cd "$ROOT" && python3 -c 'import json,glob; print(",".join(sorted(json.load(open(p))["name"] for p in glob.glob("companions/*.manifest.json") if json.load(open(p)).get("core"))))')
check "Simple installs the core and nothing else" "$(installed '{"way":"key","modules":[]}')" "$core"
check "Custom installs what was ticked" "$(installed '{"way":"key","modules":["tracking"]}')" "tracking"
full=$(installed '{"way":"key"}')
case ",$full," in *",memory,"*",workflows,"*) r=0 ;; *) r=1 ;; esac; check "Full installs every module" "$r" 0
rm -rf "$W"
exit "$fail"
