#!/bin/bash
#
# provision-lxd-local.sh -- Layer P (provisioning) for local LXD.
#
# Creates a minimally-bootstrapped LXC container: static IP, openssh-server,
# admin user with the supplied SSH key, NOPASSWD: ALL bootstrap sudoers.
# Nothing else. Layer A (configure-host.sh) tightens / hardens everything.
#
# Contract output (always JSON if --json, text otherwise):
#   { ip, ssh_user="admin", ssh_key=<path>, provider="lxd-local", provider_id=<name> }
#
# Usage:
#   provision-lxd-local.sh --name NAME --ip 10.0.100.X --ssh-key /path/pub
#
# Optional:
#   --subnet 10.0.100.0/24         subnet gating --ip + storage-path check
#                                  (default). --ip must fall inside it.
#   --network lxdbr0               LXD bridge/network to attach eth0 (default)
#   --storage-pool default         LXD storage pool for the root disk (default)
#   --image images:debian/12       LXD image source (default)
#   --memory 2GB                   Container memory limit
#   --cpu 2                        CPU limit
#   --disk 10GB                    Root disk
#   --recreate                     Delete existing container first
#   --refresh-image                Pull fresh image
#   --from-golden ALIAS            Start from a golden stand image (published by
#                                  provision/build-stand-golden.sh) instead of the
#                                  base Debian image. The golden image already has
#                                  Layer A and B01-B06 complete. SSH host keys are
#                                  regenerated and the admin key is replaced with
#                                  the caller's --ssh-key. Outputs golden_image and
#                                  golden_commit in the JSON contract.
#   --ssh-user USER                Accepted for orchestrator compatibility. This
#                                  provisioner CREATES the account and reports it
#                                  in its contract, so the only accepted value is
#                                  admin; anything else is refused rather than
#                                  silently ignored.
#   --json                         Print JSON contract on success
#   --help                         This message

set -euo pipefail

SCRIPT_VERSION="0.3.0"

# -------- defaults --------
NAME=""
IP=""
SSH_KEY=""
IMAGE="images:debian/12"
SUBNET="10.0.100.0/24"
NETWORK="lxdbr0"
STORAGE_POOL="default"
# 2560MB, not "2GB", and the unit is the whole point. LXD reads GB as decimal,
# so "2GB" gives the container 1907 MiB -- while provision-existing.sh requires
# MIN_MEM_MB=2048 MiB. The installer therefore created a node that its own
# `--provider existing` path refused by 141 MiB, which is exactly the re-run the
# base install prints as the next step ("re-run this installer with --all").
# Measured on test-node, 2026-08-14: "target has 1907 MB RAM, need >= 2048 MB".
MEMORY="2560MB"
CPU="2"
DISK="10GB"
RECREATE=0
REFRESH_IMAGE=0
JSON_OUTPUT=0
FROM_GOLDEN=""   # LXD image alias to start from instead of IMAGE (--from-golden)

# -------- helpers --------
die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[$(date '+%H:%M:%S')] $*"; }
usage() { awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"; }

# -------- arg parse --------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)          NAME="$2"; shift 2 ;;
        --ip)            IP="$2"; shift 2 ;;
        --ssh-key)       SSH_KEY="$2"; shift 2 ;;
        --subnet)        SUBNET="$2"; shift 2 ;;
        --network)       NETWORK="$2"; shift 2 ;;
        --storage-pool)  STORAGE_POOL="$2"; shift 2 ;;
        --image)         IMAGE="$2"; shift 2 ;;
        --memory)        MEMORY="$2"; shift 2 ;;
        --cpu)           CPU="$2"; shift 2 ;;
        --disk)          DISK="$2"; shift 2 ;;
        --recreate)      RECREATE=1; shift ;;
        --refresh-image) REFRESH_IMAGE=1; shift ;;
        --from-golden)   FROM_GOLDEN="$2"; shift 2 ;;
        --ssh-user)
            # install-on.sh forwards this to every provisioner. 'existing'
            # takes it; here the account does not exist yet and this script is
            # what creates it, so the name is ours to state and install-on reads
            # it back out of the JSON contract two lines after calling us.
            # Refusing a different value beats ignoring it: a caller who asked
            # for 'ops' and got 'admin' would find out at the first ssh.
            [[ "$2" == "admin" ]] || die "this provisioner creates the 'admin' account; --ssh-user $2 cannot be honoured"
            shift 2 ;;
        --json)          JSON_OUTPUT=1; shift ;;
        --help|-h)       usage; exit 0 ;;
        *)               die "Unknown arg: $1 (use --help)" ;;
    esac
