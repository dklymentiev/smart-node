#!/usr/bin/env bash
#
# update-on.sh -- one-command Smart Node update with pre-state capture,
# session-safe detached install, validation, and agent-turn smoke check.
#
# This script updates a node in place. See also tools/release.sh (release gates)
# and orchestrator/install-on.sh (the installer this wraps).
#
# Runs from the ORCHESTRATOR machine (your machine), SSHing to the node.
# Same model as orchestrator/install-on.sh.
#
# WHAT IT DOES:
#
#   Phase 0  pre-update record  -- current version, session count, spend, UFW
#   Phase 1  backup             -- run backup script on the node (or warn if absent)
#   Phase 2  install            -- detached local tmux session; polls for completion
#   Phase 3  validate           -- sudo validate.sh --json on the node
#   Phase 4  smoke              -- inject a test message; confirm the agent completed
#                                  a turn (cost > 0, no error) within 5 minutes
#   Phase 5  report             -- final table; exit 0 ok, exit 1 any failure
#
# Usage:
#   tools/update-on.sh --ip IP --ssh-user USER --ssh-key KEY --name NAME [OPTIONS]
#
# Required:
#   --ip IP              node IP address
#   --ssh-user USER      SSH user on the node
#   --ssh-key PATH       path to the SSH private key
#   --name NAME          short node name (used in log filenames and tmux session name)
#
# Optional:
#   --smoke-agent SLUG   agent slug for the turn check (default: john, the setup agent)
#   --skip-smoke         skip Phase 4; for headless/no-LLM-credential nodes
#   --verify-only        skip Phases 0-2; run Phases 3-5 only
#   --no-backup          skip Phase 1 explicitly; records the reason
#   --force-onboarding-reseed  replace the setup-agent grant baseline; requires
#                         --accept-grants-change with a non-empty reason
#   --accept-grants-change REASON  acknowledge intentional grant/MCP narrowing
#                         and record the reason in the final report
#   --ssh-port PORT      SSH port on the node (default: 22)
#   --notify-email-sender ADDR  real configured mailbox for system/owner
#                         alerts; forwarded to the communication installer
#   --secrets-operator-users CSV  existing service MCP OS users allowed to
#                         resolve secrets through the core-api Unix socket;
#                         forwarded to the node installer
#   --help               this usage text
#
# Release selection:
#   This command deploys the current local checkout. To select a version, check
#   out the desired release tag in this repository before running the updater.
#   `--target` is deliberately not supported: install-on consumes a source tree,
#   not a version name, and must never switch that tree during a node update.
#
# Exit codes:
#   0   all phases passed
#   1   a phase failed (first failing phase and check are named)
#   2   prerequisite missing (SSH unreachable, required arg absent)
#
# SCHEMA NOTES (verified against live migrations 2026-09-01):
#
#   Sessions:  sn_core.ai_sessions  (NOT sn_core.sessions)
#   Agent link: agent_id FK -> sn_config.agents(id) via JOIN on slug
#   Cost:      (usage->>'cost_usd')::numeric  (JSONB field, not a column)
#   Done when: status = 'completed' AND error IS NULL
#   Events:    sn_core.events  -- the one journal on the node. Migration 008a
#              consolidated the other way and its note is superseded; see the
#              header on that file.
#   Inbound API fields: recipient, content, channel_type, sender

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ------------------------------------------------------------------ defaults
IP=""
SSH_USER=""
SSH_KEY=""
NAME=""
SMOKE_AGENT="john"
SKIP_SMOKE=0
VERIFY_ONLY=0
NO_BACKUP=0
SSH_PORT=22
# force all companions to run in full install mode, bypassing the
# version-equality check that would otherwise run them in --contracts-only.
FORCE_ALL=0
NOTIFY_EMAIL_SENDER=""
SECRETS_OPERATOR_USERS=""
FORCE_ONBOARDING_RESEED=0
ACCEPT_GRANTS_CHANGE=""

# --------------------------------------------------------------- arg parsing
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)          IP="$2";          shift 2 ;;
        --ssh-user)    SSH_USER="$2";    shift 2 ;;
        --ssh-key)     SSH_KEY="$2";     shift 2 ;;
        --name)        NAME="$2";        shift 2 ;;
        --target)
            echo "[update] --target is not supported: update-on deploys the current local checkout; check out the desired release tag before running it." >&2
            exit 2
            ;;
        --smoke-agent) SMOKE_AGENT="$2"; shift 2 ;;
        --skip-smoke)  SKIP_SMOKE=1;     shift ;;
        --verify-only) VERIFY_ONLY=1;    shift ;;
        --no-backup)   NO_BACKUP=1;      shift ;;
        --ssh-port)    SSH_PORT="$2";    shift 2 ;;
        --force-all)   FORCE_ALL=1;      shift ;;
        --force-onboarding-reseed) FORCE_ONBOARDING_RESEED=1; shift ;;
        --accept-grants-change) ACCEPT_GRANTS_CHANGE="$2"; shift 2 ;;
        --notify-email-sender) NOTIFY_EMAIL_SENDER="$2"; shift 2 ;;
        --secrets-operator-users) SECRETS_OPERATOR_USERS="$2"; shift 2 ;;
        --help|-h)
            awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *) echo "unknown argument: $1 (use --help)" >&2; exit 2 ;;
    esac
