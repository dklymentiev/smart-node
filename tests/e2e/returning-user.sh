#!/usr/bin/env bash
# E2E: returning-user session access after reconcile applies _ENSURE_USER_SH.
#
# WHAT THIS CHECKS:
# The earlier change fix lives in admin-runner's _ENSURE_USER_SH: on every reconcile tick
# it creates ~/.claude and ~/.claude/projects for each agent user at mode 0750
# with group smart-node-agents, and re-applies that on every tick because the CLI
# recreates ~/.claude at 0700 when the directory is missing.
#
# A returning user's second turn depends on the gateway user (smart-node-gateway,
# a member of smart-node-agents) being able to traverse ~/.claude to resume the
# session. At 0700 it cannot. At 0750 it can.
#
# PAIR STRUCTURE. The negative (directory at 0700, gateway blocked) is asserted
# BEFORE the reconcile runs. The positive (0750 after reconcile, gateway can
# traverse) is asserted AFTER. Each alone proves nothing:
#   - The positive alone passes if ~/.claude happened to start at 0750.
#   - The negative alone passes on any broken setup that denies access.
# Both in sequence, on a directory the test itself set to 0700, prove the fix.
#
# Without reconcile does not run _ENSURE_USER_SH, the mode stays 0700,
# and the positive check fails.
# With reconcile applies 0750, the positive check passes.
#
# REQUIREMENTS:
#   - root (uses sudo -n as agent user and as root for reconcile)
#   - an agent-* user on the node
#   - admin-runner with a venv at $SN_ADMIN_RUNNER/venv/bin/python
#   - smart-node-gateway user in the smart-node-agents group
#
# Node-mutating (temporarily replaces ~/.claude for one agent user). Uses a
# trap to restore the directory even on failure. Exit 0 = pass, 1 = fail.

set -uo pipefail

AGENT_GROUP="${SMART_NODE_AGENT_GROUP:-smart-node-agents}"
GW_USER="${SMART_NODE_GW_USER:-smart-node-gateway}"
ADMIN_RUNNER="${SN_ADMIN_RUNNER:-/opt/smart-node/admin-runner}"

# THE RECONCILE IS AN ADMIN-RUNNER OPERATION, NOT A PYTHON MODULE.
# This file used to run `python -m admin_runner.reconcile --once`, and no such
# module exists in the canon or on any node: the companion installs as a flat
# directory whose unit runs `uvicorn app:app`. So every run failed on the call
# and three checks -- including the earlier change property this file exists to guard --
# have never actually been evaluated. What applies _ENSURE_USER_SH is the
# `ensure_agent_users` op, which is also what core-api hands the roster to
# (routes/crons.py). Nothing here is hardcoded: the endpoint comes from the
# integrations catalog and the owner identity from sn_config.users, the same
# way core-api resolves them (lib/admin_runner.py, auth.py).
PSQL_CLUSTER="${SMART_NODE_PG_CLUSTER:-}"
if [ -z "$PSQL_CLUSTER" ] && [ -r /etc/smart-node/persistence.env ]; then
    # shellcheck source=/dev/null
    . /etc/smart-node/persistence.env
    PSQL_CLUSTER="${SMART_NODE_PG_CLUSTER:-}"
fi
q() { runuser -u postgres -- psql ${PSQL_CLUSTER:+--cluster "$PSQL_CLUSTER"} \
        -d "${SMART_NODE_PG_CORE_DB:-smartnode}" -At -c "$1" 2>/dev/null; }

