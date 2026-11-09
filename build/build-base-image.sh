#!/bin/bash
#
# build-base-image.sh -- build the smart-node-base LXD image.
#
# Produces a publishable LXD image with the entire Smart Node system
# layer pre-installed: apt packages (PostgreSQL 16 + pgvector, Docker CE,
# WireGuard, fail2ban, ufw, age, sops, python3 toolchain, jq, git, cron,
# node-exporter) plus the docker daemon ready to run, plus security
# nesting enabled.
#
# Output: LXD image `smart-node-base/<VERSION>` in the local image
# store, optionally exported to a tarball.
#
# Usage:
#   build-base-image.sh --version 0.1.0
#
# Optional:
#   --image SRC     Source LXD image (default: images:debian/12)
#   --export DIR    After publish, lxc image export -> DIR/smart-node-base-<ver>.tar.gz
#   --keep-temp     Don't delete the temporary build container on success
#
# The system layer
# inside the image is byte-equivalent to what /tmp/test-node-system-layer.sh
# produces on a fresh LXC (the script that turned a 43-min install into
# 1m21s on test-node once images were also cached).
#
# DOES NOT bake Docker images. Companions install natively from the
# vendored source, and the few containers a node runs pull their images at
# install time. Not baking them avoids the prior 6 GB / 2h+2h disaster of a
# one-tarball-with-everything approach. Expected base image size: ~1.5-2 GB.
#
# Exits 0 on success, non-zero with an error message otherwise.

set -euo pipefail

# -------- defaults --------
VERSION=""
SRC_IMAGE="images:debian/12"
EXPORT_DIR=""
KEEP_TEMP=0
TEMP_NAME="snbase-build-$(date +%s)"

# -------- helpers --------
die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[$(date '+%H:%M:%S')] $*"; }

usage() {
    awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
}

# -------- arg parse --------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)    VERSION="$2"; shift 2 ;;
        --image)      SRC_IMAGE="$2"; shift 2 ;;
        --export)     EXPORT_DIR="$2"; shift 2 ;;
        --keep-temp)  KEEP_TEMP=1; shift ;;
        --help|-h)    usage; exit 0 ;;
        *)            die "unknown arg: $1 (use --help)" ;;
    esac
done

[[ -n "$VERSION" ]] || die "--version is required (e.g. --version 0.1.0)"
command -v lxc >/dev/null 2>&1 || die "lxc CLI not found"

# -------- system-layer script (inlined) --------
# This MUST stay in sync with /tmp/test-node-system-layer.sh that built test-node.
# Any drift here means the published base image and our test loop diverge.
SYSTEM_LAYER_SCRIPT=$(cat <<'INNER'
#!/bin/bash
set -e
export DEBIAN_FRONTEND=noninteractive

echo "[base] apt update"
apt-get update -qq

echo "[base] base system packages"
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl wget gpg sudo openssh-server locales \
  ufw fail2ban unattended-upgrades \
  prometheus-node-exporter \
  wireguard-tools dnsmasq qrencode age \
  jq git python3 python3-venv python3-pip rsync cron \
  vim htop

echo "[base] PGDG + postgresql-16 + pgvector"
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg
echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update -qq
apt-get install -y -qq postgresql-common postgresql-16 postgresql-16-pgvector

# Default 16/main cluster is fine to ship inside the base image -- it
# starts on port 5432. B02-persistence.sh drops it (its probe_before
# loosening) when smartnode cluster needs the port. Keeping it removes
# initdb time from per-install cost (the cluster files already exist).

echo "[base] Docker CE + plugins"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

echo "[base] sops binary"
curl -fsSL https://github.com/getsops/sops/releases/download/v3.9.4/sops-v3.9.4.linux.amd64 \
  -o /usr/local/bin/sops
chmod +x /usr/local/bin/sops

echo "[base] done"
INNER
)

# -------- 1. launch build container --------
log "launching temp build container $TEMP_NAME from $SRC_IMAGE"
lxc launch "$SRC_IMAGE" "$TEMP_NAME" >/dev/null
# Docker-in-LXC needs nesting. Bake it into the published image.
lxc config set "$TEMP_NAME" security.nesting true
lxc restart "$TEMP_NAME" >/dev/null
log "waiting for container network"
for i in {1..30}; do
    if lxc exec "$TEMP_NAME" -- true 2>/dev/null; then break; fi
    sleep 2
done
sleep 5  # give cloud-init / network a moment

# -------- 2. push and run system-layer --------
log "pushing system-layer.sh"
echo "$SYSTEM_LAYER_SCRIPT" | lxc exec "$TEMP_NAME" -- tee /root/system-layer.sh >/dev/null

log "running system-layer.sh (apt + postgres + docker + sops, ~5 min)"
lxc exec "$TEMP_NAME" -- bash /root/system-layer.sh

# -------- 3. stop + publish --------
log "stopping container"
lxc stop "$TEMP_NAME"

ALIAS="smart-node-base/$VERSION"
log "publishing image as $ALIAS"
lxc publish "$TEMP_NAME" --alias "$ALIAS" \
    description="Smart Node base image (system layer pre-installed). System packages + postgres-16 + docker-ce + sops + nesting. No companion Docker images -- they pull from registry at install-time." \
    >/dev/null

log "image published; fingerprint:"
lxc image list "$ALIAS" --format csv -c f,a,t,s

# -------- 4. optional export to tarball --------
if [[ -n "$EXPORT_DIR" ]]; then
    mkdir -p "$EXPORT_DIR"
    OUT="$EXPORT_DIR/smart-node-base-$VERSION"
    log "exporting tarball -> $OUT.tar.gz"
    lxc image export "$ALIAS" "$OUT" >/dev/null
    log "tarball: $(ls -lh ${OUT}.tar.gz | awk '{print $5}')"
fi

# -------- 5. cleanup --------
if [[ $KEEP_TEMP -eq 0 ]]; then
    log "deleting temp container $TEMP_NAME"
    lxc delete "$TEMP_NAME"
else
    log "temp container $TEMP_NAME kept (--keep-temp)"
fi

log "DONE -- usage: lxc launch $ALIAS <node-name>"