done

[[ -n "$IP" ]]       || { echo "[update] --ip is required" >&2;       exit 2; }
[[ -n "$SSH_USER" ]] || { echo "[update] --ssh-user is required" >&2; exit 2; }
[[ -n "$SSH_KEY" ]]  || { echo "[update] --ssh-key is required" >&2;  exit 2; }
[[ -n "$NAME" ]]     || { echo "[update] --name is required" >&2;     exit 2; }
[[ -f "$SSH_KEY" ]]  || { echo "[update] ssh key not found: $SSH_KEY" >&2; exit 2; }
if (( FORCE_ONBOARDING_RESEED )) && [[ -z "${ACCEPT_GRANTS_CHANGE//[[:space:]]/}" ]]; then
    echo "[update] --force-onboarding-reseed requires --accept-grants-change REASON" >&2
    exit 2
fi

TS="$(date +%Y%m%d-%H%M%S)"
SESSION_NAME="sn-update-${NAME}-${TS}"
PRE_FILE="/tmp/sn-update-${NAME}-${TS}-pre.txt"
EXIT_CODE_FILE="/tmp/${SESSION_NAME}-exit"
LOG_FILE="/tmp/${SESSION_NAME}.log"
GRANTS_BEFORE_FILE="/tmp/${SESSION_NAME}-grants-before.json"
GRANTS_AFTER_FILE="/tmp/${SESSION_NAME}-grants-after.json"
GRANTS_DIFF_FILE="/tmp/${SESSION_NAME}-grants-diff.txt"
REMOTE_GRANTS_SNAPSHOT="/tmp/${SESSION_NAME}-grants-snapshot.py"
GRANTS_GUARD_READY=0

# ----------------------------------------------------------- SSH/SCP helpers
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -p "$SSH_PORT" -i "$SSH_KEY")
SCP_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -P "$SSH_PORT" -i "$SSH_KEY")

ssh_run() {
    # Run a command on the node; inherit exit code; print output.
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${IP}" "$@"
}

ssh_capture() {
    # Run a command on the node; capture stdout; suppress stderr; inherit exit code.
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${IP}" "$@" 2>/dev/null
}

# ----------------------------------------------------------------- reporting
PHASE_RESULTS=()   # each entry: "phase:status:detail"

record_ok()   { PHASE_RESULTS+=("$1:ok:$2"); }
record_fail() { PHASE_RESULTS+=("$1:FAIL:$2"); }
record_skip() { PHASE_RESULTS+=("$1:skip:$2"); }

any_failed() {
    local e; for e in "${PHASE_RESULTS[@]}"; do [[ "${e#*:}" == FAIL:* ]] && return 0; done
    return 1
}

# ================================================================== Pre-flight
echo "[update] ${NAME}: checking SSH connectivity to ${IP}:${SSH_PORT} ..."
if ! ssh_capture "true"; then
    echo "[update] cannot SSH to ${SSH_USER}@${IP}:${SSH_PORT}" >&2
    echo "[update] check --ip, --ssh-user, --ssh-key, --ssh-port" >&2
    exit 2
fi
echo "[update] connected."
echo

# ================================================================ Phase 0: Pre-update record
if (( VERIFY_ONLY )); then
    echo "[update] Phase 0: pre-update record -- SKIPPED (--verify-only)"
    record_skip "pre-update" "--verify-only"
