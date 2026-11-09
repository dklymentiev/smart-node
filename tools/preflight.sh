#!/bin/bash
#
# preflight.sh -- Smart Node install preflight checks.
#
# Run this BEFORE install-on.sh to catch environment problems while they are
# still cheap to fix. Every failing check prints an exact fix command; pass
# --fix to auto-apply the safe, idempotent ones (currently: the lxdbr0
# DOCKER-USER firewall rules, and optionally `snap install lxd`).
#
# It changes nothing unless --fix is given, and never applies anything
# destructive. Exit 0 if every check passes, non-zero if any check FAILs.
#
# Usage:
#   preflight.sh --provider lxd-local [--fix]
#   preflight.sh --provider existing --ip ADDR --ssh-user USER --ssh-key /path/pub [--fix]
#
# Required:
#   --provider        lxd-local | existing
#                     lxd-local = checks THIS host, which will run the LXC
#                     existing  = previews the checks provision-existing.sh
#                                 enforces against a remote target
#
# For --provider existing:
#   --ip ADDR         target IP / host to reach over SSH
#   --ssh-user USER   SSH user on the target (default: admin)
#   --ssh-key PATH    path to the PUBLIC SSH key (private = same path minus .pub)
#
# Optional:
#   --fix             apply the safe, idempotent fixes, then re-check
#   --help            this message
#
# What it checks:
#   common (controller): ssh, jq, docker
#   lxd-local (this host): lxc, LXD storage pool 'default', LXD network
#     'lxdbr0', the Docker FORWARD-policy trap (see below)
#   existing (remote target): SSH reachable, passwordless sudo, systemd, apt,
#     RAM >= 2048 MB, disk >= 10 GB free on /
#
# The Docker FORWARD trap: on any Docker host, iptables FORWARD policy is DROP.
# That silently blocks lxdbr0 container traffic to the internet, so the in-LXC
# Docker install later hangs with no obvious cause. The fix inserts ACCEPT
# rules for lxdbr0 into DOCKER-USER. It is runtime-only -- it does NOT persist
# across a reboot unless the operator saves the ruleset (e.g. iptables-save).

set -uo pipefail

SCRIPT_VERSION="0.1.0"

# -------- defaults --------
PROVIDER=""
IP=""
SSH_USER="admin"
SSH_KEY=""
FIX=0

# No build headroom check: doci and doci-mcp were the last images built on the
# controller, and documents.sh installs both natively on the node now.
# remote target minimums (mirror provision-existing.sh)
MIN_MEM_MB=2048
MIN_DISK_GB=10

FAILS=0

# -------- helpers --------
die() { echo "[ERROR] $*" >&2; exit 4; }
log() { echo "[$(date '+%H:%M:%S')] [preflight] $*"; }
usage() { awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"; }

ok()    { printf '  [ok]    %s\n' "$1"; }
fixed() { printf '  [fixed] %s\n' "$1"; }
warn()  { printf '  [warn]  %s\n' "$1"; }
fail()  { printf '  [FAIL]  %s\n       -> %s\n' "$1" "$2"; FAILS=$((FAILS+1)); }

# iptables wrapper: run directly as root, else via non-interactive sudo so a
# missing check never blocks on a password prompt.
ipt() {
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then iptables "$@"; else sudo -n iptables "$@"; fi
}

# -------- arg parse --------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)  PROVIDER="${2:-}"; shift 2 ;;
        --ip)        IP="${2:-}"; shift 2 ;;
        --ssh-user)  SSH_USER="${2:-}"; shift 2 ;;
        --ssh-key)   SSH_KEY="${2:-}"; shift 2 ;;
        --fix)       FIX=1; shift ;;
        --help|-h)   usage; exit 0 ;;
        *)           die "Unknown arg: $1 (use --help)" ;;
    esac
done

[[ -n "$PROVIDER" ]] || die "--provider is required (lxd-local | existing)"
case "$PROVIDER" in
    lxd-local|existing) ;;
    *) die "Unknown --provider '$PROVIDER' (expected lxd-local | existing)" ;;
esac

log "Smart Node preflight (provider=$PROVIDER, fix=$FIX, version=$SCRIPT_VERSION)"
[[ "$FIX" -eq 1 ]] && log "--fix: safe idempotent fixes will be applied"

# ============================================================
# Common controller-side checks
# ============================================================
check_cmd() {
    # check_cmd NAME BIN FIX_CMD
    local name="$1" bin="$2" fix="$3"
    if command -v "$bin" >/dev/null 2>&1; then
        ok "$name ($(command -v "$bin"))"
    else
        fail "$name" "$fix"
    fi
}

