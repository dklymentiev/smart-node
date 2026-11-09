#!/usr/bin/env bash
#
# A public route is written only for a name that can be reached.
#
# WHAT WENT WRONG. The installer INVENTS public hostnames. Declare a domain and
# it derives panel.<domain>, studio.<domain>, voice.<domain>, doci.<domain>,
# dashboard.<domain> and auth.<domain>, then writes a Traefik router for each.
# Declaring a domain is not the same as having published a record for every
# name the installer can think of, and nothing checked the difference.
#
# Measured on a live node 2026-09-09: dashboard.<domain> and doci.<domain> had
# no DNS record at all. Traefik served routers nobody could reach, and the one
# carrying a certResolver asked Let's Encrypt for a certificate it could never
# validate -- five failed authorizations and the ACME account was rate limited
# for that name. The install was green throughout: a rejected or unreachable
# router is not a failed install, and validate does not read Traefik.
#
# It matters more for a node that is not ours. This is an open-source
# installer; whoever runs it declares THEIR domain and the derivation then
# claims six names inside it on their behalf.
#
# THE PROPERTIES, and the refusals matter as much as the writes:
#
#   1. a name that resolves                      -> write the route
#   2. a name with no record, parent resolving   -> SKIP (a definitive answer)
#   3. neither the name nor its parent resolving -> write, and warn
#      (a resolver that cannot answer must not retire a node's front door)
#   4. no public domain at all                   -> SKIP
#   5. SMART_NODE_ROUTE_REQUIRE_DNS=0            -> write regardless
#   6. every route writer asks THIS function, and so does its verify check
#
# Property 6 is the one that rots first. A check that asks a slightly
# different question from the writer is why the route gate had
# to fix core-api a second time.
#
# DNS is stubbed, not queried: the test shadows getent with a shell function,
# so it is hermetic and gives the same answer on any machine.
#
#   tests/public-route-dns-gate.test.sh                  # working tree, PASS
#   tests/public-route-dns-gate.test.sh --baseline REF   # git:REF, expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
check(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi; }