else
    echo "[update] Phase 0: pre-update record"

    pre_version="$(ssh_capture "cat /etc/smart-node/release 2>/dev/null || echo unknown")"
    pre_a_state="$(ssh_capture "grep 'status=' /var/lib/smart-node/stages/A.state 2>/dev/null \
                                || echo 'status=unknown'")"
    pre_classes_sha="$(ssh_capture "sha256sum /var/lib/smart-node/classes.json 2>/dev/null \
                                    | cut -d' ' -f1 || echo unknown")"
    pre_ufw="$(ssh_capture "sudo ufw status verbose 2>/dev/null | grep 32323 \
                             || echo 'no-VPN-rule'")"
    # SCHEMA: sn_core.ai_sessions (verified against migration 009, 2026-09-01)
    pre_session_count="$(ssh_capture \
        "sudo -n -u postgres psql -d smartnode -Atc 'SELECT count(*) FROM sn_core.ai_sessions' 2>/dev/null \
         || echo unknown")"
    # Spend from the tenant budget row (soft aggregate, not per-session).
    pre_spend="$(ssh_capture \
        "sudo -n -u postgres psql -d smartnode -Atc \
          \"SELECT COALESCE(current_spend_usd::text,'unknown') \
              FROM sn_config.budgets WHERE scope_type='tenant' LIMIT 1\" 2>/dev/null \
         || echo unknown")"

    {
        echo "sn-update pre-state"
        echo "node:           ${NAME}"
        echo "ip:             ${IP}"
        echo "timestamp:      ${TS}"
        echo "release:        ${pre_version}"
        echo "A.state:        ${pre_a_state}"
        echo "classes.sha256: ${pre_classes_sha}"
        echo "ufw-vpn-rule:   ${pre_ufw}"
        echo "session_count:  ${pre_session_count}"
        echo "spend_usd:      ${pre_spend}"
        (( NO_BACKUP )) && echo "backup:         SKIPPED by --no-backup"
    } > "$PRE_FILE"

    echo "  pre-update: node at ${pre_version}, ${pre_session_count} sessions"
    echo "  pre-state written to ${PRE_FILE}"
    record_ok "pre-update" "node at ${pre_version}, ${pre_session_count} sessions"
fi
echo

# ================================================================ Phase 1: Backup
if (( VERIFY_ONLY )); then
    echo "[update] Phase 1: backup -- SKIPPED (--verify-only)"
    record_skip "backup" "--verify-only"
elif (( NO_BACKUP )); then
    echo "[update] Phase 1: backup -- SKIPPED (--no-backup)"
    record_skip "backup" "--no-backup"
else
    echo "[update] Phase 1: backup"
    BACKUP_SCRIPT="/usr/local/bin/sn-backup"
    if ssh_capture "test -x ${BACKUP_SCRIPT}"; then
        echo "  backup: running ${BACKUP_SCRIPT} on the node ..."
        backup_out="$(ssh_capture "sudo ${BACKUP_SCRIPT} 2>&1")" && backup_rc=0 || backup_rc=$?
        if (( backup_rc == 0 )); then
            # Try to extract a path from the output; fall back gracefully.
            backup_path="$(printf '%s' "$backup_out" | grep -oE '/[^ ]+\.tar\.gz' | tail -1 \
                           || echo 'see backup output')"
            echo "  backup: ok -- ${backup_path}"
            record_ok "backup" "written to ${backup_path}"
        else
            echo "  backup: FAIL -- backup script returned exit code ${backup_rc}"
            printf '%s\n' "$backup_out" | head -5 | sed 's/^/           /'
            record_fail "backup" "backup script exited ${backup_rc}"
        fi
    else
        # No backup script: warn explicitly, do NOT gate on its absence.
        echo "  backup: no backup script found at ${BACKUP_SCRIPT} -- SKIPPED (no gate)"
        echo "  note: install a backup script at ${BACKUP_SCRIPT} to make this a hard check."
        record_skip "backup" "no script at ${BACKUP_SCRIPT}"
    fi
fi
echo

# ================================================ Grant boundary: before install
if (( VERIFY_ONLY )); then
    echo "[update] Grants before: SKIPPED (--verify-only)"
    record_skip "grants-before" "--verify-only"
else
    echo "[update] Grants before: capture effective grants and rendered MCP state"
    if [[ ! -r "${REPO_ROOT}/tools/grants-snapshot.py" ]]; then
        echo "  grants-before: FAIL -- helper missing from release tree"
        record_fail "grants-before" "tools/grants-snapshot.py is missing"
    elif ! scp "${SCP_OPTS[@]}" "${REPO_ROOT}/tools/grants-snapshot.py" \
            "${SSH_USER}@${IP}:${REMOTE_GRANTS_SNAPSHOT}" >/dev/null 2>&1; then
        echo "  grants-before: FAIL -- could not copy snapshot helper to node"
        record_fail "grants-before" "could not copy snapshot helper"
    elif ssh_run "sudo -n python3 '${REMOTE_GRANTS_SNAPSHOT}'" \
            >"${GRANTS_BEFORE_FILE}"; then
        echo "  grants-before: captured in ${GRANTS_BEFORE_FILE}"
        record_ok "grants-before" "captured in ${GRANTS_BEFORE_FILE}"
        GRANTS_GUARD_READY=1
    else
        echo "  grants-before: FAIL -- could not capture node state; install will not start"
        record_fail "grants-before" "snapshot failed; install blocked before mutation"
    fi
