#!/usr/bin/env bash
#
# build-stand-golden.sh -- build a golden stand image for tier-2 and tier-3
# test cycles.
#
# A golden image is an LXD image snapshot taken AFTER Layer A and B01-B06
# bootstrap are complete, and optionally after docker images have been
# pre-pulled from the registry. Starting a test container from the golden
# image skips the bootstrap layer entirely.
#
# WHAT IT ACTUALLY SAVES, measured on example-node 2026-09-05 rather than estimated:
#
#   full path       provision + install-on --all          ~23 min
#   from golden     provision 9s + --companions-only      ~15 min
#
# So it removes the bootstrap, which is what it claims to remove, and no more.
# This header used to promise ~5 minutes; that is not reachable this way,
# because what dominates the remaining time is BUILDING the companion docker
# images on the container. Getting to five minutes requires pre-pulling them with
# --pull-images; that is the tier-1 target's actual dependency.
#
# Runs from the ORCHESTRATOR machine (your machine), SSHing to a temporary
# build container on the local LXD host.
#
# WHAT IT DOES:
#
#   Step 1  Provision a fresh LXD container from the base Debian image.
#   Step 2  Run install-on.sh --bootstrap-only (Layer A + B01-B06).
#   Step 3  (Optional) Pre-pull docker images from the registry.
#   Step 4  Stop the container, snapshot it, publish as LXD image.
#   Step 5  Clean up the build container (left on failure for inspection).
#
# Usage:
#   provision/build-stand-golden.sh --ip IP --ssh-key KEY [OPTIONS]
#
# Required:
#   --ip IP             IP for the temporary build container
#   --ssh-key PATH      path to the SSH PRIVATE key (not the .pub)
#
# Optional:
#   --ssh-pub PATH      path to the SSH PUBLIC key (default: KEY.pub)
#   --name NAME         container name (default: sn-golden-build)
#   --alias ALIAS       LXD image alias to publish (default: sn-golden-<hash>[-<version>])
#   --release VERSION   release version to embed in the alias (e.g. 1.12.24)
#   --git-ref REF       git ref to record; defaults to HEAD of this repo
#   --allow-from CIDR   SSH inbound CIDR for Layer A (default: derived from controller)
#   --node-fqdn FQDN    FQDN for the build node (default: sn-golden-build.local)
#   --pull-images       pre-pull companion docker images from the registry
#   --network BRIDGE    LXD bridge name (default: lxdbr0)
#   --storage-pool NAME LXD storage pool (default: default)
#   --ssh-port PORT     SSH port (default: 22)
#   --keep-container    do not delete the build container on success
#   --json              print JSON result on success
#   --help              this usage text
#
# Exit codes:
#   0   golden image built and published
#   1   build failed (partial state left for inspection if --keep-container)
#   2   prerequisite missing (lxc, SSH key, required arg)
#
# REBUILDING
#   A golden image goes stale when bootstrap layers or image versions change.
#   The alias must be updated in your test orchestration when you rebuild.
#   A safe workflow: build with a new --alias, update the test config to point
#   to it, then delete the old alias.
#
# EVIDENCE
#   The published alias is printed to stdout and included in the JSON result.
#   Record it in your acceptance evidence file so every test run is linked to
#   the exact golden image it used. A test run without a recorded golden alias
#   is evidence that a particular starting state was used -- not what that state
#   was.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ------------------------------------------------------------------ defaults
IP=""
SSH_KEY=""
SSH_PUB=""
NAME="sn-golden-build"
ALIAS=""
RELEASE_VERSION=""
GIT_REF=""
ALLOW_FROM=""
NODE_FQDN="sn-golden-build.local"
PULL_IMAGES=0
NETWORK="lxdbr0"
STORAGE_POOL="default"
SSH_PORT=22
KEEP_CONTAINER=0
JSON_OUTPUT=0

