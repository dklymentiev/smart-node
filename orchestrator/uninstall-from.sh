#!/bin/bash
#
# uninstall-from.sh -- Reverse Layer C (Smart Node) on a host.
#
# Removes everything the installer recorded in the artifact manifest
# (/var/lib/smart-node/manifest.jsonl) -- Postgres clusters, Docker
# containers + networks, ufw rules, users, groups, dirs, files,
# optionally packages -- by replaying it in reverse order. Falls back to
# a fixed removal list when no manifest is present (pre-manifest nodes).
# The Smart Node roots (/var/lib/smart-node, /etc/smart-node) are always
# removed last. KEEPS Layer A hardening (sshd, ufw, fail2ban,
# unattended-upgrades) so the host stays in a defined-secure state.
#
# For a full host wipe, use provider-specific tools after this:
#   lxd-local: lxc delete --force NAME
#   contabo:   cntb delete instance ...
#   vultr:     vultr-cli instance delete ...
#
# Usage:
#   uninstall-from.sh --ip IP --ssh-user USER --ssh-key /path/private
#
# Optional:
#   --keep-packages    keep base apt packages installed (default)
#   --purge-packages   apt purge the packages the installer recorded
#   --json             print JSON result on success
#   --help             this message

set -euo pipefail

SCRIPT_VERSION="0.4.0"

IP=""
SSH_USER=""
SSH_KEY=""
KEEP_PACKAGES=1   # default: keep base packages (small, useful, low-risk)
JSON_OUTPUT=0

die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[$(date '+%H:%M:%S')] [uninstall] $*"; }
usage() { awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)             IP="$2"; shift 2 ;;
        --ssh-user)       SSH_USER="$2"; shift 2 ;;
        --ssh-key)        SSH_KEY="$2"; shift 2 ;;
        --keep-packages)  KEEP_PACKAGES=1; shift ;;
        --purge-packages) KEEP_PACKAGES=0; shift ;;
        --json)           JSON_OUTPUT=1; shift ;;
        --help|-h)        usage; exit 0 ;;
        *)                die "Unknown arg: $1 (use --help)" ;;
    esac
done

[[ -n "$IP" ]]       || die "--ip is required"
[[ -n "$SSH_USER" ]] || die "--ssh-user is required"
[[ -n "$SSH_KEY" ]]  || die "--ssh-key is required (private key)"
[[ -r "$SSH_KEY" ]]  || die "SSH key not readable: $SSH_KEY"

# TTY check: uninstall is irreversible and requires a human operator at the keyboard.
[ -t 0 ] || { echo "ERROR: --uninstall requires an interactive terminal." >&2; exit 1; }

SSH_OPTS=(
    -i "$SSH_KEY"
    -o "BatchMode=yes"
    -o "StrictHostKeyChecking=accept-new"
    -o "ConnectTimeout=10"
    -o "UserKnownHostsFile=/tmp/uninstall-from-$$"
)
trap 'rm -f /tmp/uninstall-from-$$' EXIT

log "Probing SSH at $SSH_USER@$IP"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "sudo -n true" \
    || die "SSH probe failed (NOPASSWD sudo not available, or host unreachable)"