fail=0
chk(){ if [ "$1" = 1 ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }
skip(){ echo "  SKIP: $1"; }

IS_ROOT=0; [ "$(id -u)" = 0 ] && IS_ROOT=1

# Pick agent-developer as the test subject; fall back to the first agent-* user.
if id agent-developer >/dev/null 2>&1; then
    AGENT="agent-developer"
else
    AGENT=$(getent passwd | awk -F: '$1 ~ /^agent-/ {print $1; exit}')
fi

echo "[e2e] returning-user | check=returning-user-identity | agent=${AGENT:-none} | agent_group=$AGENT_GROUP | gw_user=$GW_USER"

# All active checks require root. Skip the whole file (not fail) when absent so
# CI on a dev machine does not produce misleading red.
if [ "$IS_ROOT" != 1 ]; then
    skip "all checks (root required -- run as root on a configured node)"
    echo; echo "[e2e] returning-user: ALL GREEN (all skipped)"; exit 0
fi

if [ -z "${AGENT:-}" ]; then
    skip "all checks (no agent-* user on this node)"
    echo; echo "[e2e] returning-user: ALL GREEN (all skipped)"; exit 0
fi

# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------
if ! getent group "$AGENT_GROUP" >/dev/null 2>&1; then
    skip "all checks (agent group '$AGENT_GROUP' absent)"
    echo; echo "[e2e] returning-user: ALL GREEN (all skipped)"; exit 0
fi

if ! id "$GW_USER" >/dev/null 2>&1; then
    skip "all checks (gateway user '$GW_USER' absent -- service identity check cannot run)"
    echo; echo "[e2e] returning-user: ALL GREEN (all skipped)"; exit 0
fi

# The reconcile is reached over admin-runner's API, so the precondition is that
# the node knows where admin-runner is -- not that a venv exists on disk.
RUNNER_URL=$(q "SELECT endpoint FROM sn_config.integrations WHERE slug = 'admin-runner' AND archived_at IS NULL")
if [ -z "$RUNNER_URL" ]; then
    skip "all checks (no admin-runner endpoint in sn_config.integrations -- cannot run the reconcile)"
    echo; echo "[e2e] returning-user: ALL GREEN (all skipped)"; exit 0
fi

# Gateway must be in the agent group, or the permission model is not set up.
if ! id -nG "$GW_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$AGENT_GROUP"; then
    chk 0 "gateway user '$GW_USER' is in group '$AGENT_GROUP' (required for the permission model)"
    echo; echo "[e2e] returning-user: FAILURES"; exit 1
fi
chk 1 "gateway user '$GW_USER' is in group '$AGENT_GROUP'"

# ---------------------------------------------------------------------------
# Locate ~/.claude for the test agent and set up a trap to restore it.
# ---------------------------------------------------------------------------
AGENT_HOME=$(getent passwd "$AGENT" | cut -d: -f6)
CLAUDE_DIR="$AGENT_HOME/.claude"

BACKUP_DIR=""
cleanup() {
    local rc=$?
    if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
        rm -rf "$CLAUDE_DIR" 2>/dev/null || true
        mv "$BACKUP_DIR" "$CLAUDE_DIR" 2>/dev/null || true
        chown -R "$AGENT:$AGENT_GROUP" "$CLAUDE_DIR" 2>/dev/null || true
    elif [ -d "$CLAUDE_DIR" ]; then
        # We created it; if it wasn't there before, leave it as the reconcile set it.
        # (Removing it would break the agent's next turn.)
        true
    fi
    exit $rc
}
trap cleanup EXIT

# Back up the real ~/.claude if it exists so we can restore it.
if [ -d "$CLAUDE_DIR" ]; then
    BACKUP_DIR=$(mktemp -d /tmp/.e2e-returning-user-backup.XXXXXXXX)
    cp -a "$CLAUDE_DIR/." "$BACKUP_DIR/" 2>/dev/null || true
    rm -rf "$CLAUDE_DIR"
fi

# ---------------------------------------------------------------------------
# SETUP: create ~/.claude at 0700 as the agent user.
# This reproduces the state immediately after the CLI creates the directory on
# a first (or any) turn. The CLI uses the process umask so the result is 0700.
# ---------------------------------------------------------------------------
if ! sudo -n -u "$AGENT" mkdir -m 0700 "$CLAUDE_DIR" 2>/dev/null; then
    chk 0 "setup: mkdir $CLAUDE_DIR as $AGENT (sudo returned non-zero)"
    echo; echo "[e2e] returning-user: FAILURES"; exit 1
fi
chk 1 "setup: created $CLAUDE_DIR as $AGENT at mode 0700 (simulates CLI first turn)"

# ---------------------------------------------------------------------------
# NEGATIVE: before reconcile, the gateway cannot traverse ~/.claude at 0700.
# A user in smart-node-agents but not the owner cannot execute a 0700 directory.
# ---------------------------------------------------------------------------
mode_before=$(stat -c %a "$CLAUDE_DIR" 2>/dev/null)
[ "$mode_before" = "700" ]
chk $((1-$?)) "pre-condition: ~/.claude is 0700 before reconcile (got: ${mode_before:-absent})"

if sudo -n -u "$GW_USER" test -x "$CLAUDE_DIR" 2>/dev/null; then
    chk 0 "gateway cannot traverse ~/.claude at 0700 (it CAN -- pre-condition wrong, test is invalid)"
    # The positive check would pass even without the fix. Stop here.
    echo; echo "[e2e] returning-user: FAILURES"; exit 1
fi
chk 1 "gateway cannot traverse ~/.claude at 0700 (negative pre-condition confirmed)"

# ---------------------------------------------------------------------------
# RECONCILE: run admin-runner --once to apply _ENSURE_USER_SH.
# ---------------------------------------------------------------------------
echo "  [running reconcile to apply _ENSURE_USER_SH] ..."
reconcile_out=""
reconcile_ok=0
OWNER=$(q "SELECT email FROM sn_config.users WHERE active = true AND role IN ('owner','super-admin') AND tenant_id IS NOT NULL ORDER BY id LIMIT 1")
AGENT_SLUG=$(q "SELECT slug FROM sn_config.agents WHERE os_user = '$AGENT' AND archived_at IS NULL LIMIT 1")
# Old installed runners use Remote-User; current ones also require the private
# core-api client proof. Root can read its EnvironmentFile on the test node.
RUNNER_SECRET=""
if [ -r /etc/smart-node/admin-runner.env ]; then
    RUNNER_SECRET=$(sed -n 's/^SN_ADMIN_CLIENT_SECRET=//p' /etc/smart-node/admin-runner.env | tail -n1)
fi
runner_reconcile() {
    # Feed the secret through curl's stdin config so it never enters argv/ps.
    {
        printf 'header = "Remote-User: %s"\n' "$OWNER"
        if [ -n "$RUNNER_SECRET" ]; then
            printf 'header = "X-SN-Service: core-api"\n'
            printf 'header = "X-SN-Service-Auth: %s"\n' "$RUNNER_SECRET"
            printf 'header = "X-SN-On-Behalf-Of: %s"\n' "$OWNER"
        fi
    } | curl -fsS -m 120 -K - -X POST -H "Content-Type: application/json" \
        -d "{\"args\": {\"agents\": [{\"slug\": \"$AGENT_SLUG\", \"os_user\": \"$AGENT\"}]}}" \
        "${RUNNER_URL%/}/operations/ensure_agent_users"
}
if [ -z "$RUNNER_URL" ] || [ -z "$OWNER" ] || [ -z "$AGENT_SLUG" ]; then
    reconcile_out="cannot address the reconcile: endpoint='$RUNNER_URL' owner='$OWNER' slug='$AGENT_SLUG'"
else
    if reconcile_out=$(runner_reconcile 2>&1); then
        reconcile_ok=1
    fi
fi
chk "$reconcile_ok" "reconcile exited 0 (admin-runner op ensure_agent_users)"
if [ "$reconcile_ok" = 0 ]; then
    echo "    reconcile output:"
    printf '%s\n' "$reconcile_out" | head -30 | sed 's/^/      /'
fi

# ---------------------------------------------------------------------------
# POSITIVE: after reconcile, ~/.claude must be 0750 and gateway can traverse.
# ---------------------------------------------------------------------------
mode_after=$(stat -c %a "$CLAUDE_DIR" 2>/dev/null)
# Accept 750 or 2750 (setgid variant).
case "${mode_after:-absent}" in
    750|2750) chk 1 "~/.claude is 0750 after reconcile (got: $mode_after -- _ENSURE_USER_SH ran)" ;;
    *)        chk 0 "~/.claude is 0750 after reconcile (got: ${mode_after:-absent} -- fix not in effect)" ;;
