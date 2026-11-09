#!/usr/bin/env bash
#
# The onboarding seed addresses an existing setup agent by capability, while
# leaving its business onboarding state alone.
#
# WHAT WENT WRONG. 1.12.38 made provision_agent find the setup agent by role, so
# a renamed one is no longer cloned. Every step after it still addressed the
# agent as "foreman". On the update of the node that reported it, the seed found
# 'john' (role Foreman), skipped provisioning, then PUT grants for "foreman"
# (404), registered a planner actor "foreman" and seeded another "Configure this
# business" task for an actor with no agent behind it. The owner closes that
# task by hand after every update.
#
# HOW THIS TEST ASKS. It runs the real script, with curl replaced on PATH by a
# stub that records every request and answers from a canned roster. Nothing
# leaves the machine. The question is behaviour, not wording: what does the
# seed WRITE, for each state a node can be in.
#
# THE PROPERTIES:
#   1. a renamed setup agent gets only a non-narrowing grant merge addressed to
#      its real slug (no duplicate planner actor/task or desk)
#   2. a half-seeded fresh node (setup agent under the canonical slug) still
#      resumes: its grants are written, and its onboarding task is seeded
#   3. a fresh node with no agents is provisioned
#   4. every run exits 0 (the seed never fails an install)
#   5. a task the owner closed is not seeded again, on a node with the canonical slug
#
#   tests/onboarding-seed-leaves-a-renamed-node-alone.test.sh                # tree, PASS
#   tests/onboarding-seed-leaves-a-renamed-node-alone.test.sh --baseline REF # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "onboarding seed: renamed setup agent is merged safely (git:$BASELINE)"
else echo "onboarding seed: renamed setup agent is merged safely (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
mkdir -p "$D/bin"
read_src bootstrap/onboarding-seed.sh > "$D/seed.sh"
read_src bootstrap/srv-skeleton/agents/_specialists/configurator.md > "$D/configurator.md"
[[ -s "$D/seed.sh" ]] || { bad "onboarding-seed.sh is readable"; echo; echo "FAIL"; exit 1; }

# The stub: parses the flags the seed uses, logs "METHOD URL", writes the canned
# body where -o asks, prints 200 where -w asks.
cat > "$D/bin/curl" <<'STUB'
#!/usr/bin/env bash
method=""; url=""; out=""; wfmt=""; data=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -X) method="$2"; shift 2 ;;
        -o) out="$2"; shift 2 ;;
        -w) wfmt="$2"; shift 2 ;;
        -d|--data|--data-binary|--data-raw) data=1; shift 2 ;;
        -H|-m|--max-time) shift 2 ;;
        http://*|https://*) url="$1"; shift ;;
        *) shift ;;
    esac
done
[[ -z "$method" ]] && { [[ $data -eq 1 ]] && method=POST || method=GET; }
echo "$method $url" >> "$STUB_LOG"
body='{}'
case "$method $url" in
    "GET "*/api/agents)       body="$STUB_ROSTER" ;;
    "GET "*/api/agents\?*)    body="$STUB_ROSTER" ;;
    "GET "*/api/desks*)       body='{"desks":[]}' ;;
    "GET "*/crons*|"GET "*cron*) body='[]' ;;
    "GET "*task*)             body="${STUB_TASKS:-[]}" ;;
esac
if [[ -n "$out" ]]; then printf '%s' "$body" > "$out"; else printf '%s' "$body"; fi
[[ -n "$wfmt" ]] && printf '200'
exit 0
STUB
chmod +x "$D/bin/curl"

# The seed refuses to start without core-api's unix socket. The stub answers
# every request, so the socket only has to exist.
python3 -c 'import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$D/core.sock"

# One run of the seed against a roster. Prints the exit code; the log is $D/log.<name>.
run_seed() { # NAME ROSTER_JSON [TASKS_JSON]
    : > "$D/log.$1"
    # env -i: the seed sources /etc/smart-node/core-api.env when it exists, and a
    # real address there must not matter -- every request goes to the stub anyway.
    env -i PATH="$D/bin:/usr/bin:/bin" HOME="$D" STUB_LOG="$D/log.$1" STUB_ROSTER="$2" STUB_TASKS="${3:-[]}" \
        SN_CORE_API="http://stub.invalid" SN_SPECIALIST_MD="$D/configurator.md" \
        SN_AGENTS_ROOT="$D/agents" SN_CORE_API_SOCKET="$D/core.sock" \
        bash "$D/seed.sh" > "$D/out.$1" 2>&1
    echo $?
}
writes() { grep -v '^GET ' "$D/log.$1" 2>/dev/null; }

# "renamed" must not be the canonical slug (bootstrap/onboarding-seed.sh AGENT_SLUG).
rc_renamed=$(run_seed renamed '[{"slug":"maria","title":"Maria","role":"Configurator","capability":"configurator"}]')
rc_resume=$(run_seed resume '[{"slug":"john","title":"John","role":"Configurator","capability":"configurator"}]')
rc_closed=$(run_seed closed '[{"slug":"john","title":"John","role":"Configurator","capability":"configurator"}]' \
    '[{"title":"Configure this business","status":"done"}]')
rc_fresh=$(run_seed fresh '[]')

w=$(writes renamed)
# The host part differs by transport; the one write and its path are the claim.
if [[ "$w" =~ ^PUT\ http://[^/]+/api/agents/maria/grants\?merge=true$ ]]; then
    ok "a renamed setup agent gets only its non-narrowing grant merge"
else
    bad "a renamed setup agent gets only its non-narrowing grant merge -- sent: $(tr '\n' ';' <<<"$w")"
fi

if writes resume | grep -Eq '^PUT .*/api/agents/john/grants\?merge=true$' \
   && writes resume | grep -Eq '^POST .*/tasks$'; then
    ok "a half-seeded fresh node still resumes (grants written)"
else
    bad "a half-seeded fresh node still resumes -- sent: $(writes resume | tr '\n' ';')"
fi

if writes fresh | grep -Eq '^POST .*/api/agents$'; then
    ok "a fresh node with no agents is provisioned"
else
    bad "a fresh node with no agents is provisioned -- sent: $(writes fresh | tr '\n' ';')"
fi

if writes closed | grep -Eq '^POST .*/tasks$'; then
    bad "a task the owner closed is not seeded again -- sent: $(writes closed | tr '\n' ';')"
else
    ok "a task the owner closed is not seeded again"
fi

if [[ "$rc_renamed" == 0 && "$rc_resume" == 0 && "$rc_fresh" == 0 && "$rc_closed" == 0 ]]; then
    ok "every run exits 0"
else
    bad "every run exits 0 -- renamed=$rc_renamed resume=$rc_resume fresh=$rc_fresh"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
