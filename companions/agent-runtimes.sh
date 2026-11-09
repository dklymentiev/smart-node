#!/bin/bash
#
# agent-runtimes.sh -- Smart Node installer, Layer C Phase 2 companion:
# agent-runtimes.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B04 + the
# gateway companion.
#
# Installs the agent-runtimes companion: the execution layer for the
# node's ephemeral, stateless brain. The brain is spawned per job: it loads
# context, reasons, acts, writes memory, and dies.
#
# What plays that brain: Claude Code. Operator decision 2026-05-17 --
# do NOT build a custom worker runtime and do NOT touch Flint. Claude
# Code already IS a governed agent runtime: tool use, permissions,
# sandboxing, MCP. The companion installs Claude Code on the node
# exactly as it runs on a workstation -- no more. This SUPERSEDES the
# plan to build a custom worker runtime.
#
# Claude Code is a native standalone binary placed by the official
# installer (https://claude.ai/install.sh) -- NOT an npm package. The
# ~232 MB binary is too large to vendor in the installer repo, so it is
# fetched at install time (the node has egress) and pinned to a version
# for reproducibility.
#
# Creates:
#   - service account smart-node-agent (+ owned dirs) via lib.sh
#     create_service_user
#   - Claude Code installed for that account: the binary under
#     ~/.local/share/claude/ and the launcher ~/.local/bin/claude
#     (home = /var/lib/smart-node/agent)
#   - ~/.claude/settings.json -- a baseline config for the account
#   - /usr/local/bin/claude -- a system symlink to the launcher, so the
#     gateway companion's `cli` provider resolves `claude` on PATH
#   - /opt/smart-node/scratch -- the brain's scratch working directory,
#     group-writable; the gateway spawns claude here (CLI_DEFAULT_CWD)
#   - companions.d/agent-runtimes.json -- the manifest the B06 panel reads
#   - "Companion agent-runtimes" section in the install report
#   - /var/lib/smart-node/stages/agent-runtimes.state
#
# No systemd service: Claude Code is a CLI invoked per job, not a
# daemon. The triggers that spawn it (ai-workflow's secretary, brin)
# are separate companions.
#
# Auth: the companion installs the runtime; it does not invent an
# Anthropic API key. The node's Claude Code authenticates with an API
# key supplied via the companion's Configure step (B06 panel) -- the
# gateway pattern. A headless node cannot do interactive OAuth login.
#
# Scope (v0.2.0): install + baseline-configure Claude Code, and wire it
# into the gateway companion's `cli` provider -- a /usr/local/bin/claude
# symlink (the gateway resolves `claude` on PATH) and the shared scratch
# working dir. Out of scope, tracked as follow-ups: Codex CLI; wiring Claude
# Code to glob /etc/smart-node/mcp.d/*.json (blocked on the mcp.d shim
# format, the same open item as the gateway companion); routing Claude
# Code through the local gateway (needs an Anthropic-compatible
# passthrough on ai-gateway).
#
# Usage:
#   agent-runtimes.sh             full run: probe -> intent -> probe -> mark
#   agent-runtimes.sh --verify    idempotent post-check; exit 0 / N fails
#   agent-runtimes.sh --status    read the stage state file
#   agent-runtimes.sh --uninstall reverse the install
#   agent-runtimes.sh --help      this message
#
# Environment:
#   SMART_NODE_CC_VERSION  Claude Code version to pin (default 2.1.143)
#
# Supersedes the custom worker-runtime plan.
#
# Exits 0 on success.

set -euo pipefail

# --help works without lib.sh -- handle it before anything else.
case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="agent-runtimes"
STAGE_NAME="agent-runtimes"
STAGE_VERSION="0.2.0"

# Service account + its home (create_service_user's convention).
SERVICE_SHORT="agent"
SERVICE_USER="smart-node-agent"
AGENT_HOME="/var/lib/smart-node/agent"

# Claude Code. The version is pinned -- a node must not drift under the
# autoupdater. The official installer accepts `stable|latest|VERSION`.
CC_VERSION="${SMART_NODE_CC_VERSION:-2.1.143}"
CC_INSTALLER_URL="https://claude.ai/install.sh"
CC_BIN="$AGENT_HOME/.local/bin/claude"
CLAUDE_CFG_DIR="$AGENT_HOME/.claude"
CLAUDE_SETTINGS="$CLAUDE_CFG_DIR/settings.json"