esac

grp_after=$(stat -c %G "$CLAUDE_DIR" 2>/dev/null)
[ "${grp_after:-none}" = "$AGENT_GROUP" ]
chk $((1-$?)) "~/.claude group is $AGENT_GROUP after reconcile (got: ${grp_after:-none})"

if sudo -n -u "$GW_USER" test -x "$CLAUDE_DIR" 2>/dev/null; then
    chk 1 "gateway can traverse ~/.claude after reconcile (returning user's second turn is unblocked)"
else
    chk 0 "gateway CANNOT traverse ~/.claude after reconcile -- permission fix not in effect"
    if [ -n "$reconcile_out" ]; then
        echo "    reconcile output (for diagnosis):"
        printf '%s\n' "$reconcile_out" | head -30 | sed 's/^/      /'
    fi
fi

# ---------------------------------------------------------------------------
# CONSISTENCY: the fix must re-apply on a second reconcile tick (idempotent).
# ---------------------------------------------------------------------------
echo "  [second reconcile tick -- idempotency check] ..."
reconcile2_ok=0
runner_reconcile >/dev/null 2>&1 && reconcile2_ok=1
mode_after2=$(stat -c %a "$CLAUDE_DIR" 2>/dev/null)
case "${mode_after2:-absent}" in
    750|2750) chk 1 "~/.claude stays 0750 on a second reconcile tick (idempotent)" ;;
    *)        chk 0 "~/.claude is not 0750 after second reconcile tick (got: ${mode_after2:-absent})" ;;
esac

# ---------------------------------------------------------------------------
# Cleanup is handled by the trap.
# ---------------------------------------------------------------------------
echo
if [ "$fail" = 0 ]; then echo "[e2e] returning-user: ALL GREEN"; else echo "[e2e] returning-user: FAILURES"; fi
exit $fail