fi
echo

# ================================================================ Phase 2: Install (detached)
if (( VERIFY_ONLY )); then
    echo "[update] Phase 2: install -- SKIPPED (--verify-only)"
    record_skip "install" "--verify-only"
elif (( ! GRANTS_GUARD_READY )); then
    echo "[update] Phase 2: install -- SKIPPED (no pre-update grants snapshot)"
    record_skip "install" "no pre-update grants snapshot; mutation blocked"
else
    echo "[update] Phase 2: install"

    # Build the orchestrator command (runs on this machine, SSHes to node).
    INSTALL_ARGS=(
        "${REPO_ROOT}/orchestrator/install-on.sh"
        "--provider" "existing"
        "--name" "${NAME}"
        "--ip" "${IP}"
        "--ssh-user" "${SSH_USER}"
        "--ssh-key" "${SSH_KEY}"
        "--ssh-port" "${SSH_PORT}"
        "--all"
        # The target is an existing node, by definition of "update":
        #   --force            Layer P otherwise refuses a host that already has
        #                      /var/lib/smart-node/stages.
        #   --setup-via-token  the first-login check otherwise refuses a node that
        #                      already has an owner. It satisfies the check without
        #                      touching the existing operator store.
        # Without these two the tool cannot update any real node, which is every
        # node it exists for.
        "--force"
        "--setup-via-token"
    )
    [[ "$FORCE_ALL" -eq 1 ]] && INSTALL_ARGS+=("--force-companions")
    [[ "$FORCE_ONBOARDING_RESEED" -eq 1 ]] \
        && INSTALL_ARGS+=("--force-onboarding-reseed")
    [[ -n "$NOTIFY_EMAIL_SENDER" ]] \
        && INSTALL_ARGS+=("--notify-email-sender" "$NOTIFY_EMAIL_SENDER")
    [[ -n "$SECRETS_OPERATOR_USERS" ]] \
        && INSTALL_ARGS+=("--secrets-operator-users" "$SECRETS_OPERATOR_USERS")

    # Write a small wrapper script that runs the installer, tees output, and
    # records the exit code. Using a temp file avoids quoting hell when the
    # path passes through tmux's argument handling.
    INSTALL_SCRIPT="/tmp/${SESSION_NAME}-run.sh"
    {
        echo "#!/usr/bin/env bash"
        # Print each arg individually so the path is never ambiguous.
        printf '%q ' "${INSTALL_ARGS[@]}"
        printf '2>&1 | tee %q\n' "${LOG_FILE}"
        printf 'echo ${PIPESTATUS[0]} > %q\n' "${EXIT_CODE_FILE}"
    } > "${INSTALL_SCRIPT}"
    chmod +x "${INSTALL_SCRIPT}"

    if command -v tmux >/dev/null 2>&1; then
        echo "  install: creating local tmux session '${SESSION_NAME}'"
        tmux new-session -d -s "${SESSION_NAME}" "${INSTALL_SCRIPT}" \
            || { echo "[update] could not create tmux session" >&2; exit 2; }
        echo "  install: attach with:   tmux attach -t ${SESSION_NAME}"
        echo "  install: log file:      ${LOG_FILE}"
        echo "  install: waiting for installer (no time limit) ..."

        # Poll the exit-code file, not the tmux session lifetime. The wrapper
        # script writes ${EXIT_CODE_FILE} as its last act: the file appears only
        # when the installer has truly finished. Watching the tmux session alone
        # gave a 20-minute hard cap: on a real node with more companions
        # and existing data the install outlived the cap, the wrapper declared
        # FAIL, and skipped validation and smoke -- even though the node was
        # successfully updated. Poll indefinitely; report progress each minute.
        WAIT_POLL=10
        POLL_START=$(date +%s)
        LAST_PROGRESS_MIN=-1

        while true; do
            # Exit-code file written: installer finished.
            if [[ -f "${EXIT_CODE_FILE}" ]]; then
                break
            fi
            # Session still alive: still running. Report progress once per minute.
            if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
                elapsed=$(( $(date +%s) - POLL_START ))
                elapsed_min=$(( elapsed / 60 ))
                if (( elapsed_min > LAST_PROGRESS_MIN )); then
                    LAST_PROGRESS_MIN=$elapsed_min
                    echo "  install: still running after ${elapsed_min} minute(s) ..."
                fi
                sleep "${WAIT_POLL}"
                continue
            fi
            # Session gone without exit-code file: wrapper crashed before recording exit.
            break
        done

        elapsed_total=$(( $(date +%s) - POLL_START ))
        elapsed_min_total=$(( elapsed_total / 60 ))

        if [[ -f "${EXIT_CODE_FILE}" ]]; then
            installer_exit="$(tr -d '[:space:]' < "${EXIT_CODE_FILE}")"
            if [[ "$installer_exit" == "0" ]]; then
                echo "  install: ok -- installer exited 0 in ${elapsed_min_total} minute(s)"
                record_ok "install" "installer exited 0 in ${elapsed_min_total} minute(s)"
            else
                echo "  install: FAIL -- installer exited ${installer_exit}"
                [[ -f "${LOG_FILE}" ]] && tail -10 "${LOG_FILE}" | sed 's/^/           /'
                record_fail "install" "installer exited ${installer_exit}"
            fi
        else
            echo "  install: FAIL -- installer session ended without an exit-code file"
            echo "           the installer wrapper may have crashed; check ${LOG_FILE}"
            [[ -f "${LOG_FILE}" ]] && tail -10 "${LOG_FILE}" | sed 's/^/           /'
            record_fail "install" "session ended without exit code (installer crash?)"
        fi
    else
        # No tmux: warn and run synchronously.
        echo "  install: WARNING -- tmux not found on this machine; running synchronously"
        echo "  install: a dropped session will require re-running update-on.sh"
        bash "${INSTALL_SCRIPT}" || true
        # NOT the wrapper's own status. INSTALL_SCRIPT ends with
        #     echo ${PIPESTATUS[0]} > EXIT_CODE_FILE
        # so it exits with the echo's status -- 0, always, however the installer
        # ended. Reading it here is why a run whose installer exited 3 printed
        # "install: ok -- installer exited 0" and only turned red because
        # validate independently failed. The tmux branch above already reads the
        # file; this branch has to do the same, and be equally suspicious when
        # the file is not there at all.
        if [[ -f "${EXIT_CODE_FILE}" ]]; then
            installer_exit="$(tr -d '[:space:]' < "${EXIT_CODE_FILE}")"
        else
            installer_exit="missing"
        fi
        if [[ "$installer_exit" == "0" ]]; then
            echo "  install: ok -- installer exited 0 (synchronous)"
            record_ok "install" "installer exited 0 (synchronous, no tmux)"
        elif [[ "$installer_exit" == "missing" ]]; then
            echo "  install: FAIL -- installer wrote no exit-code file"
            echo "           the wrapper may have crashed; check ${LOG_FILE}"
            [[ -f "${LOG_FILE}" ]] && tail -10 "${LOG_FILE}" | sed 's/^/           /'
            record_fail "install" "no exit code recorded (wrapper crash?)"
        else
            echo "  install: FAIL -- installer exited ${installer_exit}"
            [[ -f "${LOG_FILE}" ]] && tail -10 "${LOG_FILE}" | sed 's/^/           /'
            record_fail "install" "installer exited ${installer_exit}"
        fi
    fi