read_src() { # PATH -> stdout
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then
    echo "public route DNS gate (git:$BASELINE)"
else
    echo "public route DNS gate (working tree)"
fi

# ---------------------------------------------------------------- behaviour
LIB=$(mktemp); read_src bootstrap/lib.sh > "$LIB"
if ! grep -q 'public_route_eligible()' "$LIB"; then
    bad "bootstrap/lib.sh defines public_route_eligible"
    rm -f "$LIB"
    echo; echo "FAIL"; exit 1
fi
ok "bootstrap/lib.sh defines public_route_eligible"

# Sourcing lib.sh needs nothing but bash; the gate touches no node state.
verdict() { # HOST [RESOLVES...] -> "WRITE"|"SKIP"
    local host="$1"; shift
    RESOLVE_OK=" $* "
    (
        # shellcheck source=/dev/null
        source "$LIB" 2>/dev/null
        # Stub the resolver. A shell function shadows the external command, so
        # the gate under test is the real one and only DNS is replaced.
        getent() { [[ "$RESOLVE_OK" == *" $2 "* ]]; }
        export -f getent 2>/dev/null || true
        if public_route_eligible "$host"; then echo "WRITE"; else echo "SKIP"; fi
    )
}

check "a name that resolves is written" \
      "$(verdict panel.example.com panel.example.com example.com)" "WRITE"
check "a name with no record, parent resolving, is SKIPPED" \
      "$(verdict dashboard.example.com example.com)" "SKIP"
check "neither resolving -> written, resolver failure must not retire a door" \
      "$(verdict panel.example.com)" "WRITE"
check "no public domain -> SKIPPED" \
      "$(verdict '' example.com)" "SKIP"
check "SMART_NODE_ROUTE_REQUIRE_DNS=0 writes regardless" \
      "$(SMART_NODE_ROUTE_REQUIRE_DNS=0 verdict dashboard.example.com example.com)" "WRITE"

# The function must print NOTHING on the deciding paths: callers use its status,
# and a function that both logs and returns a value is the earlier change shape.
out=$( (source "$LIB" 2>/dev/null; getent() { [[ "$2" == "example.com" ]]; }; \
        public_route_eligible dashboard.example.com >/dev/stdout) 2>/dev/null )
check "the gate prints nothing when it decides" "${out:-<empty>}" "<empty>"

rm -f "$LIB"

# ------------------------------------------------------- every writer asks it
for pair in \
    "bootstrap/B05-identity.sh:dashboard and public auth routes" \
    "companions/documents.sh:doci route" \
    "companions/core-api.sh:panel route" \
    "companions/studio.sh:Studio route" \
    "companions/voice.sh:voice route" ; do
    f="${pair%%:*}"; what="${pair#*:}"
    n=$(read_src "$f" | grep -c 'public_route_eligible')
    if (( n >= 2 )); then
        ok "$f asks the gate ($what; writer and verify check)"
    elif (( n == 1 )); then
        bad "$f asks the gate only once -- writer and verify check must both ask ($what)"
    else
        bad "$f never asks the gate ($what)"
    fi
done

# ------------------------------------ every route check sits behind the gate
# Counting gate calls per file cannot see a THIRD check that never asks. On the
# 1.12.36 stand B05 retired the dashboard route and its own check 15 still
# demanded it, so every update of a node without that record stopped at B05
# So read the checks themselves: in B05's verify block every
# line naming a derived public host or its route file must sit inside an
# if-block that asked public_route_eligible.
stray=$(read_src bootstrap/B05-identity.sh | awk '
    /^run_verify_checks\(\)/     { inf = 1; next }
    inf && /^}/                  { inf = 0 }
    !inf                         { next }
    /^[[:space:]]*if[[:space:]]/ { d++; g[d] = ($0 ~ /public_route_eligible/) }
    /(DASH_HOST|10-dashboard\.yml|PUB_AUTH_HOST|11-public-auth\.yml)/ {
        ok = 0; for (i = 1; i <= d; i++) if (g[i]) ok = 1
        if (!ok) print "      " $0
    }
    /^[[:space:]]*fi([[:space:];]|$)/ { if (d > 0) d-- }')
if [[ -z "$stray" ]]; then
    ok "B05: every check on a derived public route sits behind the gate"
else
    bad "B05: a check on a derived public route does not ask the gate:
$stray"
fi

# ------------------ voice: --verify derives the names the writer publishes
# The standalone `voice.sh --verify` process never derived VOICE_PUBLIC_HOST, so
# it asked the gate about an empty name, concluded "no route", and failed
# a node's 1.12.36 update on the route the writer had correctly written for
# SMART_NODE_VOICE_HOSTNAME. No stand sets that name, so a source
# property is what holds it.
vsrc=$(read_src companions/voice.sh)
vverify=$(printf '%s\n' "$vsrc" | awk '/^run_verify_mode\(\)/ {f = 1} f {print} f && /^}/ {exit}')
if printf '%s\n' "$vverify" | grep -q 'derive_domain' \
   && printf '%s\n' "$vverify" | grep -q 'derive_voice_public_host'; then
    ok "voice --verify derives the domain and the published names before checking"
else
    bad "voice --verify checks the route without deriving the names the writer uses"
fi
vwriter=$(printf '%s\n' "$vsrc" | awk '/^write_traefik_route\(\)/ {f = 1} f {print} f && /^}/ {exit}')
if printf '%s\n' "$vwriter" | grep -q 'derive_voice_public_host' \
   && printf '%s\n' "$vsrc" | grep -q '^    if voice_route_expected; then'; then
    ok "voice writer and check share one name derivation and one eligibility rule"
else
    bad "voice writer and check derive the published names separately"
fi

echo
if (( FAILED )); then echo "FAIL  $FAILED check(s)"; exit 1; fi
echo "PASS"