echo
echo "Controller (this machine):"
check_cmd "ssh client"    ssh    "sudo apt-get install -y openssh-client"
check_cmd "jq"            jq     "sudo apt-get install -y jq"
check_cmd "docker (to build/load images)" docker "install Docker: https://docs.docker.com/engine/install/"

# ============================================================
# Provider: lxd-local -- checks about THIS host
# ============================================================
preflight_lxd_local() {
    echo
    echo "LXD host (lxd-local):"

    # lxc CLI -- fixable with snap
    if command -v lxc >/dev/null 2>&1; then
        ok "lxc CLI ($(command -v lxc))"
    elif [[ "$FIX" -eq 1 ]]; then
        log "installing LXD via snap..."
        if sudo snap install lxd >/dev/null 2>&1 && command -v lxc >/dev/null 2>&1; then
            fixed "lxc CLI (snap install lxd)"
        else
            fail "lxc CLI not found" "sudo snap install lxd"
        fi
    else
        fail "lxc CLI not found" "sudo snap install lxd"
    fi

    # Only probe LXD objects if lxc exists.
    if command -v lxc >/dev/null 2>&1; then
        # Check LXD socket accessibility before probing individual objects.
        # If the socket exists but is unreadable, lxc commands fail with
        # "permission denied" and every pool/network check would report missing
        # infrastructure that actually exists -- misleading and dangerous on a
        # live host where "sudo lxd init" is the last thing you want to do.
        local lxc_probe_err
        lxc_probe_err=$(lxc storage list 2>&1 >/dev/null)
        if echo "$lxc_probe_err" | grep -qi "permission denied\|not accessible"; then
            fail "LXD socket not accessible (permission denied)" \
                 "run preflight as root, or add this user to the lxd group: sudo usermod -aG lxd $USER  -- then log out and back in"
        else
            if lxc storage info default >/dev/null 2>&1; then
                ok "LXD storage pool 'default'"
            else
                fail "LXD storage pool 'default' missing" "sudo lxd init   (or: lxc storage create default dir)"
            fi

            if lxc network info lxdbr0 >/dev/null 2>&1; then
                ok "LXD network 'lxdbr0'"
            else
                fail "LXD network 'lxdbr0' missing" "sudo lxd init   (or: lxc network create lxdbr0)"
            fi
        fi
    fi

    # ---- The Docker FORWARD trap ----
    if ! command -v docker >/dev/null 2>&1; then
        ok "Docker FORWARD policy (no Docker on host -- n/a)"
    else
        local pol
        pol=$(ipt -L FORWARD -n 2>/dev/null | head -1)
        if [[ -z "$pol" ]]; then
            warn "cannot read iptables FORWARD chain (need root) -- re-run with sudo to verify the Docker FORWARD trap"
        elif [[ "$pol" != *"policy DROP"* ]]; then
            ok "Docker FORWARD policy (not DROP -- lxdbr0 traffic allowed)"
        elif ipt -C DOCKER-USER -i lxdbr0 -j ACCEPT >/dev/null 2>&1; then
            ok "Docker FORWARD policy DROP, but DOCKER-USER accepts lxdbr0"
        elif [[ "$FIX" -eq 1 ]]; then
            log "applying DOCKER-USER lxdbr0 ACCEPT rules..."
            ipt -C DOCKER-USER -i lxdbr0 -j ACCEPT >/dev/null 2>&1 || ipt -I DOCKER-USER 1 -i lxdbr0 -j ACCEPT >/dev/null 2>&1
            ipt -C DOCKER-USER -o lxdbr0 -j ACCEPT >/dev/null 2>&1 || ipt -I DOCKER-USER 1 -o lxdbr0 -j ACCEPT >/dev/null 2>&1
            if ipt -C DOCKER-USER -i lxdbr0 -j ACCEPT >/dev/null 2>&1; then
                fixed "Docker FORWARD trap (DOCKER-USER lxdbr0 ACCEPT) -- runtime-only, save the ruleset to persist across reboot"
            else
                fail "Docker FORWARD policy DROP blocks lxdbr0 (fix did not apply -- need root)" \
                     "sudo iptables -I DOCKER-USER 1 -i lxdbr0 -j ACCEPT; sudo iptables -I DOCKER-USER 1 -o lxdbr0 -j ACCEPT"
            fi
        else
            fail "Docker FORWARD policy DROP silently blocks lxdbr0 internet traffic (in-LXC Docker install will hang)" \
                 "sudo iptables -I DOCKER-USER 1 -i lxdbr0 -j ACCEPT; sudo iptables -I DOCKER-USER 1 -o lxdbr0 -j ACCEPT   # runtime-only; save ruleset to persist"
        fi
    fi
}