fi
echo

# ================================================ Grant boundary: after install
# This runs even when the installer failed: a failed installer may already have
# mutated grants, and that is precisely when the before/after evidence matters.
if (( VERIFY_ONLY )); then
    record_skip "grants" "--verify-only"
elif (( ! GRANTS_GUARD_READY )); then
    record_skip "grants" "no pre-update snapshot; install was blocked"
else
    echo "[update] Grants after: capture and compare"
    if ! ssh_run "sudo -n python3 '${REMOTE_GRANTS_SNAPSHOT}'" \
            >"${GRANTS_AFTER_FILE}"; then
        echo "  grants: FAIL -- could not capture post-update state"
        record_fail "grants" "post-update snapshot failed"
    elif [[ ! -r "${REPO_ROOT}/tools/grants-diff.py" ]]; then
        echo "  grants: FAIL -- diff helper missing from release tree"
        record_fail "grants" "tools/grants-diff.py is missing"
    else
        GRANTS_DIFF_ARGS=(python3 "${REPO_ROOT}/tools/grants-diff.py")
        [[ -n "${ACCEPT_GRANTS_CHANGE//[[:space:]]/}" ]] \
            && GRANTS_DIFF_ARGS+=("--accept-reason" "$ACCEPT_GRANTS_CHANGE")
        GRANTS_DIFF_ARGS+=("${GRANTS_BEFORE_FILE}" "${GRANTS_AFTER_FILE}")
        grants_diff_out="$("${GRANTS_DIFF_ARGS[@]}" 2>&1)" && grants_diff_rc=0 || grants_diff_rc=$?
        printf '%s\n' "$grants_diff_out" | tee "${GRANTS_DIFF_FILE}"
        if (( grants_diff_rc != 0 )); then
            echo "  grants: FAIL -- removal/narrowing was not accepted"
            record_fail "grants" "unaccepted removal/narrowing; diff ${GRANTS_DIFF_FILE}"
        elif grep -q '^GRANTS CHECK ACCEPTED:' <<<"$grants_diff_out"; then
            echo "  grants: accepted intentional narrowing -- ${ACCEPT_GRANTS_CHANGE}"
            record_ok "grants" "accepted: ${ACCEPT_GRANTS_CHANGE}; diff ${GRANTS_DIFF_FILE}"
        else
            echo "  grants: ok -- no capability was removed or narrowed"
            record_ok "grants" "no removal/narrowing; diff ${GRANTS_DIFF_FILE}"
        fi
    fi
