#!/bin/bash
#
# B01-core.sh -- Smart Node installer, Layer C bootstrap stage B01.
#
# Runs INSIDE the target host as root. Idempotent.
#
# Creates:
#   - umbrella group `smart-node` (every service account joins it)
#   - system user `smart-node` (UID < 1000, nologin, primary group above)
#   - dirs: /var/lib/smart-node/{stages,data,tmp}, /etc/smart-node,
#           /usr/local/lib/smart-node, /var/log/smart-node,
#           /opt/smart-node (native component installs live here),
#           /etc/smart-node/{mcp.d,companions.d} (registry dirs),
#           /etc/smart-node/hooks/{available,enabled} (lifecycle hooks)
#   - /usr/local/lib/smart-node/lib.sh (from sidecar lib.sh in same dir)
#   - /usr/local/lib/smart-node/cron-runner.sh (cron job wrapper) +
#     /var/log/smart-node/cron/ (per-job run logs); cron jobs land in
#     /etc/cron.d/smart-node-* via register_cron
#   - base apt packages (jq, git, python3-venv, python3-pip,
#     build-essential, rsync, ca-certificates, cron)
#   - /etc/smart-node/install-report.md (with "Stage B01 -- core" section)
#   - /var/lib/smart-node/stages/B01.state with status=done
#
# Pre-condition: host already passed Layer A (e.g. via
# provision/provision-lxd-local.sh plus configure/configure-host.sh). Specifically: root sudo, apt working,
# locale + tz set.
#
# Usage:
#   B01-core.sh              full run: probe -> intent -> probe -> mark
#   B01-core.sh --verify     idempotent post-check; exits 0 if all checks
#                            pass, non-zero (= fail count) with
#                            [VERIFY-FAIL] lines otherwise
#   B01-core.sh --status     read /var/lib/smart-node/stages/B01.state;
#                            exit 0=done 1=failed 2=missing
#   B01-core.sh --help       this message
#
# Exits 0 on success.

set -euo pipefail

STAGE_ID="B01"
STAGE_NAME="core"
STAGE_VERSION="0.5.0"
SMART_NODE_USER="smart-node"

# Resolve script directory so we can find sidecar lib.sh on first install.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# Until lib.sh is installed, provide minimal fallbacks of log/die so we
# can fail cleanly on missing prerequisites. lib.sh also defines the
# umbrella group var SMART_NODE_GROUP; run_intent sources lib.sh (via
# install_lib_sh) before any function that needs it.
if [[ -r /usr/local/lib/smart-node/lib.sh ]]; then
    # shellcheck source=/dev/null
    source /usr/local/lib/smart-node/lib.sh
elif [[ -r "$SCRIPT_DIR/lib.sh" ]]; then
    # Fresh node: nothing installed yet. Source the sidecar lib.sh so all
    # helpers (require_root, log_init, ...) and SMART_NODE_GROUP are defined
    # before run_intent uses them. install_lib_sh still copies + records it.
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib.sh"
else
    log() { echo "[$(date '+%H:%M:%S')] $*"; }
    die() { echo "[ERROR] $*" >&2; exit 1; }
fi

# ---------- arg parse ----------
MODE="run"
case "${1:-}" in
    "")          MODE="run" ;;
    --verify)    MODE="verify" ;;
    --status)    MODE="status" ;;
    --help|-h)   awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"; exit 0 ;;
    *)           die "unknown arg: $1 (use --help)" ;;
esac

# ---------- helpers ----------
install_lib_sh() {
    install -d -m 0755 /usr/local/lib/smart-node
    local src="$SCRIPT_DIR/lib.sh"
    local dst="/usr/local/lib/smart-node/lib.sh"
    if [[ -r "$src" ]]; then
        install -m 0644 "$src" "$dst"
    elif [[ -r "$dst" ]]; then
        log "lib.sh sidecar absent; using pre-installed $dst"
    else
        die "lib.sh not found at sidecar ($src) or installed location ($dst)"
    fi
    # shellcheck source=/dev/null
    source "$dst"
    manifest_record file "$dst"

    # The permission surface travels with lib.sh: the declared list and the
    # stage that applies it. Installed here so every later install path -- the
    # full run, a single companion, the scheduled check -- finds them at a
    # fixed location instead of needing the installer tree on the node.
    local p_src p_dst
    for p_src in "$SCRIPT_DIR/permissions.sh" "$SCRIPT_DIR/permissions.list" "$SCRIPT_DIR/local-layer.sh"; do
        [[ -r "$p_src" ]] || continue
        p_dst="/usr/local/lib/smart-node/$(basename "$p_src")"
        if [[ "$p_src" == *.sh ]]; then
            install -m 0755 -o root -g root "$p_src" "$p_dst"
        else
            install -m 0644 -o root -g root "$p_src" "$p_dst"
        fi
        manifest_record file "$p_dst"
    done
}

