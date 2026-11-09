#!/usr/bin/env bash
# E2E on a THROWAWAY node: the secrets trust boundary.
#
# This is deliberately a wire test.  It runs curl as a real agent OS user so
# SO_PEERCRED, the unix-socket mode, loopback routing and the event ledger all
# participate.  A source-shape test cannot prove any of those properties.
#
# The fixture uses only refs under e2e_4658.* and removes them on exit.  Even so,
# it refuses to run unless the operator explicitly opts in and the hostname is
# recognisably a stand.  Never weaken these guards: the red half intentionally
# attempts operations which the vulnerable release accepts.
#
# Usage (node-local, as root):
#   SN_E2E_ALLOW_SECRETS=1 E2E_AGENT=<non-privileged-agent> \
#     tests/e2e/secrets-boundary.sh
set -uo pipefail

if [ "${SN_E2E_ALLOW_SECRETS:-}" != 1 ]; then
    echo "[e2e] REFUSED: set SN_E2E_ALLOW_SECRETS=1 on a throwaway stand" >&2
    exit 2
fi
case "$(hostname -s)" in
    sn-*|stand-*) ;;
    *) echo "[e2e] REFUSED: hostname is not a stand: $(hostname -s)" >&2; exit 2 ;;
esac

DB="${SMART_NODE_PG_CORE_DB:-smartnode}"
SOCK="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
TCP="${SN_CORE_API_URL:-http://127.0.0.1:8030}"
Q(){ sudo -u postgres psql -d "$DB" -tAc "$1"; }

AGENT="${E2E_AGENT:-$(Q "SELECT slug FROM sn_config.agents WHERE enabled AND archived_at IS NULL AND os_user IS NOT NULL AND slug NOT IN ('sysadmin','foreman') ORDER BY id LIMIT 1")}" 
OSU=$(Q "SELECT os_user FROM sn_config.agents WHERE slug='$AGENT' AND enabled AND archived_at IS NULL")
if [ -z "$AGENT" ] || [ -z "$OSU" ] || ! id "$OSU" >/dev/null 2>&1; then
    echo "[e2e] SKIP: needs a non-privileged enabled agent with an OS user" >&2
    exit 0
fi
if [ "$AGENT" = sysadmin ] || [ "$AGENT" = foreman ]; then
    echo "[e2e] REFUSED: $AGENT is a privileged secrets writer, choose another agent" >&2
    exit 2
fi
if [ ! -S "$SOCK" ]; then
    echo "[e2e] FAIL: core-api socket absent: $SOCK" >&2
    exit 1
fi