# Detect the target node FQDN so the operator can confirm it exactly.
log "Detecting target node FQDN"
TARGET_FQDN=$(ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" \
    "hostname -f 2>/dev/null || hostname 2>/dev/null || echo '$IP'" 2>/dev/null \
    || echo "$IP")

echo ""
echo "About to PERMANENTLY uninstall Smart Node (Layer C) from:"
echo "  Host:     $IP"
echo "  FQDN:     $TARGET_FQDN"
echo ""
echo "Services that will be stopped and removed:"
echo "  All Smart Node systemd units recorded in the node manifest"
echo "  (smart-node-core-api, smart-node-dispatcher, smart-node-planner,"
echo "  smart-node-mesh, smart-node-mesh-mcp, traefik, and others)."
echo ""
echo "Databases that will be dumped then dropped:"
echo "  All non-system databases in the Smart Node PostgreSQL cluster"
echo "  (typically: mesh, planner, doci -- then the entire cluster is dropped)."
echo "  Dumps will be written to /opt/smart-node/backups/ on the target host."
echo ""
echo "Also removed: /opt/smart-node, /var/lib/smart-node, /etc/smart-node."
echo "Layer A hardening (sshd, ufw, fail2ban) will be kept."
echo ""

# Confirmation: operator must type the target node FQDN exactly.
read -r -p "Type the node FQDN to confirm deletion: " CONFIRM
NODE_FQDN="$TARGET_FQDN"
if [ "$CONFIRM" != "$NODE_FQDN" ]; then
    echo "Aborted: input did not match node FQDN ($NODE_FQDN)." >&2
    exit 1
fi

UNINSTALL_BOOTSTRAP=$(mktemp /tmp/uninstall-bootstrap.XXXXXX.sh)
trap 'rm -f "$UNINSTALL_BOOTSTRAP" /tmp/uninstall-from-$$' EXIT

cat > "$UNINSTALL_BOOTSTRAP" <<'BOOTSTRAP_EOF'
#!/bin/bash
# Runs INSIDE the target host as root via sudo. Idempotent.
# Arg 1: KEEP_PACKAGES (1 = keep apt packages, 0 = purge recorded ones).
set -euo pipefail

KEEP_PACKAGES="${1:-1}"
SMART_NODE_USER="smart-node"
SMART_NODE_GROUP="smart-node"
MANIFEST="/var/lib/smart-node/manifest.jsonl"

echo "[U] checking current state"
HAS_USER=0; HAS_VAR=0; HAS_ETC=0; HAS_LIB=0; HAS_LOG=0; HAS_OPT=0; HAS_MAN=0
id -u "$SMART_NODE_USER" >/dev/null 2>&1 && HAS_USER=1
[[ -d /var/lib/smart-node       ]] && HAS_VAR=1
[[ -d /etc/smart-node           ]] && HAS_ETC=1
[[ -d /usr/local/lib/smart-node ]] && HAS_LIB=1
[[ -d /var/log/smart-node       ]] && HAS_LOG=1
[[ -d /opt/smart-node           ]] && HAS_OPT=1
[[ -f "$MANIFEST"               ]] && HAS_MAN=1
echo "[U] before: user=$HAS_USER var=$HAS_VAR etc=$HAS_ETC lib=$HAS_LIB log=$HAS_LOG opt=$HAS_OPT manifest=$HAS_MAN"

if (( HAS_MAN )) && command -v jq >/dev/null 2>&1; then
    echo "[U] manifest found -- replaying in reverse"

    # Dump all non-system databases before dropping the cluster(s).
    # If /opt/smart-node/backups/ is missing or not writable, exit without dropping.
    BACKUP_DIR="/opt/smart-node/backups"
    DUMP_STAMP=$(date +%Y%m%d-%H%M%S)
    if jq -r 'select(.type=="pg-cluster")|.value' "$MANIFEST" 2>/dev/null | grep -q .; then
        if [[ ! -d "$BACKUP_DIR" ]]; then
            echo "[U] ERROR: $BACKUP_DIR does not exist -- create it before uninstalling." >&2
            exit 1
        fi
        if [[ ! -w "$BACKUP_DIR" ]]; then
            echo "[U] ERROR: $BACKUP_DIR is not writable." >&2
            exit 1
        fi
        echo "[U] dumping databases to $BACKUP_DIR before cluster drop"
        DB_NAMES=$(runuser -u postgres -- psql -tAc \
            "SELECT datname FROM pg_database WHERE datistemplate = false AND datname NOT IN ('postgres')" \
            2>/dev/null) || {
            echo "[U] ERROR: psql could not list databases -- will not proceed to drop." >&2
            exit 1
        }
        while IFS= read -r dbname; do
            [[ -n "$dbname" ]] || continue
            dump_file="$BACKUP_DIR/uninstall-${dbname}-${DUMP_STAMP}.dump"
            echo "[U]   dumping $dbname -> $dump_file"
            if ! runuser -u postgres -- pg_dump "$dbname" -Fc > "$dump_file" 2>/dev/null; then
                echo "[U] ERROR: pg_dump of $dbname failed -- will not proceed to drop." >&2
                exit 1
            fi
            if [[ ! -s "$dump_file" ]]; then
                echo "[U] ERROR: dump of $dbname is empty -- will not proceed to drop." >&2
                exit 1
            fi
            echo "[U]   dump written: $dump_file"
        done <<< "$DB_NAMES"
        echo "[U] database dump complete"
    fi

    # PostgreSQL clusters first: pg_dropcluster --stop stops the cluster
    # and removes its config + data directory in one step. Must run
    # BEFORE the dir sweep (the data dir lives under a recorded dir) and
    # before any package purge (pg_dropcluster comes from
    # postgresql-common). A cluster cannot be undone with rm.
    while read -r cl; do
        [[ -n "$cl" ]] || continue
        cl_ver="${cl%/*}"; cl_name="${cl#*/}"
        if command -v pg_dropcluster >/dev/null 2>&1 \
                && pg_lsclusters --no-header "$cl_ver" "$cl_name" >/dev/null 2>&1; then
            if pg_dropcluster --stop "$cl_ver" "$cl_name" 2>/dev/null; then
                echo "[U]   rm pg-cluster $cl"
            else
                echo "[U]   WARN: pg_dropcluster $cl failed" >&2
            fi
        fi
    done < <(jq -r 'select(.type=="pg-cluster")|.value' "$MANIFEST")

    # Docker containers: remove before the network (a network with
    # attached containers cannot be removed) and before any package purge
    # (purging docker-ce stops the daemon -> docker commands fail). Like
    # the jq-driven replay, this must finish before the package purge.
    while read -r ct; do
        [[ -n "$ct" ]] || continue
        if command -v docker >/dev/null 2>&1; then
            if docker rm -f "$ct" >/dev/null 2>&1; then
                echo "[U]   rm docker-container $ct"
            else
                echo "[U]   WARN: docker rm $ct failed (already gone?)" >&2
            fi
        fi
    done < <(jq -r 'select(.type=="docker-container")|.value' "$MANIFEST")

    # Docker networks: after their containers are gone.
    while read -r net; do
        [[ -n "$net" ]] || continue
        if command -v docker >/dev/null 2>&1; then
            if docker network rm "$net" >/dev/null 2>&1; then
                echo "[U]   rm docker-network $net"
            else
                echo "[U]   WARN: docker network rm $net failed" >&2
            fi
        fi
    done < <(jq -r 'select(.type=="docker-network")|.value' "$MANIFEST")

    # ufw rules opened by Layer C (B03 opens 80/443). Layer A only
    # guarantees SSH, so these are Layer-C artifacts to reverse. ufw
    # delete is a no-op if the rule is already absent.
    while read -r rule; do
        [[ -n "$rule" ]] || continue
        if command -v ufw >/dev/null 2>&1; then
            ufw --force delete allow "$rule" >/dev/null 2>&1 || true
            echo "[U]   rm ufw-rule allow $rule"
        fi
    done < <(jq -r 'select(.type=="ufw-rule")|.value' "$MANIFEST")

    # Files first (before the dirs that contain them).
    while read -r f; do
        [[ -n "$f" ]] || continue
        if [[ -e "$f" ]]; then rm -f "$f"; echo "[U]   rm file  $f"; fi
    done < <(jq -r 'select(.type=="file")|.value' "$MANIFEST")

    # systemd units: disable + remove unit files.
    while read -r unit; do
        [[ -n "$unit" ]] || continue
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$unit" "/lib/systemd/system/$unit" 2>/dev/null || true
        echo "[U]   rm unit  $unit"
    done < <(jq -r 'select(.type=="systemd-unit")|.value' "$MANIFEST")
    systemctl daemon-reload >/dev/null 2>&1 || true

    # Dirs: longest path first so children are removed before parents.
    while read -r d; do
        [[ -n "$d" ]] || continue
        if [[ -d "$d" ]]; then rm -rf "$d"; echo "[U]   rm dir   $d"; fi
    done < <(jq -r 'select(.type=="dir")|.value' "$MANIFEST" \
                | awk '{print length"\t"$0}' | sort -rn | cut -f2-)

    # Users: kill their processes, then userdel.
    while read -r usr; do
        [[ -n "$usr" ]] || continue
        if id -u "$usr" >/dev/null 2>&1; then
            pkill -u "$usr" 2>/dev/null || true
            sleep 0.3
            userdel "$usr" 2>/dev/null || true
            echo "[U]   rm user  $usr"
        fi
    done < <(jq -r 'select(.type=="user")|.value' "$MANIFEST")

    # Groups: after their member users are gone, and BEFORE the package
    # purge -- the purge can remove jq itself, which every manifest query
    # here depends on. All jq-driven replay must finish before the purge.
    while read -r grp; do
        [[ -n "$grp" ]] || continue
        if getent group "$grp" >/dev/null 2>&1; then
            groupdel "$grp" 2>/dev/null || true
            echo "[U]   rm group $grp"
        fi
    done < <(jq -r 'select(.type=="group")|.value' "$MANIFEST")

    # Packages last: only when explicitly asked (--purge-packages). Runs
    # after all jq-driven manifest replay above, since the purge may
    # remove jq itself (it is a recorded B01 package).
    if [[ "$KEEP_PACKAGES" != "1" ]]; then
        mapfile -t PKGS < <(jq -r 'select(.type=="package")|.value' "$MANIFEST")
        if (( ${#PKGS[@]} > 0 )); then
            echo "[U]   apt purge: ${PKGS[*]}"
            DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "${PKGS[@]}" >/dev/null 2>&1 || true
            DEBIAN_FRONTEND=noninteractive apt-get autoremove -y -qq >/dev/null 2>&1 || true
        fi
    else
        echo "[U]   packages kept (--keep-packages)"
    fi
else
    echo "[U] no manifest (or jq absent) -- fallback fixed removal"
    if (( HAS_USER )); then
        pkill -u "$SMART_NODE_USER" 2>/dev/null || true
        sleep 1
    fi
    rm -rf /usr/local/lib/smart-node /var/log/smart-node /opt/smart-node
    rm -f /etc/cron.d/smart-node-* 2>/dev/null || true
    userdel "$SMART_NODE_USER" 2>/dev/null || true
    groupdel "$SMART_NODE_GROUP" 2>/dev/null || true
fi

# Smart Node roots: created by Layer A as Layer-C scaffolding, never
# recorded in the manifest. Always removed last -- this also deletes the
# manifest itself, which lives under /var/lib/smart-node.
echo "[U] removing Smart Node roots"
rm -rf /var/lib/smart-node /etc/smart-node

echo "[U] verifying clean"
VERIFY_ERRORS=0
chk() { "$@" >/dev/null 2>&1 && { echo "[VERIFY-FAIL] still present: $*" >&2; VERIFY_ERRORS=$((VERIFY_ERRORS + 1)); } || true; }
chk id -u "$SMART_NODE_USER"
chk getent group "$SMART_NODE_GROUP"
chk test -d /var/lib/smart-node
chk test -d /etc/smart-node
chk test -d /usr/local/lib/smart-node
chk test -d /var/log/smart-node
chk test -d /opt/smart-node
chk bash -c 'ls /etc/cron.d/smart-node-* 2>/dev/null'
chk bash -c 'command -v pg_lsclusters >/dev/null 2>&1 && pg_lsclusters --no-header 2>/dev/null | grep -qw smartnode'
chk bash -c 'command -v docker >/dev/null 2>&1 && docker ps -a --format "{{.Names}}" 2>/dev/null | grep -qx smart-node-traefik'
chk bash -c 'command -v docker >/dev/null 2>&1 && docker network ls --format "{{.Name}}" 2>/dev/null | grep -qx smart-node'

# Sanity: Layer A artifacts MUST survive.
keep_chk() { "$@" >/dev/null 2>&1 || { echo "[KEEP-FAIL] expected but absent: $*" >&2; VERIFY_ERRORS=$((VERIFY_ERRORS + 1)); }; }
keep_chk getent passwd root
keep_chk getent passwd "$(stat -c '%U' /home 2>/dev/null || echo admin)"   # admin user (or whoever sudoed)
keep_chk grep -q '^PermitRootLogin no' /etc/ssh/sshd_config
keep_chk test -f /etc/fail2ban/jail.d/sshd.local

if (( VERIFY_ERRORS > 0 )); then
    echo "[U] FAILED ($VERIFY_ERRORS issues)" >&2
    exit "$VERIFY_ERRORS"
fi

echo "[U] done -- Layer C removed, Layer A intact"
BOOTSTRAP_EOF

log "Pushing uninstall bootstrap"
scp "${SSH_OPTS[@]}" "$UNINSTALL_BOOTSTRAP" \
    "$SSH_USER@$IP:/tmp/uninstall-bootstrap.sh" >/dev/null

log "Executing on target (keep_packages=$KEEP_PACKAGES)"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "sudo -n bash /tmp/uninstall-bootstrap.sh $KEEP_PACKAGES"
ssh "${SSH_OPTS[@]}" "$SSH_USER@$IP" "sudo -n rm -f /tmp/uninstall-bootstrap.sh" >/dev/null 2>&1 || true

TIMESTAMP=$(date -Iseconds)
if [[ "$KEEP_PACKAGES" -eq 1 ]]; then PKG_DISPOSITION="kept"; else PKG_DISPOSITION="purged"; fi

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
    cat <<JSONEOF
{
  "status": "ok",
  "stage": "uninstall",
  "ip": "$IP",
  "ssh_user": "$SSH_USER",
  "method": "manifest-driven (fallback: fixed list when no manifest)",
  "removed": ["recorded artifacts (users/groups/dirs/files)", "/var/lib/smart-node", "/etc/smart-node"],
  "packages": "$PKG_DISPOSITION",
  "kept": ["Layer A hardening (sshd/ufw/fail2ban/unattended-upgrades)"],
  "completed_at": "$TIMESTAMP",
  "script_version": "$SCRIPT_VERSION"
}
JSONEOF
else
    cat <<TXTEOF

==========================================================================
[OK] Smart Node Layer C uninstalled
==========================================================================
  IP:       $IP
  Method:   manifest-driven (fallback: fixed list when no manifest)
  Removed:  recorded artifacts (users/groups/dirs/files),
            /var/lib/smart-node, /etc/smart-node
  Packages: $PKG_DISPOSITION
  Kept:     Layer A hardening (sshd, ufw, fail2ban, unattended-upgrades)

The host is still hardened and SSH-reachable. To wipe completely:
  lxd-local:  lxc delete --force NAME
  contabo:    cntb delete instance ...
  vultr:      vultr-cli instance delete ...
TXTEOF
fi
