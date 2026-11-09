#!/usr/bin/env bash
# E2E regression for the planner owner gate.
#
# Runs on a throwaway stand as root. The fixture creates its own Planner tasks,
# exercises the real core-api TCP and unix-socket boundaries, and deletes the
# tasks on exit. The caller supplies one non-owner agent whose OS account and
# planner grant already exist on the stand.
set -uo pipefail

if [ "${SN_E2E_ALLOW_PLANNER:-}" != 1 ]; then
    echo "[e2e] REFUSED: set SN_E2E_ALLOW_PLANNER=1 on a throwaway stand" >&2
    exit 2
fi
case "$(hostname -s)" in
    sn-*|stand-*) ;;
    *) echo "[e2e] REFUSED: hostname is not a stand: $(hostname -s)" >&2; exit 2 ;;
esac

AGENT="${E2E_AGENT:-}"
AGENT_OS_USER="${E2E_AGENT_OS_USER:-}"
OWNER_PROOF="${SN_PROXY_AUTH_SECRET:-}"
OPERATOR_PROOF="${SN_PLANNER_OPERATOR_TOKEN:-}"
TCP="${SN_CORE_API_URL:-http://127.0.0.1:8030}"
SOCK="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
if [ -z "$AGENT" ] || [ -z "$AGENT_OS_USER" ] || [ -z "$OWNER_PROOF" ] || [ -z "$OPERATOR_PROOF" ]; then
    echo "[e2e] REFUSED: set E2E_AGENT, E2E_AGENT_OS_USER, SN_PROXY_AUTH_SECRET and SN_PLANNER_OPERATOR_TOKEN" >&2
    exit 2
fi
if ! id "$AGENT_OS_USER" >/dev/null 2>&1; then
    echo "[e2e] REFUSED: agent OS user does not exist: $AGENT_OS_USER" >&2
    exit 2
fi
if [ ! -S "$SOCK" ]; then
    echo "[e2e] FAIL: core-api socket absent: $SOCK" >&2
    exit 1
fi

# The cap counts only an agent's unanswered proposals (queued for approval) and
# is PLANNER_AGENT_OPEN_TASK_CAP, default 10. Read it from the running
# planner instead of assuming a number: this test asserted a cap of 2 after
# the planner behavior had changed, and failed on 1.12.45 and 1.12.46 for that reason.
PLANNER_PID=$(systemctl show -p MainPID --value smart-node-planner.service 2>/dev/null)
CAP=""
if [ -n "$PLANNER_PID" ] && [ "$PLANNER_PID" != 0 ]; then
    CAP=$(tr '\0' '\n' < "/proc/$PLANNER_PID/environ" 2>/dev/null \
        | sed -n 's/^PLANNER_AGENT_OPEN_TASK_CAP=//p')
fi
CAP="${CAP:-10}"