# --------------------------------------------------------------- arg parsing
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)           IP="$2";              shift 2 ;;
        --ssh-key)      SSH_KEY="$2";         shift 2 ;;
        --ssh-pub)      SSH_PUB="$2";         shift 2 ;;
        --name)         NAME="$2";            shift 2 ;;
        --alias)        ALIAS="$2";           shift 2 ;;
        --release)      RELEASE_VERSION="$2"; shift 2 ;;
        --git-ref)      GIT_REF="$2";         shift 2 ;;
        --allow-from)   ALLOW_FROM="$2";      shift 2 ;;
        --node-fqdn)    NODE_FQDN="$2";       shift 2 ;;
        --pull-images)  PULL_IMAGES=1;        shift ;;
        --network)      NETWORK="$2";         shift 2 ;;
        --storage-pool) STORAGE_POOL="$2";    shift 2 ;;
        --ssh-port)     SSH_PORT="$2";        shift 2 ;;
        --keep-container) KEEP_CONTAINER=1;   shift ;;
        --json)         JSON_OUTPUT=1;        shift ;;
        --help|-h)
            sed -n '2,57p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "[golden] unknown argument: $1 (use --help)" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------- validation
[[ -n "$IP" ]]      || { echo "[golden] --ip is required" >&2;      exit 2; }
[[ -n "$SSH_KEY" ]] || { echo "[golden] --ssh-key is required" >&2; exit 2; }
[[ -f "$SSH_KEY" ]] || { echo "[golden] ssh key not found: $SSH_KEY" >&2; exit 2; }

[[ -n "$SSH_PUB" ]] || SSH_PUB="${SSH_KEY}.pub"
[[ -f "$SSH_PUB" ]] || { echo "[golden] public key not found: $SSH_PUB" >&2; exit 2; }

command -v lxc >/dev/null 2>&1 || { echo "[golden] lxc not found -- install LXD" >&2; exit 2; }

# Determine git ref for alias and evidence.
if [[ -z "$GIT_REF" ]]; then
    GIT_REF="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
fi

# Build the alias if not given.
if [[ -z "$ALIAS" ]]; then
    if [[ -n "$RELEASE_VERSION" ]]; then
        ALIAS="sn-golden-${RELEASE_VERSION}-${GIT_REF}"
    else
        ALIAS="sn-golden-${GIT_REF}"
    fi
fi

TS="$(date +%Y%m%d-%H%M%S)"
PROVISION_SCRIPT="${REPO_ROOT}/provision/provision-lxd-local.sh"
INSTALL_SCRIPT="${REPO_ROOT}/orchestrator/install-on.sh"
LOG_DIR="/tmp/sn-golden-${NAME}-${TS}"
mkdir -p "$LOG_DIR"

echo "[golden] building stand image: alias=${ALIAS}, git=${GIT_REF}"
echo "[golden] build log directory: ${LOG_DIR}"
echo

# ---------------------------------------------------------------- cleanup trap
CONTAINER_CREATED=0
cleanup() {
    local rc=$?
    if (( CONTAINER_CREATED )) && ! (( KEEP_CONTAINER )); then
        if (( rc != 0 )); then
            echo "[golden] build failed (exit ${rc}) -- leaving container '${NAME}' for inspection" >&2
            echo "[golden] delete it when done:  lxc delete --force '${NAME}'" >&2
        else
            echo "[golden] cleaning up build container '${NAME}'"
            lxc delete --force "$NAME" 2>/dev/null || true
        fi
    elif (( CONTAINER_CREATED )) && (( KEEP_CONTAINER )); then
        echo "[golden] --keep-container: container '${NAME}' left running"
    fi
}
trap cleanup EXIT

# ================================================================ Step 1: Provision
echo "[golden] Step 1: provisioning build container '${NAME}' at ${IP}"

PROVISION_LOG="${LOG_DIR}/provision.log"
PROVISION_ARGS=(
    "$PROVISION_SCRIPT"
    "--name"    "$NAME"
    "--ip"      "$IP"
    "--ssh-key" "$SSH_PUB"
    "--network" "$NETWORK"
    "--storage-pool" "$STORAGE_POOL"
    "--recreate"
)
if ! "${PROVISION_ARGS[@]}" 2>&1 | tee "$PROVISION_LOG"; then
    echo "[golden] Step 1 FAILED: provision-lxd-local.sh returned non-zero" >&2
    echo "[golden] see ${PROVISION_LOG}" >&2
    exit 1