done

# -------- validate --------
[[ -n "$NAME" ]]    || die "--name is required"
[[ -n "$IP" ]]      || die "--ip is required"
[[ -n "$SSH_KEY" ]] || die "--ssh-key is required (no silent default)"

[[ "$NAME" =~ ^[a-z][a-z0-9-]{0,30}[a-z0-9]$ ]] \
    || die "Invalid --name '$NAME' (lowercase alnum + dash, 2-32 chars, must start with letter)"
# --subnet gates --ip. Default 10.0.100.0/24 reproduces the historical
# hard-coded 10.0.100.X range. Validate the CIDR shape, then assert --ip
# falls inside it (any prefix length) via masked-integer comparison.
[[ "$SUBNET" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] \
    || die "Invalid --subnet '$SUBNET' (expected IPv4/prefix, e.g. 10.0.100.0/24)"
[[ "$IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
    || die "Invalid --ip '$IP' (expected an IPv4 address)"
_snet="${SUBNET%/*}"; _spfx="${SUBNET#*/}"
(( _spfx >= 0 && _spfx <= 32 )) || die "Invalid --subnet '$SUBNET' (prefix out of range 0-32)"
ip_to_int() {
    local o i=0
    for o in ${1//./ }; do
        (( 10#$o <= 255 )) || return 1
        i=$(( (i << 8) | 10#$o ))
    done
    printf '%s' "$i"
}
_ipi=$(ip_to_int "$IP")    || die "Invalid --ip '$IP' (octet out of range 0-255)"
_neti=$(ip_to_int "$_snet") || die "Invalid --subnet '$SUBNET' (octet out of range 0-255)"
if (( _spfx == 0 )); then _mask=0; else _mask=$(( (0xFFFFFFFF << (32 - _spfx)) & 0xFFFFFFFF )); fi
(( (_ipi & _mask) == (_neti & _mask) )) \
    || die "--ip '$IP' is not inside --subnet '$SUBNET'"
[[ -r "$SSH_KEY" ]] || die "SSH key not readable: $SSH_KEY"

grep -qE '^(ssh-rsa|ssh-ed25519|ecdsa-sha2|ssh-dss)' "$SSH_KEY" \
    || die "$SSH_KEY does not look like an SSH public key"

command -v lxc >/dev/null 2>&1 || die "lxc CLI not found. Install LXD via snap."

# -------- pre-flight --------
log "Pre-flight (name=$NAME ip=$IP image=$IMAGE)"

if lxc info "$NAME" >/dev/null 2>&1; then
    if [[ "$RECREATE" -eq 1 ]]; then
        log "Container $NAME exists -- destroying (--recreate)"
        lxc delete --force "$NAME"
    else
        die "Container '$NAME' already exists. To continue or update the install in it, run
       install-on.sh again with --provider existing --force --ssh-user admin and the same
       --ip, --ssh-key and node options. --recreate DELETES it and starts over."
    fi
fi

existing_ip_user=$(lxc list -c n4 --format csv 2>/dev/null \
    | awk -F, -v ip="$IP" '$2 ~ ip {print $1}' | head -1 || true)
[[ -z "$existing_ip_user" ]] || die "IP $IP is already used by container '$existing_ip_user'"

ping -c1 -W1 "$IP" >/dev/null 2>&1 && die "IP $IP responds to ping -- not safe to reuse" || true

to_bytes() { numfmt --from=iec "${1%B}" 2>/dev/null || echo 0; }

need_mem=$(to_bytes "$MEMORY")
have_mem=$(( $(awk '/MemAvailable/ {print $2}' /proc/meminfo) * 1024 ))
need_mem_buf=$((need_mem * 3 / 2))
(( have_mem >= need_mem_buf )) \
    || die "Insufficient host memory: need ~$(numfmt --to=iec $need_mem_buf), have $(numfmt --to=iec $have_mem)"

have_cpu=$(nproc)
(( have_cpu >= CPU )) || die "Insufficient host CPU: need $CPU cores, have $have_cpu"

need_disk=$(to_bytes "$DISK")
need_disk_buf=$((need_disk + 2 * 1024 * 1024 * 1024))
storage_path="/var/snap/lxd/common/lxd/storage-pools/$STORAGE_POOL"
have_disk=$(df --output=avail -B1 "$storage_path" 2>/dev/null | tail -1)
if [[ -n "$have_disk" && "$have_disk" =~ ^[0-9]+$ && $have_disk -lt $need_disk_buf ]]; then
    die "Insufficient disk: need $(numfmt --to=iec $need_disk_buf), have $(numfmt --to=iec $have_disk)"
fi

lxc storage info "$STORAGE_POOL" >/dev/null 2>&1 || die "LXD storage pool '$STORAGE_POOL' not found"
lxc network info "$NETWORK"  >/dev/null 2>&1 || die "LXD network '$NETWORK' not found"

log "Host check OK"

# -------- init + static IP + start --------
# When starting from a golden image, use its alias instead of the base
# Debian image. The golden image has Layer A and B01-B06 already done, so
# the bootstrap step below is replaced with a key-update-only step.
if [[ -n "$FROM_GOLDEN" ]]; then
    log "Initialising $NAME from golden image '${FROM_GOLDEN}'"
    lxc image info "$FROM_GOLDEN" >/dev/null 2>&1 \
        || die "Golden image '${FROM_GOLDEN}' not found (run provision/build-stand-golden.sh first)"
    EFFECTIVE_IMAGE="$FROM_GOLDEN"
else
    log "Initialising $NAME from $IMAGE"
    [[ "$REFRESH_IMAGE" -eq 1 ]] && lxc image refresh "$IMAGE" 2>/dev/null || true
    EFFECTIVE_IMAGE="$IMAGE"
fi

# security.nesting=true: Smart Node stage B03 installs Docker, and the
# Phase 2 companions are Docker containers -- the container must allow
# nested containerisation or Docker cannot start inside it.
INIT_ARGS=("$EFFECTIVE_IMAGE" "$NAME"
    -c limits.memory="$MEMORY"
    -c limits.cpu="$CPU"
    -c security.nesting=true
    -d root,size="$DISK")
# Non-default storage pool: bind the root disk to it. With the default pool
# the command is byte-identical to before (root inherits pool from the
# `default` profile).
[[ "$STORAGE_POOL" != "default" ]] && INIT_ARGS+=(-s "$STORAGE_POOL")
lxc init "${INIT_ARGS[@]}" >/dev/null

log "Assigning static IP $IP on $NETWORK"
# Override the profile-inherited eth0. The `default` profile attaches eth0 via
# the `network` key (lxdbr0); a non-default --network re-points it. For the
# default bridge the command is identical to before (no network= key set).
OVERRIDE_ARGS=("$NAME" eth0 ipv4.address="$IP")
[[ "$NETWORK" != "lxdbr0" ]] && OVERRIDE_ARGS+=(network="$NETWORK")
lxc config device override "${OVERRIDE_ARGS[@]}" >/dev/null

GW="${IP%.*}.1"
NET_CFG=$(mktemp /tmp/lxc-eth0-net.XXXXXX)
cat > "$NET_CFG" <<NETEOF
[Match]
Name=eth0

[Network]
Address=$IP/24
Gateway=$GW
DNS=$GW
NETEOF
lxc file push "$NET_CFG" "$NAME/etc/systemd/network/eth0.network" --mode=0644
rm -f "$NET_CFG"

log "Starting $NAME"
lxc start "$NAME"

log "Waiting for container network..."
for i in {1..60}; do
    if lxc exec "$NAME" -- ip -4 addr show eth0 2>/dev/null | grep -q "inet $IP"; then
        break
    fi
    sleep 1
    (( i == 60 )) && die "Container did not get IP $IP within 60s"
done

if lxc exec "$NAME" -- which cloud-init >/dev/null 2>&1; then
    log "Waiting for cloud-init..."
    lxc exec "$NAME" -- cloud-init status --wait >/dev/null 2>&1 || true
fi

# -------- minimal bootstrap (SSH-reachable admin user) --------
if [[ -n "$FROM_GOLDEN" ]]; then
    # Golden image: admin user and SSH are already configured, but host keys
    # were stripped from the image by build-stand-golden.sh (to prevent all
    # stands sharing identical keys). Regenerate unique host keys, then update
    # authorized_keys to match the caller's --ssh-key.
    log "Golden image: regenerating SSH host keys and updating admin SSH key"
    lxc exec "$NAME" -- ssh-keygen -A
    lxc file push "$SSH_KEY" "$NAME/root/.ssh-authorized-key" --mode=0600
    lxc exec "$NAME" -- bash -c '
        ADMIN_USER="admin"
        mkdir -p /home/$ADMIN_USER/.ssh
        chmod 700 /home/$ADMIN_USER/.ssh
        cp /root/.ssh-authorized-key /home/$ADMIN_USER/.ssh/authorized_keys
        chmod 600 /home/$ADMIN_USER/.ssh/authorized_keys
        chown -R $ADMIN_USER:$ADMIN_USER /home/$ADMIN_USER/.ssh
        rm -f /root/.ssh-authorized-key
        # RESET-FAILED FIRST, AND DO NOT SWALLOW THE RESULT.
        # The image ships with no host keys, so sshd tried to start during boot,
        # exited each time with "no hostkeys available", and hit its start-rate
        # limit before ssh-keygen -A ran above. A plain restart is then refused
        # with "Start request repeated too quickly", and the `|| true` that used
        # to end this line turned that into a provisioned container with no ssh
        # at all -- which the next step, an install over ssh, discovers for you.
        systemctl reset-failed ssh sshd >/dev/null 2>&1 || true
        systemctl restart ssh >/dev/null 2>&1 || systemctl restart sshd >/dev/null 2>&1 || true
        for _i in $(seq 1 15); do
            systemctl is-active ssh >/dev/null 2>&1 && break
            systemctl is-active sshd >/dev/null 2>&1 && break
            sleep 1
        done
        if ! systemctl is-active ssh >/dev/null 2>&1 && ! systemctl is-active sshd >/dev/null 2>&1; then
            echo "[provision] FATAL: sshd is not running after regenerating host keys" >&2
            journalctl -u ssh --no-pager -n 5 2>/dev/null >&2 || true
            exit 1
        fi
        echo "[provision] golden: host keys regenerated, ssh key updated, sshd active"
    ' || die "golden container came up without a working sshd"
else
    log "Installing openssh-server + sudo + admin user"

    BOOTSTRAP=$(mktemp /tmp/provision-bootstrap.XXXXXX.sh)
    trap 'rm -f "$BOOTSTRAP"' EXIT

    cat > "$BOOTSTRAP" <<'BOOTSTRAP_EOF'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
ADMIN_USER="admin"

apt-get update -qq
apt-get install -y -qq openssh-server sudo ca-certificates

if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo "$ADMIN_USER"
fi

# Bootstrap sudoers: NOPASSWD: ALL. Layer A will replace with scoped sudoers.
cat > /etc/sudoers.d/90-admin-bootstrap <<SUDOEOF
$ADMIN_USER ALL=(ALL) NOPASSWD: ALL
SUDOEOF
chmod 440 /etc/sudoers.d/90-admin-bootstrap

mkdir -p /home/$ADMIN_USER/.ssh
chmod 700 /home/$ADMIN_USER/.ssh
cat /root/.ssh-authorized-key > /home/$ADMIN_USER/.ssh/authorized_keys
chmod 600 /home/$ADMIN_USER/.ssh/authorized_keys
chown -R $ADMIN_USER:$ADMIN_USER /home/$ADMIN_USER/.ssh
rm -f /root/.ssh-authorized-key

systemctl enable ssh >/dev/null 2>&1 || systemctl enable sshd >/dev/null 2>&1 || true
systemctl restart ssh >/dev/null 2>&1 || systemctl restart sshd >/dev/null 2>&1 || true

echo "[provision] ok"
BOOTSTRAP_EOF

    lxc file push "$BOOTSTRAP" "$NAME/root/bootstrap.sh" --mode=0755
    lxc file push "$SSH_KEY"   "$NAME/root/.ssh-authorized-key" --mode=0600

    log "Running bootstrap inside container"
    lxc exec "$NAME" -- bash /root/bootstrap.sh
    lxc exec "$NAME" -- rm -f /root/bootstrap.sh
fi

# Quick verify the user is SSH-able (we don't actually SSH yet -- A does).
lxc exec "$NAME" -- test -f /home/admin/.ssh/authorized_keys \
    || die "post-bootstrap: admin authorized_keys missing"
# The sudoers file is only guaranteed present for a from-scratch provision.
# A golden image may have already had the sudoers entry replaced by Layer A.
if [[ -z "$FROM_GOLDEN" ]]; then
    lxc exec "$NAME" -- test -f /etc/sudoers.d/90-admin-bootstrap \
        || die "post-bootstrap: sudoers bootstrap missing"
fi

# -------- output --------
TIMESTAMP=$(date -Iseconds)

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
    _GOLDEN_LINES=""
    if [[ -n "$FROM_GOLDEN" ]]; then
        _GOLDEN_COMMIT=$(lxc image info "$FROM_GOLDEN" 2>/dev/null \
            | awk '/^Properties:/{p=1} p && /user\.git_commit:/{print $2; exit}')
        _GOLDEN_LINES="  \"golden_image\": \"${FROM_GOLDEN}\","$'\n'"  \"golden_commit\": \"${_GOLDEN_COMMIT:-unknown}\","
    fi
    cat <<JSONEOF
{
  "status": "ok",
  "provider": "lxd-local",
  "provider_id": "$NAME",
  "ip": "$IP",
  "ssh_user": "admin",
  "ssh_key": "$SSH_KEY",
  "image": "${FROM_GOLDEN:-$IMAGE}",
${_GOLDEN_LINES:+${_GOLDEN_LINES}}
  "subnet": "$SUBNET",
  "network": "$NETWORK",
  "storage_pool": "$STORAGE_POOL",
  "created_at": "$TIMESTAMP",
  "script_version": "$SCRIPT_VERSION"
}
JSONEOF
else
    cat <<TXTEOF

[OK] Provisioned
  Provider:    lxd-local
  Container:   $NAME
  IP:          $IP  (subnet $SUBNET, bridge $NETWORK)
  SSH user:    admin
  SSH key:     $SSH_KEY
  Image:       $IMAGE
  Storage:     $STORAGE_POOL
  Version:     $SCRIPT_VERSION ($TIMESTAMP)

Next: configure/configure-host.sh --ip $IP --ssh-user admin --ssh-key ${SSH_KEY%.pub}
TXTEOF
fi