# ============================================================
# Provider: existing -- checks about the REMOTE target
# ============================================================
preflight_existing() {
    echo
    echo "Remote target (existing):"

    [[ -n "$IP" ]] || die "--ip is required for --provider existing"
    [[ -n "$SSH_KEY" ]] || die "--ssh-key is required for --provider existing"

    if [[ -r "$SSH_KEY" ]]; then
        if grep -qE '^(ssh-rsa|ssh-ed25519|ecdsa-sha2|ssh-dss)' "$SSH_KEY" 2>/dev/null; then
            ok "SSH public key readable ($SSH_KEY)"
        else
            fail "$SSH_KEY does not look like an SSH public key" "point --ssh-key at the .pub file (e.g. ~/.ssh/id_ed25519.pub)"
        fi
    else
        fail "SSH public key not readable: $SSH_KEY" "check the path passed to --ssh-key"
    fi

    local key_priv="${SSH_KEY%.pub}"
    if [[ -r "$key_priv" ]]; then
        ok "SSH private key readable ($key_priv)"
    else
        fail "SSH private key not readable at $key_priv" "place the private key alongside $SSH_KEY (same path minus .pub)"
    fi

    local target="$SSH_USER@$IP"
    local ssh_opts=(-i "$key_priv" -o BatchMode=yes -o StrictHostKeyChecking=accept-new
                    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
    remote() { ssh "${ssh_opts[@]}" "$target" "$@"; }

    if [[ ! -r "$key_priv" ]]; then
        warn "skipping remote checks -- private key $key_priv not readable"
        return
    fi

    if remote true >/dev/null 2>&1; then
        ok "SSH reachable ($target)"
    else
        fail "cannot SSH to $target with $key_priv" \
             "check the host is up, the user exists, and $SSH_KEY is in its authorized_keys"
        warn "skipping remaining remote checks -- target unreachable"
        return
    fi

    if remote "sudo -n true" >/dev/null 2>&1; then
        ok "passwordless sudo ($SSH_USER)"
    else
        fail "$SSH_USER on $IP lacks passwordless sudo" \
             "on the target: echo '$SSH_USER ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/90-$SSH_USER && sudo chmod 440 /etc/sudoers.d/90-$SSH_USER"
    fi

    if remote "command -v systemctl" >/dev/null 2>&1; then
        ok "systemd present on target"
    else
        fail "target has no systemctl" "use a systemd-based distro (Layer A and the B-stages require systemd)"
    fi

    if remote "command -v apt-get" >/dev/null 2>&1; then
        ok "apt present on target"
    else
        fail "target has no apt-get" "only Debian/Ubuntu targets are supported"
    fi

    local mem_mb disk_gb
    mem_mb=$(remote "awk '/MemTotal/ {print int(\$2/1024)}' /proc/meminfo" 2>/dev/null || echo 0)
    [[ "$mem_mb" =~ ^[0-9]+$ ]] || mem_mb=0
    if (( mem_mb >= MIN_MEM_MB )); then
        ok "target RAM (${mem_mb} MB >= ${MIN_MEM_MB} MB)"
    else
        fail "target has ${mem_mb} MB RAM, need >= ${MIN_MEM_MB} MB" "use a target with >= ${MIN_MEM_MB} MB RAM"
    fi

    disk_gb=$(remote "df -BG --output=avail / | tail -1 | tr -dc '0-9'" 2>/dev/null || echo 0)
    [[ "$disk_gb" =~ ^[0-9]+$ ]] || disk_gb=0
    if (( disk_gb >= MIN_DISK_GB )); then
        ok "target disk (${disk_gb} GB free on / >= ${MIN_DISK_GB} GB)"
    else
        fail "target has ${disk_gb} GB free on /, need >= ${MIN_DISK_GB} GB" "free up space on / or use a larger target"
    fi
}

# -------- run --------
case "$PROVIDER" in
    lxd-local) preflight_lxd_local ;;
    existing)  preflight_existing ;;
esac

# -------- summary --------
echo
if (( FAILS > 0 )); then
    log "preflight: $FAILS check(s) FAILED -- fix the items above (or re-run with --fix for the safe ones) before install"
    exit 1
else
    log "preflight: all checks passed -- safe to run install-on.sh --provider $PROVIDER"
    exit 0
fi
