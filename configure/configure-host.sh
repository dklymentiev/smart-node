#!/bin/bash
#
# configure-host.sh -- Layer A (host configuration), provider-agnostic.
#
# Takes any SSH-reachable Linux host (output of any provision-*.sh script
# or a manually-provisioned VPS) and hardens it: ssh, ufw, fail2ban,
# unattended-upgrades, sysctl baseline, monitoring, locale, swap.
# Marks Layer A done at /var/lib/smart-node/stages/A.state and writes
# a Layer A section into /etc/smart-node/install-report.md.
#
# Usage:
#   configure-host.sh --ip IP --ssh-user USER --ssh-key /path/to/private/key
#
# Optional:
#   --allow-from CIDR     SSH inbound CIDR (required; install-on.sh derives it)
#   --ssh-port PORT       SSH port on the target (default: 22). Pass when the
#                         target uses a non-standard port. The UFW rule opens
#                         this port, not 22. The SSH connection also uses it.
#   --node-name NAME      enable A6 (vpn-server): set the node hostname and
#                         install a per-node WireGuard server, wildcard DNS,
#                         and phone QR. Omitted -> A6 skipped (no VPN).
#                         If --node-fqdn is also given, the node is renamed to
#                         that FQDN. Without --node-fqdn, the existing hostname
#                         is preserved if it is already set to a real name;
#                         otherwise the node falls back to NAME.sn.vpn with
#                         a warning (sn.vpn is a placeholder, not a real domain).
#   --node-fqdn DOMAIN    set the node FQDN explicitly. It becomes the node
#                         hostname, so every FQDN-derived host (panel./auth./
#                         voice./doci.) follows it. Point your own DNS/hosts
#                         at the node IP. Needs --node-name.
#   --no-vpn              skip the A6 WireGuard server, wildcard dnsmasq DNS
#                         and phone QR. The node FQDN/hostname is still set (so
#                         routing works); typically paired with --node-fqdn on
#                         a plain LAN node. Default: A6 runs as before.
#   --vpn-endpoint H:P    host:port the onboarding phone uses to reach this
#                         node's WireGuard (default: <node-ip>:51820). Pass
#                         it for a NAT'd node, where <node-ip> is not the
#                         publicly reachable address. Needs --node-name.
#   --timezone ZONE       set the host zone (IANA name, e.g. Europe/Berlin).
#                         Omitted -> the host keeps the zone it already has;
#                         the node reads "local time" from the host.
#   --json                Print machine-parseable JSON result on success
#   --help                This message
#
# Pre-condition: target host SSH-reachable as $USER with $KEY, $USER has
# NOPASSWD sudo (provision-*.sh sets this up).
#
# Exits 0 on success.

set -euo pipefail

SCRIPT_VERSION="0.3.2"

# -------- defaults --------
IP=""
SSH_USER=""
SSH_KEY=""
ALLOW_FROM=""
SSH_PORT=22
NODE_NAME=""
NODE_FQDN=""
NODE_FQDN_EXPLICIT=0
NO_VPN=0
VPN_ENDPOINT=""
TIMEZONE=""
JSON_OUTPUT=0

# -------- helpers --------
die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[$(date '+%H:%M:%S')] $*"; }
usage() { awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"; }

# -------- arg parse --------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)          IP="$2"; shift 2 ;;
        --ssh-user)    SSH_USER="$2"; shift 2 ;;
        --ssh-key)     SSH_KEY="$2"; shift 2 ;;
        --allow-from)  ALLOW_FROM="$2"; shift 2 ;;
        --ssh-port)    SSH_PORT="$2"; shift 2 ;;
        --node-name)   NODE_NAME="$2"; shift 2 ;;
        --node-fqdn)   NODE_FQDN="$2"; shift 2 ;;
        --no-vpn)      NO_VPN=1; shift ;;
        --vpn-endpoint) VPN_ENDPOINT="$2"; shift 2 ;;
        --timezone)    TIMEZONE="$2"; shift 2 ;;
        --json)        JSON_OUTPUT=1; shift ;;
        --help|-h)     usage; exit 0 ;;
        *)             die "Unknown arg: $1 (use --help)" ;;
    esac
done

# -------- validate --------
[[ -n "$IP" ]]       || die "--ip is required"
[[ -n "$SSH_USER" ]] || die "--ssh-user is required"
[[ -n "$SSH_KEY" ]]  || die "--ssh-key is required (path to PRIVATE key)"
[[ -r "$SSH_KEY" ]]  || die "SSH key not readable: $SSH_KEY"