RUN="$(date +%s)-$$"
T0=$(Q "SELECT now()")
READ_REF="e2e_4658.read_$RUN"
SET_REF="e2e_4658.set_$RUN"
DELETE_REF="e2e_4658.delete_$RUN"
WRITER_REF="e2e_4658.writer_$RUN"
fail=0
chk(){ if [ "$1" = 1 ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }

root_api(){ curl -sS --unix-socket "$SOCK" -H 'Content-Type: application/json' "$@"; }
root_code(){ curl -sS -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" \
    -H 'Content-Type: application/json' "$@"; }
agent_sock_code(){ sudo -n -u "$OSU" curl -sS -o /dev/null -w '%{http_code}' \
    --unix-socket "$SOCK" -H 'Content-Type: application/json' "$@"; }
agent_tcp_code(){ sudo -n -u "$OSU" curl -sS -o /dev/null -w '%{http_code}' "$@"; }
# The secrets route names its writers by capability (SN_SECRETS_WRITER_CAPABILITIES, default sysadmin,configurator), not by
# slug: a node installed from 1.13 has john in that role, and the slug list this used to carry matched no one there.
WRITER=$(Q "SELECT slug FROM sn_config.agents WHERE lower(capability) IN ('sysadmin','configurator') AND enabled AND archived_at IS NULL AND os_user IS NOT NULL ORDER BY CASE lower(capability) WHEN 'sysadmin' THEN 0 ELSE 1 END, id LIMIT 1")
WRITER_OSU=$(Q "SELECT os_user FROM sn_config.agents WHERE slug='$WRITER' AND enabled AND archived_at IS NULL")
writer_sock_code(){ sudo -n -u "$WRITER_OSU" curl -sS -o /dev/null -w '%{http_code}' \
    --unix-socket "$SOCK" -H 'Content-Type: application/json' "$@"; }

ORIGINAL_GRANTS=$(root_api "http://localhost/api/agents/$AGENT/grants") || exit 1

cleanup(){
    root_api -X PUT "http://localhost/api/agents/$AGENT/grants" \
        -d "$ORIGINAL_GRANTS" >/dev/null 2>&1 || true
    for ref in "$READ_REF" "$SET_REF" "$DELETE_REF" "$WRITER_REF"; do
        root_api -X DELETE "http://localhost/api/secrets/$ref" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT

root_api -X POST http://localhost/api/secrets \
    -d "{\"ref\":\"$READ_REF\",\"value\":\"fixture-read-value\"}" >/dev/null || exit 1
root_api -X POST http://localhost/api/secrets \
    -d "{\"ref\":\"$DELETE_REF\",\"value\":\"fixture-delete-value\"}" >/dev/null || exit 1

echo "[e2e] secrets-boundary: agent=$AGENT os_user=$OSU run=$RUN"
echo "[e2e] assertions:"

# The vulnerable release returns 200 here because loopback without an asserted
# agent name is treated as an anonymous component.  The fixed release requires
# a component credential and records the refusal.
read_code=$(agent_tcp_code "$TCP/api/secrets/resolve?ref=$READ_REF")
[ "$read_code" = 403 ]; chk $((1-$?)) "anonymous agent loopback read is refused (got $read_code)"

set_code=$(agent_sock_code -X POST http://localhost/api/secrets \
    -d "{\"ref\":\"$SET_REF\",\"value\":\"agent-must-not-write\"}")
[ "$set_code" = 403 ]; chk $((1-$?)) "non-privileged agent socket write is refused (got $set_code)"

delete_code=$(agent_sock_code -X DELETE "http://localhost/api/secrets/$DELETE_REF")
[ "$delete_code" = 403 ]; chk $((1-$?)) "non-privileged agent socket delete is refused (got $delete_code)"

# The per-ref boundary would be cosmetic if an agent could grant itself the
# secrets tool and the matching ref through the legacy grants endpoint.
self_grant_code=$(agent_sock_code -X PUT \
    "http://localhost/api/agents/$AGENT/grants" \
    -d "{\"grants\":{\"secrets\":\"r--\"},\"secret_refs\":[\"$READ_REF\"]}")
[ "$self_grant_code" = 403 ]; chk $((1-$?)) "agent cannot grant itself secret refs (got $self_grant_code)"

# A refused delete must leave the value intact.  Read as the root socket peer;
# the response body is intentionally not printed.  curl exits zero for a 404,
# so assert the HTTP status rather than mistaking transport success for evidence.
survives_code=$(root_code "http://localhost/api/secrets/resolve?ref=$DELETE_REF")
[ "$survives_code" = 200 ]; chk $((1-$?)) "ref refused to the agent still exists (got $survives_code)"

# The write and the delete are refused by the role check, which records the
# refusal under the agent and the permission, not under the secret. The ledger
# must show both. The record of the anonymous read above is not asserted here.
denied=$(Q "SELECT count(*) FROM sn_core.events WHERE event_type='auth.permission_denied' AND actor='agent:$AGENT' AND entity_id='secrets.write' AND ts >= '$T0'")
[ "${denied:-0}" -ge 2 ]; chk $((1-$?)) "the refused write and delete are in the event ledger (found ${denied:-0})"

# The operator path is the positive half: a gate which refuses everybody is not
# a security control.  A root socket write must work and carry a real actor.
operator_json=$(root_api -X POST http://localhost/api/secrets \
    -d "{\"ref\":\"$SET_REF\",\"value\":\"operator-can-write\"}")
operator_actor=$(printf '%s' "$operator_json" | jq -r '.actor // empty')
case "$operator_actor" in anonymous|unknown-peer|'') ok=0;; *) ok=1;; esac
chk "$ok" "operator socket write succeeds with a real actor ($operator_actor)"

# Positive read half: the operator grants one exact ref. The same kernel-proved
# agent must read that ref and still be refused another one.
grant_code=$(root_code -X PUT "http://localhost/api/agents/$AGENT/grants" \
    -d "{\"grants\":{\"secrets\":\"r--\"},\"secret_refs\":[\"$READ_REF\"]}")
[ "$grant_code" = 200 ]; chk $((1-$?)) "operator can grant one exact secret ref (got $grant_code)"

scoped_code=$(agent_sock_code "http://localhost/api/secrets/resolve?ref=$READ_REF")
[ "$scoped_code" = 200 ]; chk $((1-$?)) "agent can read its explicitly granted ref (got $scoped_code)"

outside_code=$(agent_sock_code "http://localhost/api/secrets/resolve?ref=$DELETE_REF")
[ "$outside_code" = 403 ]; chk $((1-$?)) "agent remains refused outside its ref grant (got $outside_code)"

# Owner-designated secret agents retain the legitimate write path. Prove both
# set and delete over the same socket boundary used for the refused caller.
if [ -n "$WRITER" ] && [ -n "$WRITER_OSU" ] && id "$WRITER_OSU" >/dev/null 2>&1; then
    writer_set_code=$(writer_sock_code -X POST http://localhost/api/secrets \
        -d "{\"ref\":\"$WRITER_REF\",\"value\":\"approved-writer-value\"}")
    [ "$writer_set_code" = 201 ]; chk $((1-$?)) "$WRITER can store a secret (got $writer_set_code)"
    writer_delete_code=$(writer_sock_code -X DELETE "http://localhost/api/secrets/$WRITER_REF")
    [ "$writer_delete_code" = 200 ]; chk $((1-$?)) "$WRITER can delete its test secret (got $writer_delete_code)"
else
    chk 0 "stand has an agent with the sysadmin or configurator capability and an OS user"
fi

[ "$fail" = 0 ] && echo "[e2e] PASS" || echo "[e2e] FAIL"
exit "$fail"