fi
ssh_capture "rm -f '${REMOTE_GRANTS_SNAPSHOT}'" >/dev/null 2>&1 || true
echo

# ---- what the schema stage did to the agents, lifted into THIS report
# B02 pauses smart-node-runner and the dispatcher around the schema stage and
# writes the fact into the installer log. The table below is what an operator
# actually reads after an update, so the pause gets a row here too: an update
# that stopped the agents has to say so where the reader is looking, not only in
# a log they may never open.
if [[ -f "${LOG_FILE:-}" ]]; then
    quiesce_line="$(grep -o 'agent traffic: .*' "${LOG_FILE}" | tail -1)"
    if [[ -n "$quiesce_line" ]]; then
        echo "  ${quiesce_line}"
        record_ok "quiesce" "${quiesce_line#agent traffic: }"
    fi
fi
echo

# ================================================================ Phase 3: Validate
if any_failed; then
    echo "[update] Phase 3: validate -- SKIPPED (earlier phase failed)"
    record_skip "validate" "earlier phase failed"
else
    echo "[update] Phase 3: validate"
    # A node does not carry the validator. validate.sh lives in the release tree
    # and orchestrator/install-on.sh pushes it for its own Phase 3, so push it
    # here too rather than expecting a script the product never installs.
    validate_raw=""
    validate_fails=""
    if [[ -r "${REPO_ROOT}/validate.sh" ]]; then
        if scp "${SCP_OPTS[@]}" "${REPO_ROOT}/validate.sh"                "${SSH_USER}@${IP}:/tmp/sn-update-validate.sh" >/dev/null 2>&1; then
            # stderr to a file, not to /dev/null: validate.sh writes its
            # [VERIFY-FAIL] lines there, and they are the only human-readable
            # account of what failed. Discarding them left the operator with a
            # count, and a validation-report.md pointing at a log that never
            # received them either.
            validate_raw="$(ssh_capture "sudo bash /tmp/sn-update-validate.sh --json 2>/tmp/sn-update-validate.err")" || true
            validate_fails="$(ssh_capture "grep -F '[VERIFY-FAIL]' /tmp/sn-update-validate.err 2>/dev/null || true")"
            ssh_capture "rm -f /tmp/sn-update-validate.sh /tmp/sn-update-validate.err" >/dev/null 2>&1 || true
        else
            echo "  validate: could not copy validate.sh to the node"
        fi
    else
        echo "  validate: no validate.sh in the release tree at ${REPO_ROOT}"
    fi

    if [[ -z "$validate_raw" ]]; then
        echo "  validate: FAIL -- no output from validate.sh --json on the node"
        record_fail "validate" "no output from validate.sh"
    else
        # --json means "there is a JSON verdict in here", not "this is JSON":
        # validate.sh also prints its timestamped progress lines. Keep the last
        # line that parses as a JSON object and judge that.
        validate_raw="$(printf '%s
' "$validate_raw"             | python3 -c "
import json, sys
last = ''
for line in sys.stdin:
    line = line.strip()
    if not line.startswith('{'):
        continue
    try:
        json.loads(line)
    except ValueError:
        continue
    last = line
print(last)
" 2>/dev/null)"

        if ! python3 -c "import json,sys; json.loads(sys.argv[1])" "$validate_raw" 2>/dev/null; then
            echo "  validate: FAIL -- output is not valid JSON: ${validate_raw}"
            record_fail "validate" "non-JSON output from validate.sh"
        else
            verdict="$(python3  -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('verdict','?'))"     "$validate_raw")"
            passed="$(python3   -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('passed','?'))"      "$validate_raw")"
            total="$(python3    -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('total','?'))"       "$validate_raw")"
            failed_n="$(python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('failed',0))"       "$validate_raw")"

            if [[ "$verdict" == "PASS" ]]; then
                echo "  validate: ${passed}/${total} passed"
                record_ok "validate" "${passed}/${total} passed"
            else
                echo "  validate: FAIL -- ${failed_n} check(s) failed"
                if [[ -n "${validate_fails:-}" ]]; then
                    printf '%s\n' "$validate_fails" | head -20 | sed 's/^/           /'
                    if (( $(printf '%s\n' "$validate_fails" | wc -l) > 20 )); then
                        echo "           ... and more; the full set is in the run log"
                    fi
                else
                    echo "           (see /etc/smart-node/validation-report.md on the node)"
                fi
                record_fail "validate" "${failed_n} check(s) failed"
            fi
        fi
    fi