TMP=$(mktemp -d)
chmod 0777 "$TMP"
RUN="$(date +%s)-$$"
TASK_IDS=()
fail=0
chk(){ if [ "$1" = 1 ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }

owner_code(){ curl -sS -o "$TMP/$1.json" -w '%{http_code}' "${@:2}" \
    -H 'Remote-User: admin' -H "X-SN-Proxy-Auth: $OWNER_PROOF"; }
operator_code(){ curl -sS -o "$TMP/$1.json" -w '%{http_code}' "${@:2}" \
    -H 'Remote-User: admin' -H "X-SN-Planner-Operator: $OPERATOR_PROOF"; }
asserted_code(){ sudo -n -u "$AGENT_OS_USER" curl -sS -o "$TMP/$1.json" -w '%{http_code}' "${@:2}" \
    -H 'Remote-User: admin'; }
agent_code(){ sudo -n -u "$AGENT_OS_USER" curl -sS -o "$TMP/$1.json" -w '%{http_code}' \
    --unix-socket "$SOCK" "${@:2}"; }
planner_q(){ sudo -n -u postgres psql -d planner -tAc "$1"; }
core_q(){ sudo -n -u postgres psql -d smartnode -tAc "$1"; }

cleanup(){
    local id
    for id in "${TASK_IDS[@]}"; do
        curl -sS -o /dev/null -X DELETE \
            "$TCP/api/planner/api/v1/tasks/$id" \
            -H 'Remote-User: admin' -H "X-SN-Proxy-Auth: $OWNER_PROOF" || true
    done
    rm -rf "$TMP"
}
trap cleanup EXIT

echo "[e2e] planner-owner-gate: agent=$AGENT os_user=$AGENT_OS_USER run=$RUN"
echo "[e2e] assertions:"

# A typed owner name over TCP is not an owner session, even when the process
# making the request happens to run under a real agent OS account.
code=$(asserted_code asserted-create -X POST "$TCP/api/planner/api/v1/tasks" \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"e2e-4660 asserted create $RUN\"}")
[ "$code" = 403 ]; chk $((1-$?)) "agent TCP assertion of owner cannot create a task (got $code)"

# A person named only by a header over TCP changes nothing, with the bridge
# token or without it: the node refuses the request before the token is read.
# A person writes through a signed-in session, an actor credential or the
# unix socket.
code=$(curl -sS -o "$TMP/no-token.json" -w '%{http_code}' \
    -X POST "$TCP/api/planner/api/v1/tasks" \
    -H 'Remote-User: admin' -H 'Content-Type: application/json' \
    -d "{\"title\":\"e2e-4660 missing bridge proof $RUN\"}")
[ "$code" = 403 ]; chk $((1-$?)) "operator mutation without bridge proof is refused (got $code)"

code=$(operator_code operator-create -X POST "$TCP/api/planner/api/v1/tasks" \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"e2e-4660 operator bridge $RUN\"}")
[ "$code" = 403 ]; chk $((1-$?)) "operator name with the bridge token over TCP cannot create a task (got $code)"
if [ "$code" = 201 ]; then
    tid=$(jq -r '.id // empty' "$TMP/operator-create.json")
    [ -n "$tid" ] && TASK_IDS+=("$tid")
fi

# A kernel-proved agent creates inert work: the repository, not the caller,
# forces requester=<agent>, assignee=null and status queued for approval.
code=$(agent_code agent-first -X POST http://localhost/api/planner/api/v1/tasks \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"e2e-4660 agent first $RUN\",\"assignee\":\"foreman\",\"status\":\"open\"}")
[ "$code" = 201 ]; chk $((1-$?)) "kernel-proved agent can file its first task (got $code)"
FIRST_ID=$(jq -r '.id // empty' "$TMP/agent-first.json")
if [ -n "$FIRST_ID" ]; then TASK_IDS+=("$FIRST_ID"); fi
shape=$(jq -r '[.requester,.assignee,(.state // .status),.wait_reason] | @tsv' "$TMP/agent-first.json" 2>/dev/null || true)
[ "$shape" = "$AGENT"$'\t'$'\t'"queued"$'\t'"approval" ]; chk $((1-$?)) \
    "agent task is requester=$AGENT, unassigned and queued for approval (got ${shape:-empty})"

code=$(agent_code agent-second -X POST http://localhost/api/planner/api/v1/tasks \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"e2e-4660 agent second $RUN\"}")
[ "$code" = 201 ]; chk $((1-$?)) "kernel-proved agent can file its second task (got $code)"
SECOND_ID=$(jq -r '.id // empty' "$TMP/agent-second.json")
if [ -n "$SECOND_ID" ]; then TASK_IDS+=("$SECOND_ID"); fi

if [ -n "$FIRST_ID" ]; then
    wake_before=$(core_q "SELECT count(*) FROM sn_core.events WHERE event_type='agent.wake_requested' AND payload->>'task_id'='$FIRST_ID'")
    code=$(agent_code assign-before-approval -X PATCH \
        "http://localhost/api/planner/api/v1/tasks/$FIRST_ID" \
        -H 'Content-Type: application/json' -d '{"assignee":"foreman"}')
    [ "$code" = 200 ]; chk $((1-$?)) "agent may amend its inert task without approving it (got $code)"
    sleep 1
    wake_after=$(core_q "SELECT count(*) FROM sn_core.events WHERE event_type='agent.wake_requested' AND payload->>'task_id'='$FIRST_ID'")
    [ "${wake_after:-0}" = "${wake_before:-0}" ]; chk $((1-$?)) \
        "assigning an unapproved agent task does not wake its assignee (events ${wake_before:-0}->${wake_after:-0})"
fi

pending=$(planner_q "SELECT count(*) FROM tasks WHERE requester='$AGENT' AND status='queued' AND wait_reason='approval'")
fill_ok=1
while [ "${pending:-0}" -lt "$CAP" ]; do
    code=$(agent_code agent-fill -X POST http://localhost/api/planner/api/v1/tasks \
        -H 'Content-Type: application/json' \
        -d "{\"title\":\"e2e-4752 proposal $pending $RUN\"}")
    if [ "$code" != 201 ]; then fill_ok=0; break; fi
    fid=$(jq -r '.id // empty' "$TMP/agent-fill.json")
    [ -n "$fid" ] && TASK_IDS+=("$fid")
    pending=$(planner_q "SELECT count(*) FROM tasks WHERE requester='$AGENT' AND status='queued' AND wait_reason='approval'")
done
[ "$fill_ok" = 1 ]; chk $((1-$?)) \
    "agent can hold up to the cap of $CAP unanswered proposals (reached ${pending:-0}, last code $code)"

refused_before=$(planner_q "SELECT count(*) FROM task_notifications WHERE event_type='task_creation_refused' AND notify_target='$AGENT'")
code=$(agent_code agent-over-cap -X POST http://localhost/api/planner/api/v1/tasks \
    -H 'Content-Type: application/json' \
    -d "{\"title\":\"e2e-4660 over the cap $RUN\"}")
[ "$code" = 409 ]; chk $((1-$?)) "proposal $((CAP + 1)) from the same agent is refused (got $code)"
if [ "$code" != 409 ]; then
    oid=$(jq -r '.id // empty' "$TMP/agent-over-cap.json" 2>/dev/null)
    [ -n "$oid" ] && TASK_IDS+=("$oid")
fi
refused_after=$(planner_q "SELECT count(*) FROM task_notifications WHERE event_type='task_creation_refused' AND notify_target='$AGENT'")
[ "${refused_after:-0}" -gt "${refused_before:-0}" ]; chk $((1-$?)) \
    "over-cap refusal is recorded as task_creation_refused (${refused_before:-0}->${refused_after:-0})"

if [ -n "$SECOND_ID" ]; then
    code=$(agent_code forged-approval -X POST \
        "http://localhost/api/planner/api/v1/tasks/$SECOND_ID/approve" \
        -H 'Content-Type: application/json' -d '{}')
    [ "$code" = 403 ]; chk $((1-$?)) "agent cannot approve its own task (got $code)"

    code=$(operator_code bridge-approval -X POST \
        "$TCP/api/planner/api/v1/tasks/$SECOND_ID/approve" \
        -H 'Content-Type: application/json' -d '{}')
    [ "$code" = 403 ]; chk $((1-$?)) "operator bridge cannot record an owner decision (got $code)"

    code=$(owner_code approved-at -X PATCH \
        "$TCP/api/planner/api/v1/tasks/$SECOND_ID" \
        -H 'Content-Type: application/json' \
        -d '{"approved_at":"2026-01-01T00:00:00Z"}')
    [ "$code" = 400 ]; chk $((1-$?)) "approved_at cannot be patched directly (got $code)"

    code=$(owner_code owner-approval -X POST \
        "$TCP/api/planner/api/v1/tasks/$SECOND_ID/approve" \
        -H 'Content-Type: application/json' -d "{\"assignee\":\"$AGENT\"}")
    [ "$code" = 200 ]; chk $((1-$?)) "signed-in owner session can approve (got $code)"
    approved=$(jq -r '[.status,(.approved_at != null)] | @tsv' "$TMP/owner-approval.json" 2>/dev/null || true)
    [ "$approved" = $'in_progress\ttrue' ]; chk $((1-$?)) \
        "owner approval moves the task to in_progress and stamps approved_at (got ${approved:-empty})"
fi

[ "$fail" = 0 ] && echo "[e2e] PASS" || echo "[e2e] FAIL"
exit "$fail"
