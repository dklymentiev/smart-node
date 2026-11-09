#!/usr/bin/env bash
#
# acceptance-run.sh -- run the web-chat acceptance suite on a node and write
# acceptance-evidence/<version>.json when it passes.
#
# Run this from the repository root after provisioning a CLEAN throwaway node.
# The suite runs ON the node (reads /etc/smart-node, connects to core-api via SN_CORE_API_URL).
# This script copies it there, runs it, and writes the evidence file here on the
# controller when the gate is ACCEPT.
#
# Usage:
#   tools/acceptance-run.sh --name test-node --ip <node-ip> --ssh-key ~/.ssh/id_rsa
#   tools/acceptance-run.sh --name test-node --ip <node-ip> --ssh-key ~/.ssh/id_rsa \
#     --ssh-user admin [--timeout 300]
#   tools/acceptance-run.sh --seed --name test-node --ip <node-ip> --ssh-key ~/.ssh/id_rsa
#
# --seed     Write returning-user pre-update state to the node, record IDs in
#            acceptance-evidence/returning-user-state-<name>.json on the controller.
#            Run this BEFORE the update step. The normal run reads it back after.
#
# RETURNING-USER SCENARIO:
#
#   The normal run always includes a returning-user check. If a state file from
#   --seed exists, returning-user.py verifies each record still exists and its
#   content matches. If no state file exists (e.g. a fresh install), the check
#   emits RETURNING_USER: FAIL, which is correct: a fresh install is not a valid
#   publish target for this gate.
#
#   The RETURNING_USER: PASS/FAIL line is always present in the run output, and
#   a FAIL counts as one additional FAIL in the pass/skip/fail summary.
#
# Output files:
#   acceptance-evidence/<version>.json                written when gate is ACCEPT; absent on REJECT
#   acceptance-evidence/returning-user-state-<name>.json  written by --seed
#
# Exit codes:
#   0   gate: ACCEPT (acceptance-evidence written) or --seed succeeded
#   1   gate: REJECT (FAILs or SKIPs found -- check the run output) or --seed failed
#   2   cannot run (SSH unreachable, script missing, etc.)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
NAME=""
IP=""
SSH_USER="admin"
SSH_KEY=""
TIMEOUT=300   # seconds -- web-chat tests include a 180s LLM call
MODE="run"    # run | seed

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)     NAME="$2";     shift 2 ;;
        --ip)       IP="$2";       shift 2 ;;
        --ssh-user) SSH_USER="$2"; shift 2 ;;
        --ssh-key)  SSH_KEY="$2";  shift 2 ;;
        --timeout)  TIMEOUT="$2";  shift 2 ;;
        --seed)     MODE="seed";   shift ;;
        --help|-h)
            sed -n '2,50p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "[acceptance-run] unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$NAME" ]]    || { echo "[acceptance-run] --name is required" >&2; exit 2; }
[[ -n "$IP" ]]      || { echo "[acceptance-run] --ip is required" >&2; exit 2; }
[[ -n "$SSH_KEY" ]] || { echo "[acceptance-run] --ssh-key is required" >&2; exit 2; }
[[ -r "$SSH_KEY" ]] || { echo "[acceptance-run] SSH key not readable: $SSH_KEY" >&2; exit 2; }

SCRIPT="$REPO_ROOT/tools/acceptance/web-chat.py"
[[ -f "$SCRIPT" ]] || { echo "[acceptance-run] web-chat.py not found: $SCRIPT" >&2; exit 2; }

RU_SCRIPT="$REPO_ROOT/tools/acceptance/returning-user.py"
[[ -f "$RU_SCRIPT" ]] || { echo "[acceptance-run] returning-user.py not found: $RU_SCRIPT" >&2; exit 2; }

VERSION="$(tr -d '[:space:]' < RELEASE 2>/dev/null)"
[[ -n "$VERSION" ]] || { echo "[acceptance-run] RELEASE is empty or missing" >&2; exit 2; }

COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# SN_CORE_API_URL: set by the caller, or auto-detected from the node after SSH
# is confirmed (see detection block below, after the preflight check).

# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------
SSH_OPTS=(
    -i "$SSH_KEY"
    -o "StrictHostKeyChecking=no"
    -o "UserKnownHostsFile=/dev/null"
    -o "ConnectTimeout=10"
    -o "BatchMode=yes"
)

log() { echo "[acceptance-run] $*"; }

# ---------------------------------------------------------------------------
# Preflight: can we reach the node?
# ---------------------------------------------------------------------------
log "checking SSH access to $SSH_USER@$IP ..."
if ! ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "true" >/dev/null 2>&1; then
    echo "[acceptance-run] cannot SSH to $SSH_USER@$IP" >&2
    exit 2
fi
log "SSH OK"

# ---------------------------------------------------------------------------
# Resolve core-api URL
# ---------------------------------------------------------------------------
# Prefer an explicit env override. If absent, ask the node itself: the Docker
# bridge gateway is the address core-api binds to, and docker network inspect
# gives us the right value without any hard-coded literal.
if [ -z "${SN_CORE_API_URL:-}" ]; then
    # Ask which address core-api is ACTUALLY listening on instead of naming a
    # network. On this estate core-api binds the gateway of the proxy bridge,
    # not the one named after the node, so naming a network picks the wrong
    # bridge and the suite talks to an address where nothing listens. Read the
    # listening socket; if that is unavailable, probe each bridge for /api/health.
    _gw=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" \
        "sudo -n ss -lntH 2>/dev/null | awk '\$4 ~ /:8030\$/ {split(\$4,a,\":\"); print a[1]; exit}'" \
        2>/dev/null || true)
    if [ -z "$_gw" ]; then
        _gw=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" \
            "for n in \$(docker network ls --format '{{.Name}}' 2>/dev/null); do g=\$(docker network inspect \"\$n\" --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null); [ -n \"\$g\" ] && curl -sf -o /dev/null --max-time 2 \"http://\$g:8030/api/health\" && { echo \"\$g\"; break; }; done" \
            2>/dev/null || true)
    fi
    if [ -n "$_gw" ]; then
        SN_CORE_API_URL="http://${_gw}:8030"
        log "auto-detected SN_CORE_API_URL: $SN_CORE_API_URL"
    else
        echo "[acceptance-run] SN_CORE_API_URL is not set and could not be detected from the node Docker bridge." >&2
        echo "[acceptance-run] Set it before running: export SN_CORE_API_URL=http://<host>:8030" >&2
        exit 2
    fi
fi

# ---------------------------------------------------------------------------
# Resolve brin: address and credential
# ---------------------------------------------------------------------------
# web-chat.py documents SN_BRIN_URL as REQUIRED and nothing has ever set it, so
# the agent_reachable precondition failed on every run and eight of the nine
# criteria SKIPped -- the gate could not accept even on a healthy node. Same
# method as core-api above: ask the node which address brin is listening on
# rather than naming a bridge. The key lives in a 0600 root file written by
# studio.sh; web-chat.py looks for it at a path that no companion writes.
if [ -z "${SN_BRIN_URL:-}" ]; then
    _bh=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP"         "sudo -n ss -lntH 2>/dev/null | awk '\$4 ~ /:8101\$/ {split(\$4,a,\":\"); print a[1]; exit}'"         2>/dev/null || true)
    if [ -n "$_bh" ]; then
        SN_BRIN_URL="http://${_bh}:8101"
        log "auto-detected SN_BRIN_URL: $SN_BRIN_URL"
    else
        log "WARN brin is not listening on 8101; agent criteria will SKIP"
    fi
fi
if [ -z "${BRIN_API_KEY:-}" ]; then
    BRIN_API_KEY=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP"         "sudo -n sed -n 's/^BRIN_API_KEY=//p' /etc/smart-node/studio-brin.env 2>/dev/null | head -1"         2>/dev/null || true)
    [ -n "$BRIN_API_KEY" ] && log "brin credential read from the node"
