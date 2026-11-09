#!/bin/bash
#
# provision-existing.sh -- Layer P (provisioning) for a machine that already exists.
#
# Provisions NOTHING. The target is any host you can already reach over SSH:
# a bare-metal box, a VPS, or an LXC/VM you created yourself. This script only
# VERIFIES that the target satisfies the contract Layer A depends on, then
# emits the same JSON contract the creating providers emit.
#
# Why this exists: everything after Layer P already talks to the target over
# plain SSH (scp the stage scripts, ssh sudo -n bash). Layer P is the only
# LXD-specific part of the installer. With this provider the installer stops
# requiring LXD on the controller and works against any SSH-reachable host.
#
# Contract output (always JSON if --json, text otherwise):
#   { ip, ssh_user, ssh_key, provider="existing", provider_id=<name> }
#
# Usage:
#   provision-existing.sh --name NAME --ip ADDR --ssh-key /path/id.pub \
#                         [--ssh-user admin]
#
# Optional:
#   --ssh-user USER   SSH user on the target (default: admin)
#   --force           Proceed even if the target already has Smart Node stages
#   --json            Print JSON contract on success
#   --help            This message
#
# Requirements on the TARGET (verified here, never installed):
#   - SSH reachable with the private key matching --ssh-key
#   - that user has passwordless sudo (sudo -n true)
#   - systemd + apt (Layer A and the B-stages assume both)
#   - >= 2 GB RAM and >= 10 GB free on /

set -euo pipefail

SCRIPT_VERSION="0.1.0"

# -------- defaults --------
NAME=""
IP=""
SSH_KEY=""
SSH_USER="admin"
FORCE=0
JSON_OUTPUT=0

MIN_MEM_MB=2048
MIN_DISK_GB=10

# -------- helpers --------
die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[$(date '+%H:%M:%S')] $*"; }
usage() { awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"; }

# -------- arg parse --------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)     NAME="$2"; shift 2 ;;
        --ip)       IP="$2"; shift 2 ;;
        --ssh-key)  SSH_KEY="$2"; shift 2 ;;
        --ssh-user) SSH_USER="$2"; shift 2 ;;
        --force)    FORCE=1; shift ;;
        --json)     JSON_OUTPUT=1; shift ;;
        --help|-h)  usage; exit 0 ;;
        # Accepted and ignored: the orchestrator passes sizing/placement flags
        # that only mean something to a provider that CREATES the machine.
        --disk|--memory|--cpu|--image)          shift 2 ;;
        --subnet|--network|--storage-pool)      shift 2 ;;
        --recreate|--refresh-image)             shift ;;
        *)          die "Unknown arg: $1 (use --help)" ;;
    esac
done

# -------- validate args --------
[[ -n "$NAME" ]]    || die "--name is required"
[[ -n "$IP" ]]      || die "--ip is required"
[[ -n "$SSH_KEY" ]] || die "--ssh-key is required (no silent default)"

[[ "$NAME" =~ ^[a-z][a-z0-9-]{0,30}[a-z0-9]$ ]] \
    || die "Invalid --name '$NAME' (lowercase alnum + dash, 2-32 chars, must start with letter)"
[[ -r "$SSH_KEY" ]] || die "SSH key not readable: $SSH_KEY"
grep -qE '^(ssh-rsa|ssh-ed25519|ecdsa-sha2|ssh-dss)' "$SSH_KEY" \
    || die "$SSH_KEY does not look like an SSH public key"

# Layer A is handed the PRIVATE key; derive and check it here so the operator
# finds out now rather than three minutes into the install.
SSH_KEY_PRIV="${SSH_KEY%.pub}"
[[ -r "$SSH_KEY_PRIV" ]] \
    || die "Private key not readable at $SSH_KEY_PRIV (expected alongside $SSH_KEY)"

command -v ssh >/dev/null 2>&1 || die "ssh client not found on the controller"

SSH_OPTS=(-i "$SSH_KEY_PRIV" -o BatchMode=yes -o StrictHostKeyChecking=accept-new
          -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
target="$SSH_USER@$IP"

remote() { ssh "${SSH_OPTS[@]}" "$target" "$@"; }

# -------- verify target --------
log "Pre-flight (name=$NAME target=$target)"

remote true >/dev/null 2>&1 \
    || die "cannot SSH to $target with $SSH_KEY_PRIV -- check the host is up, the user exists, and the key is in its authorized_keys"
log "SSH OK"

remote "sudo -n true" >/dev/null 2>&1 \
    || die "$SSH_USER on $IP lacks passwordless sudo -- add: $SSH_USER ALL=(ALL) NOPASSWD: ALL"
log "passwordless sudo OK"

remote "command -v systemctl" >/dev/null 2>&1 \
    || die "target has no systemctl -- Layer A and the B-stages require systemd"
remote "command -v apt-get" >/dev/null 2>&1 \
    || die "target has no apt-get -- only Debian/Ubuntu targets are supported"

os_pretty=$(remote ". /etc/os-release 2>/dev/null && echo \"\$PRETTY_NAME\"" 2>/dev/null || echo "unknown")
log "target OS: $os_pretty"

mem_mb=$(remote "awk '/MemTotal/ {print int(\$2/1024)}' /proc/meminfo" 2>/dev/null || echo 0)
[[ "$mem_mb" =~ ^[0-9]+$ ]] || mem_mb=0
(( mem_mb >= MIN_MEM_MB )) \
    || die "target has ${mem_mb} MB RAM, need >= ${MIN_MEM_MB} MB"

disk_gb=$(remote "df -BG --output=avail / | tail -1 | tr -dc '0-9'" 2>/dev/null || echo 0)
[[ "$disk_gb" =~ ^[0-9]+$ ]] || disk_gb=0
(( disk_gb >= MIN_DISK_GB )) \
    || die "target has ${disk_gb} GB free on /, need >= ${MIN_DISK_GB} GB"
log "resources OK (${mem_mb} MB RAM, ${disk_gb} GB free on /)"

# Refuse to silently re-install over a node that is already set up. Re-running
# a single stage is fine; a full --all over an existing node is almost always
# an accident.
if [[ "$FORCE" -eq 0 ]] \
   && remote "sudo -n test -d /var/lib/smart-node/stages" >/dev/null 2>&1; then
    die "target already has /var/lib/smart-node/stages -- it looks like a Smart Node already. Use --force to proceed anyway."
fi

log "Target check OK -- provisioning nothing, the machine already exists"

# -------- output --------
TIMESTAMP=$(date -Iseconds)

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
    cat <<JSONEOF
{
  "status": "ok",
  "provider": "existing",
  "provider_id": "$NAME",
  "ip": "$IP",
  "ssh_user": "$SSH_USER",
  "ssh_key": "$SSH_KEY",
  "os": "$os_pretty",
  "created_at": "$TIMESTAMP",
  "script_version": "$SCRIPT_VERSION"
}
JSONEOF
else
    cat <<TXTEOF

[OK] Target accepted (nothing provisioned -- machine already existed)
  Provider:    existing
  Node name:   $NAME
  IP:          $IP
  SSH user:    $SSH_USER
  SSH key:     $SSH_KEY
  OS:          $os_pretty
  Version:     $SCRIPT_VERSION ($TIMESTAMP)

Next: configure/configure-host.sh --ip $IP --ssh-user $SSH_USER --ssh-key $SSH_KEY_PRIV
TXTEOF
fi