# Strict input-shape validation. IP, SSH_USER and ALLOW_FROM are
# interpolated into a bootstrap script that runs as root on the target;
# an unvalidated value is a command-injection vector. Accept only the
# expected shapes -- the regexes permit no shell metacharacters.
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] \
    || die "--ssh-user '$SSH_USER' invalid (expected a unix username)"
[[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
    || die "--ip '$IP' invalid (expected an IPv4 address)"

# validate_cidr VALUE -- accept a bare IPv4 or IPv4/prefix; bound every
# octet to 0-255 and the prefix to 0-32. 10#-prefixed arithmetic so a
# zero-padded octet ("08") is read as decimal, not octal.
validate_cidr() {
    local v="$1" ip pfx o
    [[ "$v" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$ ]] \
        || die "--allow-from '$v' invalid (expected IPv4 or IPv4/CIDR)"
    ip="${v%/*}"
    pfx="${v#"$ip"}"; pfx="${pfx#/}"
    for o in ${ip//./ }; do
        (( 10#$o <= 255 )) || die "--allow-from '$v': octet $o out of range (0-255)"
    done
    [[ -z "$pfx" ]] || (( 10#$pfx <= 32 )) \
        || die "--allow-from '$v': prefix /$pfx out of range (0-32)"
}
[[ -n "$ALLOW_FROM" ]] || die "--allow-from is required (install-on.sh derives it automatically)"
validate_cidr "$ALLOW_FROM"

[[ "$SSH_PORT" =~ ^[0-9]+$ ]] && (( SSH_PORT >= 1 && SSH_PORT <= 65535 )) \
    || die "--ssh-port '$SSH_PORT' invalid (expected a port number 1-65535)"

# A6 (vpn-server) inputs. NODE_NAME and VPN_ENDPOINT are interpolated into
# the root bootstrap script -- validate their shape with the same rigour as
# IP/SSH_USER. NODE_NAME is a single DNS label (the host part of the node
# FQDN <name>.sn.vpn); VPN_ENDPOINT is a host:port. Neither shape permits
# the sed delimiter '|' or a shell metacharacter.
if [[ -n "$VPN_ENDPOINT" && -z "$NODE_NAME" ]]; then
    die "--vpn-endpoint needs --node-name (A6 is disabled without a node name)"
fi
# TIMEZONE is interpolated into the bootstrap too: an IANA name only.
if [[ -n "$TIMEZONE" ]]; then
    [[ "$TIMEZONE" =~ ^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$ && "$TIMEZONE" != *..* ]] \
        || die "--timezone must be an IANA zone name like Europe/Berlin (got '$TIMEZONE')"
fi
if [[ -n "$NODE_FQDN" && -z "$NODE_NAME" ]]; then
    die "--node-fqdn needs --node-name"
fi
if [[ -n "$VPN_ENDPOINT" && "$NO_VPN" -eq 1 ]]; then
    die "--vpn-endpoint conflicts with --no-vpn (no WireGuard peer is generated)"
fi
if [[ -n "$NODE_NAME" ]]; then
    [[ "$NODE_NAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
        || die "--node-name '$NODE_NAME' invalid (expected a lowercase DNS label)"
    # Node FQDN: the operator's --node-fqdn (a LAN/public domain), or the
    # sn.vpn placeholder as a last-resort default (configure-host.sh warns
    # when it falls back and the target already has a real hostname). Track
    # whether the FQDN was given explicitly so the bootstrap can preserve the
    # target's existing hostname rather than renaming it to sn.vpn.
    if [[ -n "$NODE_FQDN" ]]; then
        NODE_FQDN_EXPLICIT=1
    else
        NODE_FQDN="$NODE_NAME.sn.vpn"
        NODE_FQDN_EXPLICIT=0
    fi
    [[ "$NODE_FQDN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] \
        || die "--node-fqdn '$NODE_FQDN' invalid (expected a dotted lowercase DNS name)"
    # WireGuard endpoint (A6 only). Default: the node's own IP on the standard
    # port. Skipped under --no-vpn -- no phone peer is generated.
    if [[ "$NO_VPN" -ne 1 ]]; then
        [[ -n "$VPN_ENDPOINT" ]] || VPN_ENDPOINT="$IP:51820"
        [[ "$VPN_ENDPOINT" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] \
            || die "--vpn-endpoint '$VPN_ENDPOINT' invalid (expected host:port)"
    fi
fi

# Sanity: not a public key by accident.
if head -1 "$SSH_KEY" 2>/dev/null | grep -qE '^(ssh-rsa|ssh-ed25519|ecdsa-sha2|ssh-dss)'; then
    die "$SSH_KEY looks like a PUBLIC key. Pass the private key path (drop .pub)."
fi

# Probe SSH reachability before doing anything destructive.
SSH_OPTS=(
    -i "$SSH_KEY"
    -p "$SSH_PORT"
    -o "BatchMode=yes"
    -o "StrictHostKeyChecking=accept-new"
    -o "ConnectTimeout=10"
    -o "UserKnownHostsFile=/tmp/configure-host-known-hosts.$$"
)
# scp uses -P (uppercase) for port; -p means "preserve timestamps".
SCP_OPTS=(
    -i "$SSH_KEY"
    -P "$SSH_PORT"
    -o "BatchMode=yes"
    -o "StrictHostKeyChecking=accept-new"
    -o "ConnectTimeout=10"
    -o "UserKnownHostsFile=/tmp/configure-host-known-hosts.$$"
)
trap 'rm -f /tmp/configure-host-known-hosts.$$' EXIT

log "Probing SSH at $SSH_USER@$IP"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "sudo -n true" \
    || die "SSH probe failed (NOPASSWD sudo not configured, or host unreachable)"

# -------- build bootstrap --------
log "Preparing bootstrap"

BOOTSTRAP=$(mktemp /tmp/configure-host-bootstrap.XXXXXX.sh)
trap 'rm -f "$BOOTSTRAP" /tmp/configure-host-known-hosts.$$' EXIT

cat > "$BOOTSTRAP" <<'BOOTSTRAP_EOF'
#!/bin/bash
# Runs INSIDE the target host as root via SSH + sudo.
# Idempotent. SSHd is RELOADED (not restarted) so our live SSH session
# survives the config change.
set -euo pipefail

ALLOW_FROM="__ALLOW_FROM__"
SCRIPT_VERSION="__SCRIPT_VERSION__"
ADMIN_USER="__SSH_USER__"
SSH_PORT="__SSH_PORT__"
NODE_NAME="__NODE_NAME__"
NODE_FQDN="__NODE_FQDN__"
NODE_FQDN_EXPLICIT="__NODE_FQDN_EXPLICIT__"
NO_VPN="__NO_VPN__"
VPN_ENDPOINT="__VPN_ENDPOINT__"
TIMEZONE="__TIMEZONE__"

echo "[A] preparing /var/lib/smart-node + /etc/smart-node"
install -d -m 0755 -o root -g root /var/lib/smart-node
install -d -m 0755 -o root -g root /var/lib/smart-node/stages
install -d -m 0755 -o root -g root /etc/smart-node

if [[ ! -f /etc/smart-node/install-report.md ]]; then
    cat > /etc/smart-node/install-report.md <<RPTEOF
# Smart Node install report

**Initialised:** $(date -Iseconds)
RPTEOF
    chmod 0644 /etc/smart-node/install-report.md
fi

echo "[A] apt update + upgrade"
export DEBIAN_FRONTEND=noninteractive

# WAIT FOR THE DPKG LOCK INSTEAD OF DYING ON IT. A machine that has just booted
# for the first time is usually running unattended-upgrades, which holds
# /var/lib/dpkg/lock-frontend for a minute or two. Without this the very first
# apt call fails with "Could not get lock ... held by process N (unattended-upgr)"
# and Layer A aborts -- on a brand new cloud VPS, which is the product's main
# path, not an edge case. Observed 2026-08-25 on a freshly provisioned node.
#
# Written as apt config rather than a flag so it covers every apt call in this
# script and any added later, including the ones further down.
cat > /etc/apt/apt.conf.d/99smart-node-lock-timeout <<'APTEOF'
// Block for up to 10 minutes waiting for the dpkg lock rather than failing.
DPkg::Lock::Timeout "600";
APTEOF

apt-get update -qq
apt-get -y -qq upgrade

echo "[A] base packages"
# python3-systemd is not decoration: the sshd jail written below asks for the
# systemd backend, and without this module fail2ban refuses to start AT ALL --
# "Backend 'systemd' failed to initialize due to No module named 'systemd'",
# then "Failed to initialize any backend for Jail 'sshd'", then Exiting. The
# jail is not degraded, the daemon is dead, and with it every other jail. It is
# not a dependency of the fail2ban package because the package does not know
# which backend you will choose.
apt-get install -y -qq \
    ufw fail2ban python3-systemd unattended-upgrades \
    ca-certificates curl wget vim htop \
    sudo openssh-server locales

echo "[A] sshd hardening (config write; reload at end)"
SSHD=/etc/ssh/sshd_config
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' "$SSHD"
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD"
sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "$SSHD"
sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' "$SSHD"
grep -q '^UsePAM' "$SSHD" || echo 'UsePAM yes' >> "$SSHD"

# UFW comes LAST (after A.state write) to avoid killing the live SSH
# session before persistent state is on disk. See below.

echo "[A] fail2ban sshd jail (systemd backend)"
cat > /etc/fail2ban/jail.d/sshd.local <<F2BEOF
[sshd]
enabled  = true
port     = ssh
filter   = sshd
backend  = systemd
bantime  = 1h
findtime = 10m
maxretry = 5
F2BEOF
systemctl enable fail2ban >/dev/null 2>&1
# A restart that fails here USED to be a warning, and the install went on to fail
# forty seconds later at `chk fail2ban-client status sshd` with no explanation
# attached. Say the reason at the moment it is known: the daemon writes it to its
# own log and then exits, so by the time the verify runs there is nothing to ask.
if ! systemctl restart fail2ban; then
    echo "[A] WARN: fail2ban failed to start -- the verify below will fail. Reason:"
    tail -5 /var/log/fail2ban.log 2>/dev/null | sed 's/^/[A]        /'
fi

echo "[A] unattended-upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<APTEOF
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
APTEOF

# Auto-reboot file MUST NOT end with .local (apt.conf.d ignores those).
cat > /etc/apt/apt.conf.d/52unattended-upgrades-reboot <<AUEOF
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
AUEOF

echo "[A] sysctl baseline"
cat > /etc/sysctl.d/99-secure-baseline.conf <<SYSEOF
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
kernel.kptr_restrict = 1
kernel.dmesg_restrict = 1
SYSEOF
sysctl --system >/dev/null 2>&1 || true

echo "[A] timezone + locale"
# The host zone is the operator's to choose. Set only when asked; an update
# never moves a node that was set by hand.
if [[ -n "$TIMEZONE" ]]; then
    [[ -f "/usr/share/zoneinfo/$TIMEZONE" ]] || { echo "[ERROR] unknown zone $TIMEZONE" >&2; exit 1; }
    echo "$TIMEZONE" > /etc/timezone
    ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime
    dpkg-reconfigure -f noninteractive tzdata >/dev/null 2>&1 || true
fi
sed -i 's/^# *\(en_US.UTF-8\)/\1/' /etc/locale.gen 2>/dev/null || true
locale-gen >/dev/null 2>&1 || true
update-locale LANG=en_US.UTF-8 >/dev/null 2>&1 || true
grep -q '^LANG=' /etc/environment 2>/dev/null || echo 'LANG=en_US.UTF-8' >> /etc/environment

echo "[A] swap (2G; skipped if unprivileged LXC)"
if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    if swapon /swapfile 2>/dev/null; then
        grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        echo "[A] swap enabled"
    else
        echo "[A] swapon not permitted -- skipped"
        rm -f /swapfile
    fi
fi

echo "[A8] monitoring -- prometheus-node-exporter"
# A8 is MANDATORY (installer-stages.md). node_exporter is the host-metrics
# baseline every node carries, independent of whether the Phase-2
# observability companion (Grafana) is later installed -- Grafana, when it
# arrives, scrapes this. Bind to :9100; UFW (end of script) restricts reach
# to ALLOW_FROM. The package ships its own systemd unit.
apt-get install -y -qq prometheus-node-exporter
cat > /etc/default/prometheus-node-exporter <<NEEOF
# Managed by Smart Node configure-host.sh (Layer A, A8 monitoring).
ARGS="--web.listen-address=:9100"
NEEOF
systemctl enable prometheus-node-exporter >/dev/null 2>&1
systemctl restart prometheus-node-exporter \
    || echo "[A8] WARN: prometheus-node-exporter failed to start"

# ------------------------------------------------------------------------
# A6 -- vpn-server (per-node WireGuard + wildcard DNS + phone QR)
# ------------------------------------------------------------------------
# Optional: runs only when configure-host.sh was given --node-name. It is
# the foundation of the node's identity inside its own VPN:
#   * FQDN <name>.sn.vpn -- every later stage (B03/B05/B06) and companion
#     derives panel./auth./voice. from `hostname -f`, so all node domains
#     agree WITHOUT threading a domain variable through each stage.
#   * WireGuard server `wg-node` (10.88.0.1/24, UDP 51820, kernel mode).
#   * dnsmasq answering *.<name>.sn.vpn -> 10.88.0.1 on the wg-node iface.
#   * a ready-to-scan phone peer (phone1) + QR at /etc/wireguard/peers/.
# The two UFW rules A6 needs are added in the UFW section at end of script.
if [[ -n "$NODE_NAME" ]]; then
    # NODE_FQDN is threaded in from configure-host.sh (operator's --node-fqdn,
    # or the sn.vpn placeholder when none was given). The hostname is set
    # REGARDLESS of --no-vpn so every FQDN-derived host (panel./auth./voice./
    # doci.) resolves.
    #
    # When NODE_FQDN was not given explicitly (NODE_FQDN_EXPLICIT=0), preserve
    # the machine's current FQDN if it is already set to a real name. This
    # prevents accidental renames on a machine that already has an identity.
    if [[ "$NODE_FQDN_EXPLICIT" != "1" ]]; then
        existing_fqdn=$(hostname -f 2>/dev/null || true)
        if [[ -n "$existing_fqdn" && "$existing_fqdn" =~ \. && "$existing_fqdn" != "$NODE_NAME.sn.vpn" ]]; then
            echo "[A6] preserving existing hostname $existing_fqdn (pass --node-fqdn to override)"
            NODE_FQDN="$existing_fqdn"
        else
            echo "[A6] WARN: no real hostname on this machine -- defaulting FQDN to $NODE_FQDN (sn.vpn is a placeholder; pass --node-fqdn to set a real domain)"
        fi
    fi
    echo "[A6] node FQDN $NODE_FQDN"

    echo "[A6] setting hostname -> $NODE_FQDN"
    hostnamectl set-hostname "$NODE_FQDN" 2>/dev/null || echo "$NODE_FQDN" > /etc/hostname
    hostname "$NODE_FQDN" 2>/dev/null || true
    # 127.0.1.1 line: FQDN then short name, so `hostname -f` resolves with
    # no DNS. Replace any existing 127.0.1.1 line; add one if absent.
    if grep -qE '^127\.0\.1\.1' /etc/hosts; then
        sed -i "s|^127\.0\.1\.1.*|127.0.1.1\t$NODE_FQDN $NODE_NAME|" /etc/hosts
    else
        printf '127.0.1.1\t%s %s\n' "$NODE_FQDN" "$NODE_NAME" >> /etc/hosts
    fi

    # Pre-flight: if UDP 51820 is already in use by an existing WireGuard
    # interface (e.g. wg0 on a host that is already a VPN endpoint), step
    # aside rather than writing a config that will never start. Treat this
    # run as --no-vpn: the hostname is still set, but WireGuard, dnsmasq
    # and the phone QR are skipped. The verify checks also skip (they are
    # gated on NO_VPN != 1), so stage A can still pass. Pass --no-vpn
    # explicitly on future runs to suppress this message.
    if [[ "$NO_VPN" != "1" ]] && ss -ulnp 2>/dev/null | grep -q ':51820\b'; then
        echo "[A6] UDP 51820 already in use -- stepping aside (--no-vpn behaviour)."
        echo "[A6] An existing WireGuard interface holds the port. Stop it first,"
        echo "[A6] or pass --no-vpn explicitly to skip the VPN server permanently."
        NO_VPN=1
    fi

    if [[ "$NO_VPN" == "1" ]]; then
        echo "[A6] --no-vpn -- skipping WireGuard server, wildcard DNS and phone QR"
    else
    echo "[A6] packages -- wireguard-tools, dnsmasq, qrencode"
    # wireguard-tools ONLY -- NOT the `wireguard` metapackage, which pulls
    # wireguard-dkms + a full kernel build chain to compile a module that
    # is already in the host kernel (mainline since 5.6). wg-quick uses
    # kernel mode against the host module. --no-install-recommends keeps
    # an MTA and other recommends out of the node.
    apt-get install -y -qq --no-install-recommends \
        wireguard-tools dnsmasq qrencode

    install -d -m 0700 -o root -g root /etc/wireguard
    install -d -m 0700 -o root -g root /etc/wireguard/peers

    # Node + phone keypairs -- generated once; a re-run keeps them so the
    # already-printed QR stays valid.
    if [[ ! -s /etc/wireguard/wg-node.key ]]; then
        ( umask 077; wg genkey > /etc/wireguard/wg-node.key )
        wg pubkey < /etc/wireguard/wg-node.key > /etc/wireguard/wg-node.pub
    fi
    if [[ ! -s /etc/wireguard/peers/phone1.key ]]; then
        ( umask 077; wg genkey > /etc/wireguard/peers/phone1.key )
        wg pubkey < /etc/wireguard/peers/phone1.key > /etc/wireguard/peers/phone1.pub
    fi
    NODE_WG_PRIV=$(cat /etc/wireguard/wg-node.key)
    NODE_WG_PUB=$(cat /etc/wireguard/wg-node.pub)
    PHONE_WG_PRIV=$(cat /etc/wireguard/peers/phone1.key)
    PHONE_WG_PUB=$(cat /etc/wireguard/peers/phone1.pub)

    # wg-node.conf -- rewritten each run from the stored keys (idempotent).
    cat > /etc/wireguard/wg-node.conf <<WGEOF
# Smart Node per-node VPN -- generated by configure-host.sh (A6). Do not edit.
[Interface]
Address = 10.88.0.1/24
ListenPort = 51820
PrivateKey = $NODE_WG_PRIV

[Peer]
# phone1 -- onboarding phone (QR at /etc/wireguard/peers/phone1.png)
PublicKey = $PHONE_WG_PUB
AllowedIPs = 10.88.0.2/32
WGEOF
    chmod 0600 /etc/wireguard/wg-node.conf
    systemctl enable wg-quick@wg-node >/dev/null 2>&1
    # restart (not start): a re-run with a changed peer set takes effect.
    systemctl restart wg-quick@wg-node \
        || echo "[A6] WARN: wg-quick@wg-node failed to start"

    echo "[A6] dnsmasq -- wildcard *.$NODE_FQDN -> 10.88.0.1"
    cat > /etc/dnsmasq.d/smart-node.conf <<DNEOF
# Smart Node node DNS -- generated by configure-host.sh (A6). Do not edit.
# Answers only on the wg-node VPN interface. bind-dynamic (not
# bind-interfaces) so the service survives wg-node appearing after
# dnsmasq at boot, and never grabs 0.0.0.0:53.
interface=wg-node
bind-dynamic
no-resolv
server=1.1.1.1
server=8.8.8.8
address=/$NODE_FQDN/10.88.0.1
DNEOF
    systemctl enable dnsmasq >/dev/null 2>&1
    systemctl restart dnsmasq || echo "[A6] WARN: dnsmasq failed to start"

    # Phone-side config + QR. Endpoint is how the phone reaches this node
    # from the public internet; for a NAT'd node it is the host's public
    # IP:forwarded-port (passed as --vpn-endpoint). AllowedIPs = node
    # subnet only -> split tunnel (the phone keeps its normal traffic).
    cat > /etc/wireguard/peers/phone1.conf <<PHEOF
[Interface]
PrivateKey = $PHONE_WG_PRIV
Address = 10.88.0.2/32
DNS = 10.88.0.1

[Peer]
PublicKey = $NODE_WG_PUB
Endpoint = $VPN_ENDPOINT
AllowedIPs = 10.88.0.0/24
PersistentKeepalive = 25
PHEOF
    chmod 0600 /etc/wireguard/peers/phone1.conf
    qrencode -t PNG -o /etc/wireguard/peers/phone1.png \
        < /etc/wireguard/peers/phone1.conf
    chmod 0640 /etc/wireguard/peers/phone1.png
    echo "[A6] phone QR -> /etc/wireguard/peers/phone1.png (endpoint $VPN_ENDPOINT)"
    fi
fi

# NOTE: sudoers tightening is intentionally NOT done here. The bootstrap
# sudoers NOPASSWD: ALL (from Layer P) stays in place until a future
# "finalize" stage at the end of the C pipeline. Reason: Layer C stages
# need NOPASSWD: ALL to install themselves over SSH. Tightening too
# early breaks the orchestrator. Layer P is free to name that sudoers file
# whatever it likes (a cloud image may have set it up already); the verify
# below asserts the CAPABILITY, not any particular provisioner artifact.

echo "[A] inline verify (pre-UFW)"
VERIFY_ERRORS=0
CHECKS_TOTAL=0
chk() {
    CHECKS_TOTAL=$((CHECKS_TOTAL + 1))
    "$@" >/dev/null 2>&1 || { echo "[VERIFY-FAIL] $*" >&2; VERIFY_ERRORS=$((VERIFY_ERRORS + 1)); }
}

chk test -f /home/$ADMIN_USER/.ssh/authorized_keys
chk grep -q '^PermitRootLogin no' /etc/ssh/sshd_config
chk grep -q '^PasswordAuthentication no' /etc/ssh/sshd_config
chk fail2ban-client status sshd
chk test -f /etc/apt/apt.conf.d/20auto-upgrades
chk runuser -u "$ADMIN_USER" -- sudo -n true
chk test -f /etc/sysctl.d/99-secure-baseline.conf
chk grep -q '^LANG=' /etc/environment
chk dpkg -s prometheus-node-exporter
chk systemctl is-active prometheus-node-exporter
_pne_ok=0
for _i in $(seq 1 30); do
    if sh -c "ss -ltn | grep -q ':9100'" >/dev/null 2>&1; then
        _pne_ok=1; break
    fi
    sleep 1
done
CHECKS_TOTAL=$((CHECKS_TOTAL + 1))
if (( ! _pne_ok )); then
    echo "[VERIFY-FAIL] port :9100 not listening after 30s (prometheus-node-exporter)" >&2
    VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
fi

# A6 checks. The node FQDN/hostname is asserted whenever --node-name was
# given (set regardless of --no-vpn); the WireGuard/DNS/QR checks run only
# when the VPN server was actually installed (not --no-vpn).
if [[ -n "$NODE_NAME" ]]; then
    chk test "$(hostname -f 2>/dev/null)" = "$NODE_FQDN"
    if [[ "$NO_VPN" != "1" ]]; then
        chk test -f /etc/wireguard/wg-node.conf
        chk systemctl is-active wg-quick@wg-node
        chk wg show wg-node
        chk systemctl is-active dnsmasq
        chk test -f /etc/wireguard/peers/phone1.png
    fi
fi

if (( VERIFY_ERRORS > 0 )); then
    cat > /var/lib/smart-node/stages/A.state <<STEOF
status=failed
timestamp=$(date -Iseconds)
version=configure-host@$SCRIPT_VERSION
checks_passed=$((CHECKS_TOTAL - VERIFY_ERRORS))/$CHECKS_TOTAL
reason=post-config verify failed
STEOF
    echo "[A] FAILED: $VERIFY_ERRORS verify checks"
    exit "$VERIFY_ERRORS"
fi

echo "[A] write A.state + report section"
cat > /var/lib/smart-node/stages/A.state <<STEOF
status=done
timestamp=$(date -Iseconds)
version=configure-host@$SCRIPT_VERSION
checks_passed=$CHECKS_TOTAL/$CHECKS_TOTAL
STEOF
chmod 0644 /var/lib/smart-node/stages/A.state

# Append Layer A section to report (idempotent: replace any existing).
if grep -qFx '## Layer A -- configure-host' /etc/smart-node/install-report.md; then
    # Strip existing section before "## " of next section or EOF.
    awk '
        BEGIN { skip = 0 }
        /^## Layer A -- configure-host$/ { skip = 1; next }
        /^## / { if (skip) { skip = 0 } }
        skip == 0 { print }
    ' /etc/smart-node/install-report.md > /etc/smart-node/install-report.md.new
    mv /etc/smart-node/install-report.md.new /etc/smart-node/install-report.md
fi

{
    echo ""
    echo "## Layer A -- configure-host"
    echo ""
    echo "**Version:** configure-host@$SCRIPT_VERSION"
    echo "**Marked done:** $(date -Iseconds)"
    echo ""
    echo '```'
    echo "ssh user: $ADMIN_USER"
    echo "allow-from: $ALLOW_FROM (UFW activates moments after this report is written)"
    echo "sshd: PermitRootLogin=no, PasswordAuthentication=no"
    grep -E "^(PermitRootLogin|PasswordAuthentication|PubkeyAuthentication|UsePAM)" /etc/ssh/sshd_config
    echo ""
    echo "--- fail2ban ---"
    fail2ban-client status sshd | head -10
    echo ""
    echo "--- sysctl baseline ---"
    sysctl net.ipv4.tcp_syncookies kernel.kptr_restrict 2>&1
    echo ""
    echo "--- locale / tz ---"
    cat /etc/timezone
    grep ^LANG= /etc/default/locale 2>/dev/null || true
    echo ""
    echo "--- monitoring (A8) ---"
    echo "node-exporter: $(systemctl is-active prometheus-node-exporter 2>&1)"
    echo "listen: :9100 (UFW-scoped to $ALLOW_FROM)"
    if [[ -n "$NODE_NAME" ]]; then
        echo ""
        echo "--- node identity (A6) ---"
        echo "node FQDN:   $(hostname -f 2>/dev/null || hostname)"
        if [[ "$NO_VPN" == "1" ]]; then
            echo "vpn-server:  SKIPPED (--no-vpn) -- point your own DNS at this node"
        else
            echo "wg-node:     $(systemctl is-active wg-quick@wg-node 2>&1) -- 10.88.0.1/24 udp 51820"
            echo "node DNS:    $(systemctl is-active dnsmasq 2>&1) -- *.$NODE_FQDN -> 10.88.0.1"
            echo "phone QR:    /etc/wireguard/peers/phone1.png (endpoint $VPN_ENDPOINT)"
        fi
    fi
    echo '```'
} >> /etc/smart-node/install-report.md

# Late steps: A.state is written. From here on, killing the SSH session
# is non-fatal -- the durable state is on disk.

echo "[A] reloading sshd (live session survives SIGHUP)"
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

echo "[A] ufw (default deny + allow from $ALLOW_FROM to ports $SSH_PORT, 9100) -- session may drop now"
# Do NOT reset: preserve rules the host already holds (existing WireGuard
# tunnels, LXD forwarding, custom entries). Set defaults and add only the
# rules this installer requires. UFW silently ignores duplicate rules.
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow from "$ALLOW_FROM" to any port "$SSH_PORT" proto tcp >/dev/null
# A8: node_exporter scrape, restricted to the same trusted CIDR as SSH.
ufw allow from "$ALLOW_FROM" to any port 9100 proto tcp >/dev/null
if [[ -n "$NODE_NAME" && "$NO_VPN" != "1" ]]; then
    # A6: WireGuard handshake -- world-open, the onboarding phone connects
    # from any cellular IP (a test from b02msg caught the default deny here).
    ufw allow 51820/udp >/dev/null
    # A6: node DNS -- scoped to the wg-node interface so it is reachable
    # only over the VPN, never world-exposed.
    ufw allow in on wg-node to any port 53 >/dev/null
fi
# Background the enable so any RST from existing connection doesn't kill us
# mid-step. The bash will exit cleanly; ufw will activate ~0.5s later.
( sleep 1 && ufw --force enable >/dev/null 2>&1 ) &
disown
echo "[A] done"
BOOTSTRAP_EOF

# Substitute placeholders. Every value is validated above (IPv4/CIDR,
# unix username, internal version constant, DNS label, host:port) -- none
# can contain the sed delimiter '|' or a shell metacharacter. NODE_NAME
# and VPN_ENDPOINT are empty when --node-name was not given, which leaves
# the A6 block dormant (gated on a non-empty $NODE_NAME).
sed -i \
    -e "s|__ALLOW_FROM__|$ALLOW_FROM|g" \
    -e "s|__SCRIPT_VERSION__|$SCRIPT_VERSION|g" \
    -e "s|__SSH_USER__|$SSH_USER|g" \
    -e "s|__SSH_PORT__|$SSH_PORT|g" \
    -e "s|__NODE_NAME__|$NODE_NAME|g" \
    -e "s|__NODE_FQDN__|$NODE_FQDN|g" \
    -e "s|__NODE_FQDN_EXPLICIT__|$NODE_FQDN_EXPLICIT|g" \
    -e "s|__NO_VPN__|$NO_VPN|g" \
    -e "s|__VPN_ENDPOINT__|$VPN_ENDPOINT|g" \
    -e "s|__TIMEZONE__|$TIMEZONE|g" \
    "$BOOTSTRAP"

# -------- push + execute --------
log "Pushing bootstrap to $SSH_USER@$IP:/tmp/configure-host-bootstrap.sh"
scp "${SCP_OPTS[@]}" "$BOOTSTRAP" \
    "$SSH_USER@$IP:/tmp/configure-host-bootstrap.sh" >/dev/null

log "Executing on target (sudo bash; ~30-90s)"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "sudo -n bash /tmp/configure-host-bootstrap.sh"
# UFW activates a moment after bash returns; this cleanup ssh is best-effort.
ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "sudo -n rm -f /tmp/configure-host-bootstrap.sh" \
    >/dev/null 2>&1 || log "(cleanup ssh blocked by UFW -- /tmp file will be cleaned on next reboot)"

# -------- output --------
TIMESTAMP=$(date -Iseconds)

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
    cat <<JSONEOF
{
  "status": "ok",
  "stage": "A",
  "ip": "$IP",
  "ssh_user": "$SSH_USER",
  "ssh_port": $SSH_PORT,
  "ssh_key": "$SSH_KEY",
  "allow_from": "$ALLOW_FROM",
  "node_name": "$NODE_NAME",
  "node_fqdn": "$NODE_FQDN",
  "vpn_endpoint": "$VPN_ENDPOINT",
  "completed_at": "$TIMESTAMP",
  "script_version": "$SCRIPT_VERSION",
  "state_file": "/var/lib/smart-node/stages/A.state",
  "report": "/etc/smart-node/install-report.md"
}
JSONEOF
else
    if [[ -n "$NODE_NAME" ]]; then
        if [[ "$NO_VPN" -eq 1 ]]; then
            VPN_LINE="  Node FQDN:   $NODE_FQDN (A6 vpn-server SKIPPED -- --no-vpn)"
        else
            VPN_LINE="  Node FQDN:   $NODE_FQDN (A6 vpn-server)
  Phone QR:    /etc/wireguard/peers/phone1.png (endpoint $VPN_ENDPOINT)"
        fi
    else
        VPN_LINE="  VPN:         (A6 skipped -- no --node-name)"
    fi
    cat <<TXTEOF

[OK] Layer A complete
  IP:          $IP
  SSH user:    $SSH_USER
  SSH port:    $SSH_PORT
  Allow from:  $ALLOW_FROM
$VPN_LINE
  State:       /var/lib/smart-node/stages/A.state (status=done)
  Report:      /etc/smart-node/install-report.md
  Version:     $SCRIPT_VERSION ($TIMESTAMP)

Next: install/01-base.sh (Layer C stage 1)
TXTEOF
fi