fi
echo

# ================================================================ Phase 4: Smoke
if (( SKIP_SMOKE )); then
    echo "[update] Phase 4: smoke -- SKIPPED (--skip-smoke)"
    record_skip "smoke" "--skip-smoke"
elif any_failed; then
    echo "[update] Phase 4: smoke -- SKIPPED (earlier phase failed)"
    record_skip "smoke" "earlier phase failed"
else
    echo "[update] Phase 4: smoke (agent: ${SMOKE_AGENT})"

    # The smoke check runs a small script on the node via stdin.
    # We pass SMOKE_AGENT as $1 so the heredoc can stay single-quoted (no
    # local expansion risk) while still receiving the runtime value.
    #
    # SCHEMA VERIFIED 2026-09-01 against live migrations:
    #   sn_core.events         -- the one event journal on the node
    #   sn_core.ai_sessions    -- one row per LLM session (not sn_core.sessions)
    #   sn_config.agents(slug) -- join target to resolve slug -> agent_id
    #   usage->>'cost_usd'     -- JSONB field (not a column)
    #   a finished turn        -- a non-empty assistant row in ai_session_messages.
#                             NOT status='completed': nothing sets it, the CLI
#                             session stays 'active' so the next turn can resume
    #   Inbound API: POST /api/messages/inbound, fields: recipient/content/channel_type/sender

    smoke_out="$(ssh_run bash -s -- "${SMOKE_AGENT}" << 'SMOKE_SCRIPT'
set -uo pipefail
SMOKE_AGENT="$1"
SMOKE_IDENTITY="${SN_ADMIN_IDENTITY:-admin}"
POLL_INTERVAL=15
POLL_MAX=20    # 20 polls * 15s = 5 minutes

CORE_SOCKET="${CORE_API_UDS:-/run/smart-node/api/core-api.sock}"
# Through sudo: the socket's directory is closed to everyone but core-api and
# the agent group, so the ssh user cannot even stat it and a plain -S test
# reported a healthy socket as missing.
if ! sudo -n test -S "$CORE_SOCKET"; then
    echo "  smoke: FAIL -- core-api Unix socket missing: $CORE_SOCKET"
    exit 1
fi
echo "  smoke: core-api through proved local socket"

# -- zero-agent guard: a seeded install with no agents provisioned cannot pass
# the smoke check -- there is nobody to reply. This is not a node defect; skip
# with reason so the update does not fail on a legitimate seeded install.
_agent_count=$(sudo -n -u postgres psql -d smartnode -Atc \
    "SELECT count(*) FROM sn_config.agents" 2>/dev/null || echo 0)
if [[ "$_agent_count" -eq 0 ]]; then
    echo "  smoke: SKIP -- no agents provisioned on this node"
    exit 0
fi

# -- baseline event id (sn_core.events: the journal every writer uses)
BASELINE=$(sudo -n -u postgres psql -d smartnode -Atc \
    "SELECT COALESCE(max(id),0) FROM sn_core.events" 2>/dev/null || echo 0)
INJECT_TS=$(date --iso-8601=seconds)
echo "  smoke: baseline event id=${BASELINE}, ts=${INJECT_TS}"

# -- inject one message; no retry on failure or timeout
# Keep the status: -f alone reports "curl failed" and throws the reason away,
# which is how a 403 came to read as an agent that would not answer.
# The body comes back on stdout with the status on its own last line. It used
# to go to a mktemp file through -o: that file belongs to the ssh user, curl
# runs as root, and the kernel refuses root a write to another user's file in
# a sticky /tmp (fs.protected_regular), so curl failed and the status read 000
# on a node that had just validated clean.
inject_out=$(sudo -n /usr/bin/curl -s --unix-socket "$CORE_SOCKET" -w '\n%{http_code}' \
    -X POST "http://core-api/api/messages/inbound" \
    -H "Content-Type: application/json" \
    -d "{\"recipient\":\"${SMOKE_AGENT}\",
         \"content\":\"Smoke test. Reply with the word READY.\",
         \"channel_type\":\"internal\",
         \"sender\":\"${SMOKE_IDENTITY}\"}"     2>/dev/null) || inject_out=$'\n000'
inject_code="${inject_out##*$'\n'}"
inject_resp="${inject_out%$'\n'*}"
if [[ "$inject_code" != "200" && "$inject_code" != "201" ]]; then
    echo "  smoke: FAIL -- inbound API returned HTTP ${inject_code}"
    echo "         response: ${inject_resp}"
    exit 1