fi
# Threaded into every run below. Empty values are passed as empty, which the
# suite already treats as "no brin" and reports as a missing precondition.
BRIN_ENV="SN_BRIN_URL='${SN_BRIN_URL:-}' BRIN_API_KEY='${BRIN_API_KEY:-}'"

# ---------------------------------------------------------------------------
# Stage directory on the node
# ---------------------------------------------------------------------------
RSTAGE=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" 'mktemp -d /tmp/sn-accept.XXXXXXXX' 2>/dev/null) \
    || { echo "[acceptance-run] could not create staging dir on $IP" >&2; exit 2; }
log "staging on node: $RSTAGE"

cleanup() {
    ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "rm -rf '$RSTAGE'" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Seed mode: plant returning-user state before the update
# ---------------------------------------------------------------------------
if [[ "$MODE" == "seed" ]]; then
    log "copying returning-user.py to node ..."
    scp "${SSH_OPTS[@]}" "$RU_SCRIPT" "$SSH_USER@$IP:$RSTAGE/returning-user.py" >/dev/null

    mkdir -p "$REPO_ROOT/acceptance-evidence"
    STATE_FILE="$REPO_ROOT/acceptance-evidence/returning-user-state-${NAME}.json"

    log "running returning-user.py --seed on node ..."
    # stderr (progress) flows to the terminal; stdout (JSON state) is captured.
    SEED_JSON=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" \
        "sudo -n env SN_CORE_API_URL='$SN_CORE_API_URL' $BRIN_ENV python3 $RSTAGE/returning-user.py --seed") \
        || SEED_RC=$?
    SEED_RC=${SEED_RC:-0}

    if [[ -z "$SEED_JSON" ]]; then
        echo "[acceptance-run] seed: no JSON state produced -- seed failed" >&2
        exit 1
    fi

    printf '%s\n' "$SEED_JSON" > "$STATE_FILE"
    log "returning-user state written: acceptance-evidence/returning-user-state-${NAME}.json"
    exit "$SEED_RC"
fi

# ---------------------------------------------------------------------------
# Copy and run
# ---------------------------------------------------------------------------
log "copying web-chat.py to node ..."
scp "${SSH_OPTS[@]}" "$SCRIPT" "$SSH_USER@$IP:$RSTAGE/web-chat.py" >/dev/null

log "running acceptance suite (timeout: ${TIMEOUT}s) ..."
echo
RUN_OUTPUT=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" \
    "sudo -n env SN_CORE_API_URL='$SN_CORE_API_URL' $BRIN_ENV timeout $TIMEOUT python3 $RSTAGE/web-chat.py" 2>&1) \
    || RUN_RC=$?
RUN_RC=${RUN_RC:-0}

# Print the full output so the operator sees it.
printf '%s\n' "$RUN_OUTPUT"
echo

# ---------------------------------------------------------------------------
# Parse the summary line
# "# done | PASS | pass=9 skip=0 fail=0 | gate: ACCEPT"
# ---------------------------------------------------------------------------
SUMMARY=$(printf '%s\n' "$RUN_OUTPUT" | grep '^# done |' | tail -1)
if [[ -z "$SUMMARY" ]]; then
    echo "[acceptance-run] ERROR: no summary line in output -- the run did not complete" >&2
    exit 2
fi

VERDICT=$(printf '%s' "$SUMMARY" | grep -oE '\| (PASS|FAIL|SKIP) \|' | tr -d '| ' || true)
PASS_COUNT=$(printf '%s' "$SUMMARY" | grep -oE 'pass=[0-9]+' | grep -oE '[0-9]+' || echo 0)
SKIP_COUNT=$(printf '%s' "$SUMMARY" | grep -oE 'skip=[0-9]+' | grep -oE '[0-9]+' || echo 0)
FAIL_COUNT=$(printf '%s' "$SUMMARY" | grep -oE 'fail=[0-9]+' | grep -oE '[0-9]+' || echo 0)

# ---------------------------------------------------------------------------
# Returning-user check
# Runs after web-chat so its RETURNING_USER: PASS/FAIL line is always present
# in the output. A FAIL adds one to FAIL_COUNT before the gate is decided.
# ---------------------------------------------------------------------------
log "copying returning-user.py to node ..."
scp "${SSH_OPTS[@]}" "$RU_SCRIPT" "$SSH_USER@$IP:$RSTAGE/returning-user.py" >/dev/null

RU_FLAGS="--check"
RU_STATE_FILE="$REPO_ROOT/acceptance-evidence/returning-user-state-${NAME}.json"
if [[ -f "$RU_STATE_FILE" ]]; then
    scp "${SSH_OPTS[@]}" "$RU_STATE_FILE" "$SSH_USER@$IP:$RSTAGE/ru-state.json" >/dev/null
    RU_FLAGS="--check --state-file $RSTAGE/ru-state.json"
    log "returning-user state: acceptance-evidence/returning-user-state-${NAME}.json"
else
    log "no returning-user state file -- RETURNING_USER will FAIL (fresh install or seed not run)"
fi

log "running returning-user check ..."
echo
# 2>&1: progress messages (stderr) and the RETURNING_USER line (stdout) are both captured
# and printed together so the operator sees the full trace.
RU_OUTPUT=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" \
    "sudo -n env SN_CORE_API_URL='$SN_CORE_API_URL' $BRIN_ENV python3 $RSTAGE/returning-user.py $RU_FLAGS" 2>&1) || true
printf '%s\n' "$RU_OUTPUT"
echo

RU_LINE=$(printf '%s\n' "$RU_OUTPUT" | grep '^RETURNING_USER:' | tail -1)
if [[ -z "$RU_LINE" ]]; then
    RU_LINE="RETURNING_USER: FAIL returning-user.py produced no output"
    printf '%s\n' "$RU_LINE"
fi

RU_VERDICT="FAIL"
if [[ "$RU_LINE" == "RETURNING_USER: PASS" ]]; then
    RU_VERDICT="PASS"
else
    FAIL_COUNT=$(( FAIL_COUNT + 1 ))
fi

# Combined gate: web-chat must pass clean AND returning-user must pass.
if [[ "$VERDICT" == "PASS" && "$SKIP_COUNT" == "0" && "$FAIL_COUNT" == "0" ]]; then
    GATE="ACCEPT"
else
    GATE="REJECT"
fi

log "summary: verdict=$VERDICT pass=$PASS_COUNT skip=$SKIP_COUNT fail=$FAIL_COUNT returning_user=$RU_VERDICT gate=$GATE"

# ---------------------------------------------------------------------------
# Write evidence (only on ACCEPT)
# ---------------------------------------------------------------------------
if [[ "$GATE" == "ACCEPT" ]]; then
    mkdir -p "$REPO_ROOT/acceptance-evidence"
    EV="$REPO_ROOT/acceptance-evidence/$VERSION.json"
    cat > "$EV" <<EVEOF
{
  "version": "$VERSION",
  "commit": "$COMMIT",
  "node": "$NAME",
  "ip": "$IP",
  "passed": $PASS_COUNT,
  "skipped": $SKIP_COUNT,
  "failed": $FAIL_COUNT,
  "returning_user": "$RU_VERDICT",
  "gate": "ACCEPT",
  "at": "$(date -Is)"
}
EVEOF
    log "acceptance evidence written: acceptance-evidence/$VERSION.json"
    log "commit this file before publishing: git add acceptance-evidence/$VERSION.json"
    exit 0
else
    log "gate: REJECT -- acceptance-evidence NOT written"
    log "Fix the issues above, then re-run this script on a clean node."
    exit 1
fi