# Gateway wiring. The gateway companion's `cli` provider spawns `claude`
# by bare name and uses a default cwd. A system symlink puts the launcher
# on PATH; the scratch dir is the brain's working directory.
CLAUDE_SYSLINK="/usr/local/bin/claude"
SCRATCH_DIR="/opt/smart-node/scratch"

# lib.sh is installed by B01-core; the companion has no fallback.
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

# Runtime helper (sn_runtime_register / sn_runtime_unregister) -- optional
# at this point in time so older nodes don't break.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTG_HELPER="$SCRIPT_DIR/_shared/integration.sh"
if [[ -r "$INTG_HELPER" ]]; then
    # shellcheck source=/dev/null
    source "$INTG_HELPER"
fi

# ---------- arg parse ----------
MODE="run"
case "${1:-}" in
    "")          MODE="run" ;;
    --verify)    MODE="verify" ;;
    --status)    MODE="status" ;;
    --uninstall)      MODE="uninstall" ;;
    --contracts-only) MODE="contracts-only" ;;
    *)                die "unknown arg: $1 (use --help)" ;;
esac

# ---------- helpers ----------

# as_agent CMD... -- run CMD as the service account, with its HOME set
# (the installer and the launcher resolve ~ from it).
as_agent() {
    runuser -u "$SERVICE_USER" -- env "HOME=$AGENT_HOME" "$@"
}