fi

if ! python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert d.get('ok')" \
    "$inject_resp" 2>/dev/null; then
    echo "  smoke: FAIL -- inbound API did not return ok=true"
    echo "         response: ${inject_resp}"
    exit 1
fi
echo "  smoke: message injected to ${SMOKE_AGENT}"

# -- poll (read-only; one inject above, never repeated)
# SCHEMA: sn_core.ai_sessions; cost in usage->>'cost_usd' JSONB; agent via JOIN
i=0
while (( i < POLL_MAX )); do
    sleep "${POLL_INTERVAL}"
    i=$(( i + 1 ))

    result=$(sudo -n -u postgres psql -d smartnode -Atc "
        SELECT (s.usage->>'cost_usd')::numeric
        FROM   sn_core.ai_sessions s
        JOIN   sn_config.agents a ON a.id = s.agent_id
        JOIN   sn_core.ai_session_messages m ON m.session_id = s.id
        WHERE  a.slug = '${SMOKE_AGENT}'
          AND  m.role = 'assistant'
          AND  m.content IS NOT NULL AND m.content <> ''
          AND  m.created_at >= '${INJECT_TS}'::timestamptz - interval '30 seconds'
          AND  (s.usage->>'cost_usd')::numeric > 0
          AND  s.error IS NULL
        ORDER  BY m.created_at DESC
        LIMIT  1
    " 2>/dev/null || true)

    if [[ -n "$result" ]]; then
        echo "  smoke: ok -- agent ${SMOKE_AGENT} completed turn, cost \$${result}"
        exit 0
    fi
    echo "  smoke: poll ${i}/${POLL_MAX} -- no completed turn yet"
done

# timeout: surface the last error if any session was created
last_err=$(sudo -n -u postgres psql -d smartnode -Atc "
    SELECT COALESCE(s.error, 'no error column set')
    FROM   sn_core.ai_sessions s
    JOIN   sn_config.agents a ON a.id = s.agent_id
    WHERE  a.slug = '${SMOKE_AGENT}'
      AND  s.created_at >= '${INJECT_TS}'::timestamptz - interval '30 seconds'
    ORDER  BY s.last_activity_at DESC
    LIMIT  1
" 2>/dev/null || echo "query failed")

echo "  smoke: FAIL -- no completed turn for ${SMOKE_AGENT} within 5 minutes"
echo "         last session error: ${last_err}"
exit 1
SMOKE_SCRIPT
    )" && smoke_rc=0 || smoke_rc=$?

    printf '%s\n' "$smoke_out"

    if (( smoke_rc == 0 )); then
        if grep -q 'smoke: SKIP --' <<< "$smoke_out"; then
            # Zero-agent guard exited 0 with SKIP; record as skip, not ok.
            skip_reason="$(printf '%s' "$smoke_out" | grep -oP '(?<=smoke: SKIP -- ).*' | tail -1 || true)"
            record_skip "smoke" "${skip_reason:-no agents provisioned}"
        else
            # Extract cost from the "ok" line for the report.
            cost="$(printf '%s' "$smoke_out" | grep -oE 'cost \$[0-9.]+' | head -1 || echo 'cost ?')"
            record_ok "smoke" "agent ${SMOKE_AGENT} completed turn, ${cost}"
        fi
    else
        # Use the phase's own FAIL reason when it set one; fall back to timeout message.
        smoke_reason="$(printf '%s' "$smoke_out" | grep -oP '(?<=smoke: FAIL -- ).*' | tail -1 || true)"
        smoke_reason="${smoke_reason:-no completed turn for ${SMOKE_AGENT} within 5 minutes}"
        record_fail "smoke" "$smoke_reason"
    fi
fi
echo

# ================================================================ Phase 5: Report
echo "[update] Phase 5: report"
printf '  %-14s  %-6s  %s\n' "phase" "status" "detail"
printf '  %-14s  %-6s  %s\n' "-------------" "------" "------"
for entry in "${PHASE_RESULTS[@]}"; do
    phase="${entry%%:*}"
    rest="${entry#*:}"
    status="${rest%%:*}"
    detail="${rest#*:}"
    printf '  %-14s  %-6s  %s\n' "$phase" "$status" "$detail"
done
echo

if any_failed; then
    for entry in "${PHASE_RESULTS[@]}"; do
        rest="${entry#*:}"
        status="${rest%%:*}"
        if [[ "$status" == "FAIL" ]]; then
            phase="${entry%%:*}"
            detail="${rest#*:}"
            echo "[update] FAILED -- phase '${phase}': ${detail}"
            break
        fi
    done
    exit 1
fi

echo "[update] all phases passed"
exit 0
