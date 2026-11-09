#!/usr/bin/env bash
#
# web-local.test.sh -- a node named localhost or *.localhost is served on plain
# http; every other node renders exactly what it rendered before (https on
# websecure, `tls: {}`).
#
# A person installing a node on their own machine met three certificate
# warnings in a row before the first screen (studio, auth, admin), and a
# self-signed certificate can only be made trusted per system and per browser.
# The routers now take their entrypoint, tls line and scheme from lib.sh, so
# this pins both sides of the one rule.
#
#   tests/web-local.test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail=0
check() { # name expected actual
    if [[ "$2" == "$3" ]]; then echo "[OK]   $1"; else echo "[FAIL] $1: expected '$2', got '$3'"; fail=1; fi
}

# shellcheck source=/dev/null
source <(sed -n '/^web_local() {/,/^web_tls_yaml()/p' "$ROOT/bootstrap/lib.sh")

for d in localhost sn.localhost a.b.localhost; do
    check "$d scheme"   http "$(web_scheme "$d")"
    check "$d entry"    web  "$(web_entry "$d")"
    check "$d tls line" "# plain http: a local node" "$(web_tls_yaml "$d")"
done
for d in example.com localhost.example.com notlocalhost ""; do
    check "'$d' scheme"   https     "$(web_scheme "$d")"
    check "'$d' entry"    websecure "$(web_entry "$d")"
    check "'$d' tls line" "tls: {}" "$(web_tls_yaml "$d")"
done

# The entrypoint the routers name is the helper's, not a literal: a literal
# "websecure" left in a public router would keep that router off a local node.
left=$(grep -nE 'entryPoints: \["websecure"\]|entrypoints=websecure' \
    "$ROOT"/bootstrap/B05-identity.sh "$ROOT"/bootstrap/B06-web-interface.sh \
    "$ROOT"/companions/{studio,core-api,hitl}.sh || true)
check "no router names websecure literally" "" "$left"

exit $fail