# ensure_curl -- the official installer is fetched with curl. Layer A
# (A2 base-packages) installs it; guard anyway.
ensure_curl() {
    command -v curl >/dev/null 2>&1 \
        || die "curl not found -- Layer A installs it; the node is misconfigured"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_curl (precondition check, no state written),
#   probe_before (log-only probe, no state written), create_service_user
#   (OS account creation, one-time), install_claude_code (binary download,
#   only when version changes).
#
# CONTRACT (run): write_settings (baseline settings.json -- idempotent,
#   leaves an existing file untouched, but restores it if deleted; the
#   settings path $CLAUDE_SETTINGS is referenced by agents on every run),
#   wire_gateway (creates $CLAUDE_SYSLINK symlink and $SCRATCH_DIR; the
#   symlink is referenced by the gateway's cli provider -- if absent the
#   gateway cannot spawn agents), register_companion (companions.d record,
#   read by install-on.sh for version comparison), sn_runtime_register
#   (sn_config row describing the runtime -- must be current), stage_mark.
run_contracts_only() {
    require_root
    log "companion agent-runtimes -- contracts only (v$STAGE_VERSION)"
    write_settings
    wire_gateway
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B04,gateway"
    if declare -F sn_runtime_register >/dev/null; then
        sn_runtime_register \
            --slug             claude-code \
            --label            "Claude Code" \
            --purpose          "Reasoning + tool use" \
            --kind             claude-code \
            --status           available \
            --install-uri      "https://claude.com/claude-code" \
            --install-path     "$CLAUDE_SYSLINK" \
            --description      "Anthropic Claude Code CLI agent runtime." \
            --companion-slug   "$STAGE_ID" || true
    fi
    stage_mark "$STAGE_ID" done \
        "version=agent-runtimes@$STAGE_VERSION"
    log "companion agent-runtimes -- contracts only done"
}

# ---------- modes ----------

# probe_before -- a re-run (the launcher already present) reinstalls
# the pinned version idempotently and rewrites the baseline config.
probe_before() {
    if [[ -x "$CC_BIN" ]]; then
        log "Claude Code already installed for $SERVICE_USER -- idempotent re-run"
    elif [[ -d "$CLAUDE_CFG_DIR" ]]; then
        log "WARN: $CLAUDE_CFG_DIR exists but no launcher -- treating as a resumed run"
    fi
}

# install_claude_code -- run the official installer as the service
# account, pinned to CC_VERSION. The installer is non-interactive; it
# places the binary under ~/.local and sets up the launcher.
install_claude_code() {
    log "installing Claude Code $CC_VERSION for $SERVICE_USER (official installer)"
    local tmp; tmp=$(mktemp)
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 "$CC_INSTALLER_URL" -o "$tmp" \
        || die "could not fetch the Claude Code installer ($CC_INSTALLER_URL)"
    chmod +x "$tmp"
    # The service account owns the install tree; the installer writes
    # into its HOME. A nologin shell is fine -- runuser runs the command
    # directly, no login shell needed.
    install -m 0755 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$tmp" \
        "$AGENT_HOME/.cc-install.sh"
    rm -f "$tmp"
    as_agent bash "$AGENT_HOME/.cc-install.sh" "$CC_VERSION" \
        || die "Claude Code installer failed"
    rm -f "$AGENT_HOME/.cc-install.sh"
    [[ -x "$CC_BIN" ]] \
        || die "installer finished but the launcher is missing ($CC_BIN)"
}

# write_settings -- a baseline ~/.claude/settings.json for the account.
# Generate-once: a re-run leaves an existing file untouched so operator
# edits survive. The directory is created by the installer; ensure it.
write_settings() {
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$CLAUDE_CFG_DIR"
    if [[ -f "$CLAUDE_SETTINGS" ]]; then
        log "settings.json present -- left as is"
        return 0
    fi
    log "writing baseline settings.json"
    local tmp; tmp=$(mktemp)
    printf '{\n  "includeCoAuthoredBy": true\n}\n' > "$tmp"
    install -m 0640 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$tmp" "$CLAUDE_SETTINGS"
    rm -f "$tmp"
    manifest_record file "$CLAUDE_SETTINGS"
}

# wire_gateway -- make the gateway companion's `cli` provider operational.
# The gateway installs before agent-runtimes and deliberately carries no
# coupling to it; agent-runtimes, the dependent, does the wiring:
#   - /usr/local/bin/claude -- a symlink to the launcher, so the gateway
#     (which spawns `claude` by bare name) resolves it on the standard
#     PATH. The binary is group-readable (smart-node), so the gateway
#     service user can exec it.
#   - the scratch dir -- the gateway spawns claude with this as cwd
#     (ai-gateway CLI_DEFAULT_CWD). Group-writable (setgid) for the
#     smart-node group so whichever account spawns the brain can use it.
# Both are idempotent and recorded in the manifest.
wire_gateway() {
    log "linking the launcher onto PATH: $CLAUDE_SYSLINK -> $CC_BIN"
    ln -sfn "$CC_BIN" "$CLAUDE_SYSLINK"
    manifest_record file "$CLAUDE_SYSLINK"
    log "creating the brain scratch dir $SCRATCH_DIR"
    install -d -m 2770 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$SCRATCH_DIR"
    manifest_record dir "$SCRATCH_DIR"
}

write_report_section() {
    local cc_reported
    cc_reported=$(as_agent "$CC_BIN" --version 2>/dev/null | head -n1 || echo "unknown")
    local body
    body=$(cat <<EOF
**Version:** agent-runtimes@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
runtime:    Claude Code  (pinned $CC_VERSION; reported: $cc_reported)
account:    $SERVICE_USER  (home $AGENT_HOME)
launcher:   $CC_BIN
on PATH:    $CLAUDE_SYSLINK  (-> launcher; the gateway cli provider)
scratch:    $SCRATCH_DIR  (brain working dir, group-writable)
config:     $CLAUDE_SETTINGS
service:    none -- Claude Code is invoked per job, not a daemon
\`\`\`

Claude Code is the node's ephemeral stateless brain. The Anthropic API
key is supplied via the Configure step in the B06 panel (a headless
node cannot do interactive OAuth). MCP wiring (\`mcp.d/\` glob) and
Codex integration remains follow-up work. The custom worker-runtime plan is superseded.
EOF
)
    report_section "Companion agent-runtimes" "$body"
}

run_intent() {
    require_root
    log "companion agent-runtimes -- start"
    require_stage B04
    require_stage gateway
    log_init "$STAGE_ID"
    ensure_curl
    probe_before
    create_service_user "$SERVICE_SHORT" >/dev/null
    install_claude_code
    write_settings
    wire_gateway
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B04,gateway"
    write_report_section
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=agent-runtimes@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=agent-runtimes@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"runtime\":\"claude-code\",\"pinned\":\"$CC_VERSION\",\"checks\":$VERIFY_TOTAL}"
    fi
    if declare -F sn_runtime_register >/dev/null; then
        sn_runtime_register \
            --slug             claude-code \
            --label            "Claude Code" \
            --purpose          "Reasoning + tool use" \
            --kind             claude-code \
            --status           available \
            --install-uri      "https://claude.com/claude-code" \
            --install-path     "$CLAUDE_SYSLINK" \
            --description      "Anthropic Claude Code CLI agent runtime." \
            --companion-slug   "$STAGE_ID" || true
    fi
    log "companion agent-runtimes -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    # 1. B04 precondition.
    verify_check "stage B04 done" require_stage B04
    # 2. gateway companion precondition.
    verify_check "companion gateway done" require_stage gateway
    # 3. service user exists.
    verify_check "service user $SERVICE_USER exists" id -u "$SERVICE_USER"
    # 4. Claude Code launcher present and executable.
    verify_check "Claude Code launcher present" test -x "$CC_BIN"
    # 5. launcher runs and reports a version.
    verify_check "claude --version runs" bash -c "
        runuser -u '$SERVICE_USER' -- env HOME='$AGENT_HOME' '$CC_BIN' --version >/dev/null 2>&1"
    # 6. baseline settings present.
    verify_check "settings.json present" test -f "$CLAUDE_SETTINGS"
    # 7. the gateway-facing symlink resolves the launcher.
    verify_check "$CLAUDE_SYSLINK resolves the launcher" bash -c "
        [[ -L '$CLAUDE_SYSLINK' \
           && \"\$(readlink -f '$CLAUDE_SYSLINK')\" == \"\$(readlink -f '$CC_BIN')\" ]]"
    # 8. claude resolves on PATH (the gateway spawns it by bare name).
    verify_check "claude resolves on PATH" bash -c "command -v claude >/dev/null"
    # 9. the brain scratch dir present and group-writable (setgid 2770).
    verify_check "scratch dir $SCRATCH_DIR present, 2770" bash -c "
        [[ -d '$SCRATCH_DIR' \
           && \$(stat -c '%a' '$SCRATCH_DIR' 2>/dev/null) == 2770 ]]"
    # 10. companion manifest written.
    verify_check "companions.d/agent-runtimes.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/agent-runtimes.json"
    # 11. install-report has the section.
    verify_check "install-report has the agent-runtimes section" \
        bash -c "grep -qFx '## Companion agent-runtimes' '$SMART_NODE_REPORT'"
    # 12. manifest records the service account.
    verify_check "manifest records the agent account" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"user\",\"value\":\"$SERVICE_USER\",' \"\$m\""
}

run_verify_mode() {
    log_init "$STAGE_ID"
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    if (( VERIFY_ERRORS > 0 )); then
        echo "[FAIL] $VERIFY_ERRORS check(s) failed" >&2
        exit "$VERIFY_ERRORS"
    fi
    echo "[OK] $VERIFY_TOTAL/$VERIFY_TOTAL checks passed"
}

run_status_mode() {
    stage_status "$STAGE_ID"
    exit $?
}

# run_uninstall_mode -- reverse the install. Removing the service
# account takes its whole home with it -- the Claude Code binary, the
# launcher and ~/.claude all live under $AGENT_HOME.
run_uninstall_mode() {
    require_root
    log_init "$STAGE_ID"
    log "companion agent-runtimes -- uninstall"
    unregister_companion "$STAGE_ID"
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        # Kill anything still running as the account, then remove it
        # together with its home (binary + launcher + ~/.claude).
        pkill -u "$SERVICE_USER" >/dev/null 2>&1 || true
        userdel --remove "$SERVICE_USER" >/dev/null 2>&1 \
            || userdel "$SERVICE_USER" >/dev/null 2>&1 || true
    fi
    rm -rf "$AGENT_HOME"
    # Gateway wiring lives outside the account home -- remove it explicitly.
    rm -f "$CLAUDE_SYSLINK"
    rm -rf "$SCRATCH_DIR"
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion agent-runtimes -- uninstalled (log dir left for inspection)"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
