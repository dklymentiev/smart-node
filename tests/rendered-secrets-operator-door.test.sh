#!/bin/bash
# Regression contract for
#
# Run the same test against the released source to prove the unfixed behavior fails:
#   SMART_NODE_SOURCE_ROOT=/path/to/main tests/rendered-secrets-operator-door.test.sh

set -uo pipefail

SELF_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${SMART_NODE_SOURCE_ROOT:-$SELF_ROOT}"
CORE="$ROOT/companions/core-api.sh"
INSTALLER="$ROOT/orchestrator/install-on.sh"
UPDATER="$ROOT/tools/update-on.sh"

pass=0
fail=0
ok() { echo "ok - $1"; pass=$((pass + 1)); }
bad() { echo "not ok - $1"; fail=$((fail + 1)); }

has() {
    local file="$1" pattern="$2" label="$3"
    if grep -Eq -- "$pattern" "$file"; then ok "$label"; else bad "$label"; fi
}

lacks() {
    local file="$1" pattern="$2" label="$3"
    if grep -Eq -- "$pattern" "$file"; then bad "$label"; else ok "$label"; fi
}

# The operator makes one explicit node-level choice. Both a fresh install and
# the supported update path must carry it to the companion that owns core-api.
has "$INSTALLER" '--secrets-operator-users' \
    "node installer accepts the declared service-user list"
has "$INSTALLER" 'SMART_NODE_SECRETS_OPERATOR_USERS=' \
    "node installer passes the declaration to core-api"
has "$UPDATER" '--secrets-operator-users' \
    "update path forwards the same declaration"

# core-api renders the two required halves from that declaration: OS access to
# the Unix socket and the application allow-list consumed by /resolve.
has "$CORE" 'SECRETS_OPERATORS_ENV="/etc/smart-node/secrets-operators.env"' \
    "core-api owns a rendered secrets-operator contract"
has "$CORE" 'EnvironmentFile=-\$SECRETS_OPERATORS_ENV' \
    "the generated unit consumes the rendered contract"
has "$CORE" 'SN_SECRETS_OPERATOR_USERS=' \
    "the contract renders the application allow-list"
has "$CORE" 'usermod -a -G "\$SMART_NODE_AGENT_GROUP" "\$user"' \
    "the same renderer grants each declared user socket access"
has "$CORE" '_envfile_get "\$SECRETS_OPERATORS_ENV" SN_SECRETS_OPERATOR_USERS' \
    "an update preserves the existing declaration when no override is supplied"
has "$CORE" 'LEGACY_SECRETS_OPERATORS_DROPIN=' \
    "the known field hotfix has an explicit migration path"
has "$CORE" 'rm -f "\$LEGACY_SECRETS_OPERATORS_DROPIN"' \
    "the hand-written drop-in is retired after rendering its replacement"
has "$CORE" 'verify.*secrets operator.*SMART_NODE_AGENT_GROUP|secrets operator.*group.*SMART_NODE_AGENT_GROUP' \
    "verify checks the effective socket-group membership"

# Product source must stay node-agnostic. example-node service names belong in the
# install invocation/configuration, never in the shipped trust policy.
lacks "$CORE" 'sn-google-ads-mcp|sn-analytics-mcp' \
    "product policy does not hardcode this node's service principals"

# Exercise the real canonicalisation function without touching host accounts or
# /etc. The renderer consumes this exact output for both the env file and group
# loop, so duplicate/order/carry-forward behavior cannot drift between halves.
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
sed -n '/^desired_secrets_operator_users()/,/^}/p' "$CORE" > "$TMPD/function.sh"
if (
    set -uo pipefail
    source "$TMPD/function.sh"
    SECRETS_OPERATORS_ENV="$TMPD/operators.env"
    SMART_NODE_SECRETS_OPERATOR_USERS="svc-one,root,svc-two,svc-one"
    _envfile_get() { sed -n -E "s/^$2=//p" "$1" | tail -n1; }
    die() { exit 1; }
    [[ "$(desired_secrets_operator_users)" == "root,svc-one,svc-two" ]]
); then
    ok "operator declaration is canonical and always retains root"
else
    bad "operator declaration is canonical and always retains root"
fi

printf 'SN_SECRETS_OPERATOR_USERS=root,svc-existing\n' > "$TMPD/operators.env"
if (
    set -uo pipefail
    source "$TMPD/function.sh"
    SECRETS_OPERATORS_ENV="$TMPD/operators.env"
    unset SMART_NODE_SECRETS_OPERATOR_USERS
    _envfile_get() { sed -n -E "s/^$2=//p" "$1" | tail -n1; }
    die() { exit 1; }
    [[ "$(desired_secrets_operator_users)" == "root,svc-existing" ]]
); then
    ok "an update carries the rendered operator declaration forward"
else
    bad "an update carries the rendered operator declaration forward"
fi

rm -f "$TMPD/operators.env"
printf 'Environment="SN_SECRETS_OPERATOR_USERS=root,svc-legacy"\n' > "$TMPD/legacy.conf"
if (
    set -uo pipefail
    source "$TMPD/function.sh"
    SECRETS_OPERATORS_ENV="$TMPD/operators.env"
    LEGACY_SECRETS_OPERATORS_DROPIN="$TMPD/legacy.conf"
    unset SMART_NODE_SECRETS_OPERATOR_USERS
    _envfile_get() { sed -n -E "s/^$2=//p" "$1" | tail -n1; }
    die() { exit 1; }
    [[ "$(desired_secrets_operator_users)" == "root,svc-legacy" ]]
); then
    ok "the first update imports the field hotfix before retiring it"
else
    bad "the first update imports the field hotfix before retiring it"
fi

if (
    set -uo pipefail
    source "$TMPD/function.sh"
    SECRETS_OPERATORS_ENV="$TMPD/operators.env"
    SMART_NODE_SECRETS_OPERATOR_USERS='svc-ok,$not-a-user'
    _envfile_get() { return 0; }
    die() { exit 1; }
    desired_secrets_operator_users >/dev/null
); then
    bad "invalid service principals are refused"
else
    ok "invalid service principals are refused"
fi

echo "$pass passed, $fail failed"
(( fail == 0 ))