fi
CONTAINER_CREATED=1
echo "[golden] Step 1 OK: container '${NAME}' running at ${IP}"
echo

# Brief settle for SSH.
sleep 5

# ================================================================ Step 2: Bootstrap
echo "[golden] Step 2: running bootstrap (Layer A + B01-B06) via install-on.sh --bootstrap-only"

BOOTSTRAP_LOG="${LOG_DIR}/bootstrap.log"
INSTALL_ARGS=(
    "$INSTALL_SCRIPT"
    # Step 1 above ALREADY created this container. Asking install-on.sh for the
    # lxd-local provider here makes it provision a second time and stop on
    # "Container already exists", which is why this builder has never produced
    # an image. From here the container is an existing target.
    "--provider" "existing"
    "--name" "$NAME"
    "--ip" "$IP"
    "--ssh-user" "admin"
    "--ssh-key" "$SSH_KEY"
    "--ssh-port" "$SSH_PORT"
    "--node-fqdn" "$NODE_FQDN"
    "--no-vpn"
    "--bootstrap-only"
    "--setup-via-token"
)
[[ -n "$ALLOW_FROM" ]] && INSTALL_ARGS+=("--allow-from" "$ALLOW_FROM")

if ! "${INSTALL_ARGS[@]}" 2>&1 | tee "$BOOTSTRAP_LOG"; then
    echo "[golden] Step 2 FAILED: install-on.sh --bootstrap-only returned non-zero" >&2
    echo "[golden] see ${BOOTSTRAP_LOG}" >&2
    exit 1
fi
echo "[golden] Step 2 OK: bootstrap complete"
echo

# ================================================================ Step 3: Pre-pull images (optional)
if (( PULL_IMAGES )); then
    echo "[golden] Step 3: pre-pulling companion docker images from registry"
    # At bootstrap-only stage, no companion has run yet, so no docker-compose.yml
    # files exist under /opt/smart-node or /etc/smart-node. This step is only
    # effective when --pull-images is used AFTER the companions have pre-staged
    # their compose files, which does not happen during bootstrap-only. In practice
    # this step is a no-op until the golden build workflow is extended to stage
    # compose files before snapshot. It is kept here as the hook for that work.
    PRE_PULL_LOG="${LOG_DIR}/pre-pull.log"
    SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -p "$SSH_PORT" -i "$SSH_KEY")
    PULL_SCRIPT='