ensure_group() {
    # SERVICE group. -r: system group (GID < 1000). create_service_user
    # makes this the primary group of every companion account.
    #
    # It holds SERVICE accounts only. An agent user may NEVER be a member --
    # every group-write in this tree is safe only because of that. Agents get
    # their own group below. See bootstrap/permissions.list.
    if ! getent group "$SMART_NODE_GROUP" >/dev/null 2>&1; then
        groupadd -r "$SMART_NODE_GROUP"
    fi
    manifest_record group "$SMART_NODE_GROUP"

    # AGENT group. agent-<slug> accounts run here, and it owns the few things
    # agents genuinely need: their model credential, the shared cron log tree,
    # the scratch area, their own runtime dirs. A service user MAY join it --
    # that grants agents nothing. The asymmetry IS the boundary.
    if ! getent group "$SMART_NODE_AGENT_GROUP" >/dev/null 2>&1; then
        groupadd -r "$SMART_NODE_AGENT_GROUP"
    fi
    manifest_record group "$SMART_NODE_AGENT_GROUP"
}

ensure_user() {
    if ! id -u "$SMART_NODE_USER" >/dev/null 2>&1; then
        # -r: system account (UID < 1000). Primary group = umbrella group
        # (ensure_group ran first). Home is /var/lib/smart-node.
        useradd -r -g "$SMART_NODE_GROUP" -d /var/lib/smart-node \
            -s /usr/sbin/nologin "$SMART_NODE_USER"
    fi
    manifest_record user "$SMART_NODE_USER"
}

ensure_dirs() {
    # /var/lib/smart-node, /var/lib/smart-node/stages, /etc/smart-node:
    # all created by Layer A (configure-host.sh) as root:root. We only
    # add the smart-node-owned subdirs, the lib path and /opt/smart-node.
    install -d -m 0750 -o "$SMART_NODE_USER" -g "$SMART_NODE_GROUP" /var/lib/smart-node/data
    install -d -m 0750 -o "$SMART_NODE_USER" -g "$SMART_NODE_GROUP" /var/lib/smart-node/tmp
    install -d -m 0755 -o root             -g root              /usr/local/lib/smart-node
    # 0751, not 0750: agents are not in the service group, but their wake-cron
    # writes into the cron/ subtree below, so they must be able to TRAVERSE
    # this directory. Execute-only for others -- pass through, no listing.
    install -d -m 0751 -o "$SMART_NODE_USER" -g "$SMART_NODE_GROUP" /var/log/smart-node
    # Cron job run logs (one file per job, written by cron-runner.sh).
    # Group = the AGENT group: agent wake-crons and service crons both append
    # here, and service users are members of it (see create_service_user).
    # 3775: setgid so files inherit the group, group-writable so every cron
    # owner can append, and STICKY so one job cannot delete another's log.
    install -d -m 3775 -o root -g "$SMART_NODE_AGENT_GROUP" "$SMART_NODE_CRON_LOG_DIR"
    # /opt/smart-node holds native component installs (Postgres data,
    # Mesh, Rein, ...). root-owned, service group, setgid so per-component
    # subdirs inherit the group. NOT group-writable: components own and write
    # their own subtrees, and nothing legitimately writes at this level.
    # It was 2775 while agents also joined the service group, which let them
    # create files here. setgid is kept; only the group-write bit goes.
    install -d -m 2755 -o root -g "$SMART_NODE_GROUP" /opt/smart-node
    # Registry dirs (cron.d/-style, additive). mcp.d/ -- MCP shims globbed
    # by agent runtimes; companions.d/ -- companion manifests read by the
    # B06 web UI. setgid so files dropped by companion installers inherit
    # the umbrella group.
    install -d -m 2750 -o root -g "$SMART_NODE_GROUP" "$SMART_NODE_MCP_DIR"
    install -d -m 2750 -o root -g "$SMART_NODE_GROUP" "$SMART_NODE_COMPANIONS_DIR"
    # Stage lifecycle hooks: available/ holds hook scripts, enabled/ holds
    # the EVENT.NAME symlinks run_hooks dispatches. Same 2750 setgid as
    # the registries above.
    install -d -m 2750 -o root -g "$SMART_NODE_GROUP" "$SMART_NODE_HOOKS_DIR/available"
    install -d -m 2750 -o root -g "$SMART_NODE_GROUP" "$SMART_NODE_HOOKS_DIR/enabled"
    # Record the dirs B01 owns. Layer A owns /var/lib/smart-node and
    # /etc/smart-node themselves, so those roots are not recorded here.
    manifest_record dir /var/lib/smart-node/data
    manifest_record dir /var/lib/smart-node/tmp
    manifest_record dir /usr/local/lib/smart-node
    manifest_record dir /var/log/smart-node
    manifest_record dir "$SMART_NODE_CRON_LOG_DIR"
    manifest_record dir /opt/smart-node
    manifest_record dir "$SMART_NODE_MCP_DIR"
    manifest_record dir "$SMART_NODE_COMPANIONS_DIR"
    manifest_record dir "$SMART_NODE_HOOKS_DIR"
    manifest_record dir "$SMART_NODE_HOOKS_DIR/available"
    manifest_record dir "$SMART_NODE_HOOKS_DIR/enabled"
}