#!/usr/bin/env bash
set -uo pipefail
echo "[golden] pre-pull: looking for docker-compose.yml files with ghcr.io image references"
FOUND=0
find /opt/smart-node /etc/smart-node -name "docker-compose.yml" 2>/dev/null | while read -r cf; do
    FOUND=1
    while IFS= read -r img; do
        [[ "$img" == ghcr.io/* ]] || continue
        echo "[golden] pre-pull: docker pull $img"
        docker pull "$img" 2>&1 || echo "[golden] pre-pull: WARNING -- could not pull $img (skipping)"
    done < <(grep -oE "image: (ghcr\.io/[^[:space:]]+)" "$cf" 2>/dev/null | awk "{print \$2}" || true)
done
[[ "$FOUND" -eq 0 ]] && echo "[golden] pre-pull: no docker-compose.yml found (bootstrap-only stage -- companions have not run yet)"
echo "[golden] pre-pull: done"
'
    if ! ssh "${SSH_OPTS[@]}" "admin@${IP}" "sudo bash -s" <<< "$PULL_SCRIPT" 2>&1 | tee "$PRE_PULL_LOG"; then
        echo "[golden] Step 3 WARNING: pre-pull script returned non-zero (non-fatal)" >&2
    fi
    echo "[golden] Step 3 done (see ${PRE_PULL_LOG})"
    echo
else
    echo "[golden] Step 3: pre-pull images -- SKIPPED (pass --pull-images to enable)"
    echo
fi

# ================================================================ Step 3b: Strip SSH host keys
# Host keys must be removed from the container before snapshotting. Every stand
# started from the same golden image would otherwise share identical host keys,
# which is a security problem and breaks SSH known_hosts across stands.
# provision-lxd-local.sh --from-golden regenerates them with ssh-keygen -A on
# first start, so each stand gets its own unique keys.
echo "[golden] Step 3b: stripping SSH host keys from build container"
if ! lxc exec "$NAME" -- bash -c 'rm -f /etc/ssh/ssh_host_*; echo "[golden] host keys removed"'; then
    echo "[golden] Step 3b FAILED: could not remove SSH host keys" >&2
    exit 1
fi
echo "[golden] Step 3b OK: host keys stripped"
echo

# ================================================================ Step 4: Snapshot and publish
echo "[golden] Step 4: stopping container, snapshotting, and publishing as '${ALIAS}'"

# Stop the container cleanly before snapshotting.
if ! lxc stop "$NAME" 2>&1; then
    echo "[golden] Step 4 FAILED: could not stop container '${NAME}'" >&2
    exit 1
fi
echo "[golden] container stopped"

SNAPSHOT_NAME="golden-${GIT_REF}"
if ! lxc snapshot "$NAME" "$SNAPSHOT_NAME" 2>&1; then
    echo "[golden] Step 4 FAILED: could not snapshot '${NAME}'" >&2
    exit 1
fi
echo "[golden] snapshot '${SNAPSHOT_NAME}' created"

# Delete existing alias if present (so the publish succeeds).
if lxc image info "$ALIAS" >/dev/null 2>&1; then
    echo "[golden] removing existing image alias '${ALIAS}'"
    lxc image delete "$ALIAS" 2>/dev/null || true
fi

PUBLISH_LOG="${LOG_DIR}/publish.log"
PUBLISH_DESC="Smart Node golden stand image -- git=${GIT_REF}"
[[ -n "$RELEASE_VERSION" ]] && PUBLISH_DESC="${PUBLISH_DESC} release=${RELEASE_VERSION}"
PUBLISH_DESC="${PUBLISH_DESC} built=${TS}"

# LXD image properties make the git commit and build metadata readable by
# provision-lxd-local.sh --from-golden without parsing the description string.
PUBLISH_PROPS=(
    "description=${PUBLISH_DESC}"
    "user.git_commit=${GIT_REF}"
    "user.built_at=${TS}"
)
[[ -n "$RELEASE_VERSION" ]] && PUBLISH_PROPS+=("user.release_version=${RELEASE_VERSION}")
[[ -n "$NODE_FQDN" ]] && PUBLISH_PROPS+=("user.node_fqdn=${NODE_FQDN}")

if ! lxc publish "${NAME}/${SNAPSHOT_NAME}" \
        --alias "$ALIAS" \
        "${PUBLISH_PROPS[@]}" \
        2>&1 | tee "$PUBLISH_LOG"; then
    echo "[golden] Step 4 FAILED: could not publish image" >&2
    echo "[golden] see ${PUBLISH_LOG}" >&2
    exit 1
fi
echo "[golden] Step 4 OK: image published as '${ALIAS}'"
echo

# ================================================================ Step 5: Report
echo "[golden] Step 5: report"
echo

if (( JSON_OUTPUT )); then
    python3 -c "
import json, sys
print(json.dumps({
  'status': 'ok',
  'alias': sys.argv[1],
  'git_ref': sys.argv[2],
  'release_version': sys.argv[3] or None,
  'built_at': sys.argv[4],
  'log_dir': sys.argv[5],
}, indent=2))
" "$ALIAS" "$GIT_REF" "$RELEASE_VERSION" "$TS" "$LOG_DIR"
else
    echo "[golden] DONE"
    echo "  alias:     ${ALIAS}"
    echo "  git_ref:   ${GIT_REF}"
    [[ -n "$RELEASE_VERSION" ]] && echo "  release:   ${RELEASE_VERSION}"
    echo "  built_at:  ${TS}"
    echo "  logs:      ${LOG_DIR}"
    echo
    echo "Use this alias with:"
    echo "  provision/provision-lxd-local.sh --from-golden '${ALIAS}' --name sn-XX --ip 10.0.100.X --ssh-key <path>"
    echo "Then run install-on.sh with --companions-only to install companions on the pre-bootstrapped container."
fi

exit 0