# build-essential commented out 2026-05-27: pip --only-binary=:all: dry-run across
# all companion requirements.txt resolved 100+ deps entirely from wheels (incl.
# torch, pydantic-core, tokenizers, numba, uvloop). Saves ~5 min on B01.
# If a future companion needs gcc, it can `apt-get install -y build-essential`
# from its own install script. Re-add here if we hit it repeatedly.
PACKAGES=(jq git python3 python3-venv python3-pip rsync ca-certificates cron acl)

install_packages() {
    export DEBIAN_FRONTEND=noninteractive
    # Only run apt-get update if cache is older than 1h or empty.
    local cache_age=999999
    if [[ -f /var/cache/apt/pkgcache.bin ]]; then
        cache_age=$(( $(date +%s) - $(stat -c %Y /var/cache/apt/pkgcache.bin) ))
    fi
    if (( cache_age > 3600 )); then
        log "apt-get update (cache age ${cache_age}s)"
        apt-get update -qq
    fi
    # Find packages not yet installed to avoid noisy "0 upgraded" output.
    local missing=()
    for p in "${PACKAGES[@]}"; do
        if ! dpkg-query -W -f='${Status}\n' "$p" 2>/dev/null \
                | grep -q 'install ok installed'; then
            missing+=("$p")
        fi
    done
    if (( ${#missing[@]} > 0 )); then
        log "installing: ${missing[*]}"
        apt-get install -y -qq "${missing[@]}"
        # Record only packages this stage actually installed -- pre-existing
        # base-image packages are not ours to remove on uninstall.
        for p in "${missing[@]}"; do
            manifest_record package "$p"
        done
    else
        log "all base packages already installed"
    fi
}

install_cron_infra() {
    # Install the cron wrapper sidecar next to lib.sh, and make sure the
    # system cron daemon is running so /etc/cron.d/smart-node-* files
    # (dropped later by register_cron) actually fire.
    local src="$SCRIPT_DIR/cron-runner.sh"
    local dst="$SMART_NODE_CRON_RUNNER"
    if [[ -r "$src" ]]; then
        install -m 0755 "$src" "$dst"
    elif [[ -x "$dst" ]]; then
        log "cron-runner.sh sidecar absent; using pre-installed $dst"
    else
        die "cron-runner.sh not found at sidecar ($src) or installed location ($dst)"
    fi
    # Existing cron logs may predate the runner's evidence-log mode. Normalize
    # them during every B01 update so the permission gate can converge before
    # the next scheduled tick.
    if [[ -d /var/log/smart-node/cron ]]; then
        find /var/log/smart-node/cron -maxdepth 1 -type f -exec chmod 0644 {} + 2>/dev/null || true
    fi
    manifest_record file "$dst"
    # Enable + start the cron service (best-effort: a non-systemd host or
    # build sandbox has no service to enable -- not fatal at B01).
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable --now cron >/dev/null 2>&1 \
            || log "could not enable cron service (continuing; verify will flag it)"
    else
        log "systemctl absent; skipping cron service enable"
    fi
}

# install_sn_op -- validated sysadmin wrapper.
# Ships /usr/local/sbin/sn-op through a tmp write + atomic mv. root:root 0755,
# not setuid. Idempotent: a byte-identical file is left untouched.
#
# sn-op is the ONE binary any member of grp-role-sysadmin can invoke via sudo
# (see the /etc/sudoers.d/agent-max grant for %grp-role-sysadmin). It validates
# arguments, canonicalises paths under /work, verifies planner approvals for
# Tier B verbs, emits sn_core.events
# rows, then execs the real binary.
install_sn_op() {
    local src="$SCRIPT_DIR/sn-op"
    local dst="/usr/local/sbin/sn-op"
    if [[ ! -r "$src" ]]; then
        log "sn-op sidecar absent at $src -- skipping install"
        return 0
    fi
    install -d -m 0755 -o root -g root /usr/local/sbin
    if [[ -r "$dst" ]] && cmp -s "$src" "$dst"; then
        manifest_record file "$dst"
        return 0
    fi
    local tmp="$dst.tmp.$$"
    install -m 0755 -o root -g root "$src" "$tmp"
    # Basic sanity: script must be executable and pass `bash -n` (syntax).
    if ! bash -n "$tmp" 2>/dev/null; then
        local err
        err="$(bash -n "$tmp" 2>&1 || true)"
        rm -f "$tmp"
        die "sn-op refused by bash -n: $err"
    fi
    mv -f "$tmp" "$dst"
    manifest_record file "$dst"
    log "installed $dst (validated sysadmin wrapper)"
}

# install_agent_max_sudoers -- SN_OP grant for the sysadmin role
# group. Ships the file to /etc/sudoers.d/agent-max through a tmp write +
# `visudo -c -f` + atomic mv, so a parse error never leaves a malformed
# sudoers file in place. Idempotent: a byte-identical file is left untouched,
# a changed file is replaced atomically.
#
# The current shape is a single Cmnd_Alias SN_OP naming /usr/local/sbin/sn-op
# and one grant to %grp-role-sysadmin. The role-group subject
# is rename-safe (a single-user grant on `agent-sysadmin` went
# to a non-existent user after the rename) and second-sysadmin-safe. Older
# installations may still carry the old aliases (SN_WORK_PERM
# / SN_WORK_GROUP) which were found unsafe under fnmatch(3) matching; the
# atomic replace overwrites them. They may also still name a single-user
# subject (agent-sysadmin or agent-max); the atomic replace
# overwrites that too.
install_agent_max_sudoers() {
    local src="$SCRIPT_DIR/agent-max.sudoers"
    local dst="/etc/sudoers.d/agent-max"
    if [[ ! -r "$src" ]]; then
        log "agent-max.sudoers sidecar absent at $src -- skipping install"
        return 0
    fi
    if ! command -v visudo >/dev/null 2>&1; then
        die "visudo not found -- required to gate /etc/sudoers.d/agent-max"
    fi
    # Pre-condition: sn-op wrapper must exist or the sudoers grant references
    # a non-existent binary. install_sn_op runs before this in run_intent.
    if [[ ! -x /usr/local/sbin/sn-op ]]; then
        die "sn-op wrapper missing at /usr/local/sbin/sn-op -- refusing to install sudoers grant"
    fi
    install -d -m 0755 -o root -g root /etc/sudoers.d
    # Byte-identical? Nothing to do (keeps mtime; supports the "re-run is a
    # no-op" contract).
    if [[ -r "$dst" ]] && cmp -s "$src" "$dst"; then
        manifest_record file "$dst"
        return 0
    fi
    local tmp="$dst.tmp.$$"
    install -m 0440 -o root -g root "$src" "$tmp"
    if ! visudo -cf "$tmp" >/dev/null 2>&1; then
        local err
        err="$(visudo -cf "$tmp" 2>&1 || true)"
        rm -f "$tmp"
        die "agent-max.sudoers refused by visudo: $err"
    fi
    mv -f "$tmp" "$dst"
    manifest_record file "$dst"
    log "installed $dst (SN_OP grant replacing direct-command aliases)"
}

# remove_superseded_sysadmin_sudoers -- remove the old broad sudoers grant.
# The file /etc/sudoers.d/smart-node-sysadmin was
# written by an earlier bootstrap and contained
# `agent-sysadmin ALL=(ALL) NOPASSWD: ALL`. Left in place, its union with
# /etc/sudoers.d/agent-max resolves to the broader (ALL) grant and turns the
# narrow scope into dead code. Removal runs in the SAME B01 pass as the
# install. Idempotent:
# an already-absent file returns clean. Post-removal we re-run visudo -c on
# the full sudoers set, so a botched removal (or unrelated breakage in the
# drop-in dir) is refused rather than silently swallowed.
remove_superseded_sysadmin_sudoers() {
    local old="/etc/sudoers.d/smart-node-sysadmin"
    [[ -e "$old" ]] || return 0
    if ! command -v visudo >/dev/null 2>&1; then
        die "visudo not found -- required to gate $old removal"
    fi
    rm -f "$old"
    if ! visudo -c >/dev/null 2>&1; then
        local err
        err="$(visudo -c 2>&1 || true)"
        die "sudoers state invalid after removing $old: $err"
    fi
    log "removed $old (superseded by /etc/sudoers.d/agent-max)"
}

# remove_temporary_owner_agent_max_sudoers -- the temporary bridge grant.
#
# An operator installed /etc/sudoers.d/owner-agent-max-temporary
# containing an `ALL=(ALL) NOPASSWD: ALL` rule for the sysadmin identity so
# that Max could continue to operate while the scoped grant landed. The name
# was chosen to escape the removal step above (which targets
# smart-node-sysadmin by exact name). Once /etc/sudoers.d/agent-max is
# installed with the SN_OP alias against %grp-role-sysadmin, this bridge is
# not only redundant but strictly widening: sudo unions grants across
# drop-ins, so leaving it in place makes the scoped grant dead code.
#
# The removal runs in the SAME B01 pass as the SN_OP install. It is
# idempotent: an already-absent file returns clean. Post-removal we re-run
# visudo -c on the full sudoers set, so a
# botched removal (or unrelated breakage in the drop-in dir) is refused
# rather than silently swallowed.
remove_temporary_owner_agent_max_sudoers() {
    local old="/etc/sudoers.d/owner-agent-max-temporary"
    [[ -e "$old" ]] || return 0
    if ! command -v visudo >/dev/null 2>&1; then
        die "visudo not found -- required to gate $old removal"
    fi
    rm -f "$old"
    if ! visudo -c >/dev/null 2>&1; then
        local err
        err="$(visudo -c 2>&1 || true)"
        die "sudoers state invalid after removing $old: $err"
    fi
    log "removed $old (temporary owner-installed bridge, superseded by SN_OP scoped grant)"
}

# ensure_permissions_check_cron -- the node checks its own permission surface.
#
# The install-time pass is not enough on its own: paths get created outside any
# installer (core-api renders an agent directory when one is added through the
# panel) and a node can be edited by hand. This is the standing check that says
# so. It only REPORTS -- --verify never repairs -- because a routine check must
# not silently change a running node, and must never be the thing that takes it
# down. Drift lands in the cron log and in validate.sh's report.
ensure_permissions_check_cron() {
    local stage="/usr/local/lib/smart-node/permissions.sh"
    [[ -x "$stage" ]] || { log "permissions stage absent -- skipping its check cron"; return 0; }
    register_cron "permissions-check" "17 4 * * *" "$stage --verify" root
    log "registered daily permission-surface check (04:17)"
}

write_report_section() {
    local versions
    versions=$(
        jq --version 2>/dev/null || true
        python3 --version 2>/dev/null || true
        git --version  2>/dev/null || true
    )
    local body
    body=$(cat <<EOF
**Version:** B01-core@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
user: $SMART_NODE_USER (uid=$(id -u "$SMART_NODE_USER" 2>/dev/null), gid=$(id -g "$SMART_NODE_USER" 2>/dev/null))
group: $SMART_NODE_GROUP (umbrella, gid=$(getent group "$SMART_NODE_GROUP" | cut -d: -f3))
state: $SMART_NODE_STATE_DIR
report: $SMART_NODE_REPORT
lib: /usr/local/lib/smart-node/lib.sh ($SMART_NODE_LIB_VERSION)
opt: /opt/smart-node
manifest: $SMART_NODE_MANIFEST
log: $SMART_NODE_LOG
registries: $SMART_NODE_MCP_DIR, $SMART_NODE_COMPANIONS_DIR
cron: $SMART_NODE_CRON_RUNNER (wrapper), ${SMART_NODE_CRON_PREFIX}* (jobs), $SMART_NODE_CRON_LOG_DIR (logs)
hooks: $SMART_NODE_HOOKS_DIR/{available,enabled} (events: $SMART_NODE_HOOK_EVENTS)
packages: ${PACKAGES[*]}
$versions
\`\`\`
EOF
)
    report_section "Stage B01 -- core" "$body"
}

# ---------- modes ----------

run_intent() {
    require_root
    log "stage B01 (core) -- start"
    # Layer A must have completed first: /var/lib/smart-node/stages/A.state
    # status=done. Do the precheck via a direct file read so we fail
    # BEFORE creating the group / user / dirs (those would leave a
    # half-state if A is missing).
    if [[ ! -f /var/lib/smart-node/stages/A.state ]]; then
        die "Layer A not done (/var/lib/smart-node/stages/A.state missing). Run configure-host.sh first."
    fi
    awk -F= '$1=="status"{print $2; exit}' /var/lib/smart-node/stages/A.state \
        | grep -qx done \
        || die "Layer A state is not 'done'. Check /var/lib/smart-node/stages/A.state."
    # Install + source lib.sh first so SMART_NODE_GROUP and the helper
    # functions (report_section, stage_mark, manifest_record, ...) are
    # available below.
    install_lib_sh
    log_init "$STAGE_ID"
    ensure_group
    ensure_user
    ensure_dirs
    install_packages
    install_cron_infra
    install_sn_op
    install_agent_max_sudoers
    remove_superseded_sysadmin_sudoers
    remove_temporary_owner_agent_max_sudoers
    ensure_permissions_check_cron
    write_report_section
    # Final verify pass; record check count.
    local errors=0
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=B01-core@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=B01-core@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    log "stage B01 (core) -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

# sysadmin_sudo_scope_ok -- verify check 22. Walks every agent-* member
# of grp-role-sysadmin that is also in smart-node-agents (the umbrella that
# names the caller as an agent, not as a person) and requires that member's
# effective sudo grant to be exactly SN_OP as declared by the SHIPPED sidecar
# provision/sudoers/agent-max.sudoers. The canon is the sidecar (not the
# installed /etc/sudoers.d/agent-max), because an installed file that was
# widened would otherwise vouch for itself.
#
# The old glob grant on the role group let a member escape /work with `..`
# traversal. The wrapper closes that escape. Granting the role group also
# keeps the rule valid when an individual sysadmin account is renamed and
# when a second sysadmin is added.
#
# admin is skipped: admin's own broader grant lives elsewhere and would fail
# every comparison here.
#
# Rules:
#   - sudo absent or getent absent  -> exit 0 (build sandbox).
#   - group absent                  -> exit 0 (nothing to measure).
#   - no measurable member          -> exit 0 (nothing to measure).
#   - sidecar absent + measurable   -> fail (bootstrap invariant).
#   - alias parse failure           -> fail.
#   - any member widens the grant   -> fail, log the member and the widening.
#
# The reason for a failure goes to the stage log; verify_check itself only
# prints the check name.
sysadmin_sudo_scope_ok() {
    command -v sudo >/dev/null 2>&1 || return 0
    command -v getent >/dev/null 2>&1 || return 0
    local group_line
    group_line="$(getent group grp-role-sysadmin 2>/dev/null)" || return 0
    local raw_members member members=()
    raw_members="${group_line##*:}"
    # Iterate member list; skip admin (own broader grant) and members that
    # are not also in the smart-node-agents umbrella. A user that is not an
    # agent has no reason to be measured against SN_OP.
    IFS=',' read -r -a member_arr <<<"$raw_members"
    for member in "${member_arr[@]}"; do
        [[ -n "$member" ]] || continue
        [[ "$member" == "admin" ]] && continue
        if id -nG "$member" 2>/dev/null | tr ' ' '\n' | grep -qx smart-node-agents; then
            members+=("$member")
        fi
    done
    if (( ${#members[@]} == 0 )); then
        return 0
    fi
    local canon="$SCRIPT_DIR/agent-max.sudoers" allowed reason
    if [[ ! -r "$canon" ]]; then
        log "check 22: grp-role-sysadmin has measurable members but $canon is absent -- nothing to compare against"
        return 1
    fi
    if ! allowed="$(sudoers_alias_commands "$canon" SN_OP 2>&1)"; then
        log "check 22: $canon: $allowed"
        return 1
    fi
    local m rc=0
    for m in "${members[@]}"; do
        # sudo exits non-zero for a user with no grants; the parser reports that
        # case by name, so the exit status is not used.
        if ! reason="$(sudo -n -ll -U "$m" 2>/dev/null \
                | sudo_ll_within <(printf '%s\n' "$allowed") 2>&1)"; then
            log "check 22: $m: $reason"
            rc=1
        fi
    done
    return "$rc"
}

run_verify_checks() {
    # 1. Layer A precondition: A.state present with status=done.
    verify_check "Layer A done" bash -c '
        [[ -f /var/lib/smart-node/stages/A.state ]] && \
        awk -F= "\$1==\"status\"{print \$2; exit}" /var/lib/smart-node/stages/A.state \
            | grep -qx done'
    # 2. umbrella group exists.
    verify_check "umbrella group $SMART_NODE_GROUP exists" \
        getent group "$SMART_NODE_GROUP"
    # 3. system user exists.
    verify_check "user $SMART_NODE_USER exists" id -u "$SMART_NODE_USER"
    # 4. nologin shell.
    verify_check "user $SMART_NODE_USER has nologin shell" \
        bash -c "getent passwd '$SMART_NODE_USER' | awk -F: '{print \$7}' | grep -q nologin"
    # 5. smart-node-owned subdirs present (data, tmp, log).
    verify_check "smart-node subdirs owned by $SMART_NODE_USER" \
        bash -c "
            for d in /var/lib/smart-node/data /var/lib/smart-node/tmp /var/log/smart-node; do
                [[ \$(stat -c '%U' \"\$d\") == '$SMART_NODE_USER' ]] || exit 1
            done"
    # 6. /opt/smart-node present with the service group, and NOT group-writable
    #    (an agent could previously create files here).
    verify_check "/opt/smart-node present (group $SMART_NODE_GROUP, not group-writable)" \
        bash -c "[[ -d /opt/smart-node && \$(stat -c '%G' /opt/smart-node) == '$SMART_NODE_GROUP' && \$(stat -c '%a' /opt/smart-node) == 2755 ]]"
    # 7. lib.sh present + sourceable.
    verify_check "lib.sh installed" test -r /usr/local/lib/smart-node/lib.sh
    # 8. base packages.
    verify_check "base packages installed" bash -c '
        for p in jq git python3 python3-venv python3-pip rsync ca-certificates cron acl; do
            dpkg-query -W -f="\${Status}\n" "$p" 2>/dev/null | grep -q "install ok installed" || exit 1
        done'
    # 9. install-report present + has stage section.
    verify_check "install-report has Stage B01 section" \
        bash -c "grep -qFx '## Stage B01 -- core' '$SMART_NODE_REPORT'"
    # 10. core tools runnable.
    verify_check "jq/python3/git runnable" bash -c '
        jq --version >/dev/null && python3 --version >/dev/null && git --version >/dev/null'
    # 11. manifest records the core B01 artifacts.
    verify_check "manifest records B01 core artifacts" bash -c '
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f "$m" ]] || exit 1
        grep -qF "{\"type\":\"group\",\"value\":\"smart-node\"," "$m" &&
        grep -qF "{\"type\":\"user\",\"value\":\"smart-node\","  "$m" &&
        grep -qF "{\"type\":\"dir\",\"value\":\"/opt/smart-node\"," "$m"'
    # 12. MCP registry dir present.
    verify_check "mcp.d registry dir present" test -d "$SMART_NODE_MCP_DIR"
    # 13. companions registry dir present.
    verify_check "companions.d registry dir present" test -d "$SMART_NODE_COMPANIONS_DIR"
    # 14. cron wrapper installed + executable.
    verify_check "cron-runner.sh installed + executable" \
        test -x "$SMART_NODE_CRON_RUNNER"
    # 15. cron log dir present with the AGENT group (agent wake-crons and
    #     service crons both append here; service users join that group).
    verify_check "cron log dir present (group $SMART_NODE_AGENT_GROUP)" \
        bash -c "[[ -d '$SMART_NODE_CRON_LOG_DIR' && \$(stat -c '%G' '$SMART_NODE_CRON_LOG_DIR') == '$SMART_NODE_AGENT_GROUP' ]]"
    # 15b. the permission surface travels with the node.
    verify_check "permission stage + list installed" \
        bash -c "[[ -x /usr/local/lib/smart-node/permissions.sh && -r /usr/local/lib/smart-node/permissions.list ]]"
    # 16. cron service active (lenient: a non-systemd host passes).
    verify_check "cron service active" bash -c '
        command -v systemctl >/dev/null 2>&1 || exit 0
        systemctl is-active cron >/dev/null 2>&1'
    # 17. hooks available/ dir present.
    verify_check "hooks/available dir present" \
        test -d "$SMART_NODE_HOOKS_DIR/available"
    # 18. hooks enabled/ dir present.
    verify_check "hooks/enabled dir present" \
        test -d "$SMART_NODE_HOOKS_DIR/enabled"
    # 19. /usr/local/sbin/sn-op wrapper present, root:root 0755, not
    #     setuid, bash -n passes. Skipped on a node whose bootstrap ran before
    #     this stage version.
    verify_check "sn-op wrapper installed (root:root 0755, not setuid)" bash -c '
        f=/usr/local/sbin/sn-op
        [[ -f "$f" ]] || exit 1
        [[ $(stat -c "%a %U:%G" "$f") == "755 root:root" ]] || exit 1
        # Refuse setuid, setgid, or sticky bits on the wrapper.
        mode=$(stat -c "%A" "$f")
        [[ "$mode" != *s* ]] || exit 1
        bash -n "$f" >/dev/null 2>&1'
    # 20. /etc/sudoers.d/agent-max present, mode 0440 root:root,
    #     visudo -c passes.
    verify_check "agent-max sudoers file present + visudo -c ok" bash -c '
        f=/etc/sudoers.d/agent-max
        [[ -f "$f" ]] || exit 1
        [[ $(stat -c "%a %U:%G" "$f") == "440 root:root" ]] || exit 1
        visudo -cf "$f" >/dev/null 2>&1'
    # 21. The old broad-grant file
    #     /etc/sudoers.d/smart-node-sysadmin must not be present. Its union
    #     with the narrow agent-max file would resolve to the broader grant
    #     and defeat the whole scope of the SN_OP alias.
    verify_check "superseded smart-node-sysadmin sudoers file absent" bash -c '
        [[ ! -e /etc/sudoers.d/smart-node-sysadmin ]]'
    # 21b. The temporary bridge grant
    #     (/etc/sudoers.d/owner-agent-max-temporary) must not be present after
    #     the SN_OP scoped grant is in place. Same union hazard as check 21:
    #     leaving it defeats the scope. Remove it on the same install pass.
    verify_check "temporary owner-agent-max-temporary sudoers file absent" bash -c '
        [[ ! -e /etc/sudoers.d/owner-agent-max-temporary ]]'
    # 22. Effective sudo scope for
    #     every agent-* member of grp-role-sysadmin is exactly SN_OP.
    #     Walks the role group (not a single fixed user, which was renamed
    #     out from under the old grant) using the shipped sidecar as canon.
    #     See sysadmin_sudo_scope_ok for the exact measurement, the skip
    #     conditions, and the reason lines. A member that widens the grant
    #     is named in the stage log.
    verify_check "grp-role-sysadmin members' effective sudo = SN_OP only" \
        sysadmin_sudo_scope_ok
}

run_verify_mode() {
    if [[ ! -r /usr/local/lib/smart-node/lib.sh ]]; then
        echo "[VERIFY-FAIL] lib.sh not installed (run without --verify first)" >&2
        exit 1
    fi
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
    # stage_status defined in lib.sh; if lib not present, fallback inline.
    if declare -F stage_status >/dev/null; then
        stage_status "$STAGE_ID"
        exit $?
    fi
    local f="/var/lib/smart-node/stages/${STAGE_ID}.state"
    if [[ ! -f "$f" ]]; then
        echo "stage $STAGE_ID: missing"
        exit 2
    fi
    cat "$f"
    awk -F= '$1=="status"{print $2; exit}' "$f" | grep -qx done
}

case "$MODE" in
    run)    run_intent ;;
    verify) run_verify_mode ;;
    status) run_status_mode ;;
esac
