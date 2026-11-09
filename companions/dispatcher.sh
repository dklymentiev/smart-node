#!/bin/bash
#
# dispatcher.sh -- Smart Node installer, Layer C Phase 2 companion:
# dispatcher.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B02, the
# gateway and an agent runtime (Claude Code, Flint, or both); memory is
# optional (see dispatcher.manifest.json).
#
# Installs the dispatcher companion: the node's REALTIME trigger. Per
# the runtime model, the brain is ephemeral and stateless -- something must
# wake it. Two triggers do: the secretary (scheduled -- it lives in the workflows
# companion) and the dispatcher (realtime -- this one). An inbound
# message on a channel where someone is waiting -- voice, web chat --
# must wake the brain NOW; the dispatcher is that path. It also does
# intent/NLU classification and the direct-agent-vs-Rein routing
# decision. It is NOT the brain -- it wakes the brain.
#
# Component: brin -- a FastAPI/uvicorn service (HTTP API on :8101, an
# MCP server on :8102). `brin` is a working name (like `rein`);
# `dispatcher` is the functional name (operator decision 2026-05-17).
# The companion artifact is dispatcher.sh; the component stays brin.
#
# Native systemd service, NOT a container: brin is a Python app and the
# gateway companion already establishes the native-service pattern for
# Smart Node Python components. The component ships as a vendored
# snapshot in the sibling directory `brin/` (the gateway pattern).
#
# Why a companion, not a bootstrap stage: per installer-stages.md
# the dispatcher is a NEW canonical element -- it is
# installed, operator-triggered, in Phase 2 once its dependencies
# (messages substrate, the gateway egress, the memory store, the agent
# runtime) are in place.
#
# Creates:
#   - service account smart-node-dispatcher (+ owned dirs) via lib.sh
#     create_service_user
#   - /opt/smart-node/dispatcher -- the install tree (vendored brin
#     snapshot + a Python venv with the pinned requirements + the
#     runtime _storage/ and logs/ dirs brin writes into)
#   - /opt/smart-node/dispatcher/.env (0600, service-user owned) -- the
#     runtime config: loopback bind, a generate-once BRIN_API_KEY, the
#     local gateway URL, the memory companion's Mesh URL
#   - smart-node-dispatcher.service -- the systemd unit
#   - smart-node-flow-sweep.service + .timer -- once a minute, has the flow
#     engine fail every run that is over its time budget (flow_sweep.py)
#   - smart-node-runner.service -- the agent launcher: polls the
#     bus every few seconds and starts the agent that has work, so an answer
#     can come back inside a live conversation instead of on the next cron wake
#   - companions.d/dispatcher.json -- the manifest the B06 panel reads
#   - "Companion dispatcher" section in the install report
#   - /var/lib/smart-node/stages/dispatcher.state
#
# Brain wiring: brin reaches the gateway companion on the loopback
# (127.0.0.1:8000) and the memory companion's Mesh container at its
# smart-node-bridge IP. The dispatcher resolves the smart-node-mesh
# container IP and points MESH_API_URL there. If memory is somehow
# unresolvable the dispatcher still installs (degraded) -- the brain
# wiring is exercised, not asserted, by --verify; --verify checks that
# the brin service is up and answers /health, NOT that a dependency is
# reachable.
#
# Scope (v0.1.0): install + wire brin, nothing more. The messages
# registry read path (B02) and the MCP shim (:8102 -> mcp.d/) are
# follow-ups -- the same unsettled mcp.d item as the other companions.
# LLM provider keys live on the gateway, not here.
#
# Usage:
#   dispatcher.sh             full run: probe -> intent -> probe -> mark
#   dispatcher.sh --verify    idempotent post-check; exit 0 / N failures
#   dispatcher.sh --status    read /var/lib/smart-node/stages/dispatcher.state
#   dispatcher.sh --uninstall reverse the install
#   dispatcher.sh --help      this message
#
# Environment:
#   SMART_NODE_DISPATCHER_PORT  loopback port to bind (default 8101)
#
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

STAGE_ID="dispatcher"
STAGE_NAME="dispatcher"
STAGE_VERSION="0.2.11"

# Where this script lives -- the vendored brin snapshot sits beside it.
# install-on.sh / the operator ship the two together onto the node.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SNAPSHOT="$SCRIPT_DIR/brin"

# Install tree, namespaced like every Smart Node artifact.
SERVICE_SHORT="dispatcher"
SERVICE_USER="smart-node-dispatcher"
INSTALL_DIR="/opt/smart-node/dispatcher"
VENV_DIR="$INSTALL_DIR/venv"
ENV_FILE="$INSTALL_DIR/.env"
LOG_DIR="/var/log/smart-node/dispatcher"

# brin writes SQLite stores into _storage/, JSON session/history state
# into data/, and its own log into logs/ -- all relative to the install
# tree. They are runtime state, kept across re-runs and excluded from
# the snapshot refresh. data/ is wired explicitly via BRIN_DATA_DIR
# (.env) so brin never falls back to a host path.
STORAGE_DIR="$INSTALL_DIR/_storage"
DATA_DIR="$INSTALL_DIR/data"
BRIN_LOG_DIR="$INSTALL_DIR/logs"

# systemd unit -- native service (brin is a Python app).
SYSTEMD_UNIT="smart-node-dispatcher.service"
SYSTEMD_PATH="/etc/systemd/system/$SYSTEMD_UNIT"

# The planner credential, minted and written by the tracking companion.
# Bound to the `runner` actor and NOT delegating: brin authors as itself and
# cannot author as anyone else, which is exactly the difference between it and
# core-api. Both units below need it -- the runner writes task state, and the
# dispatcher's task watcher reads the task list.
PLANNER_KEY_ENV="/etc/smart-node/planner-key-runner.env"
# The interpreter of the flow engine (the workflows companion's venv). The
# flow sweep runs under it, because it calls the engine's own check.
FLOW_SWEEP_PY="/opt/smart-node/workflows/venv/bin/python"
# The sweep itself is a module of the flow engine. The dispatcher is installed
# before the workflows companion, so during an update from an engine older than
# the sweep the timer exists a few seconds before the module does.
FLOW_SWEEP_ENGINE="/opt/smart-node/workflows/rein-src/rein/sweep.py"

# brin binds 0.0.0.0 (all interfaces). Access boundary is ufw alone:
# open_firewall() opens the port only to docker bridge subnets, never to Anywhere.
# Native callers (Studio, voice, runner.py) use 127.0.0.1:PORT.
# Container callers use host.docker.internal:PORT.
# Port overridable via SMART_NODE_DISPATCHER_PORT; brin's own default is 8101.
DISP_HOST="0.0.0.0"
DISP_PORT="${SMART_NODE_DISPATCHER_PORT:-8101}"

# The gateway companion binds 127.0.0.1:8000 -- a native sibling
# reaches it on the loopback.
GATEWAY_URL="http://127.0.0.1:8000"

# The memory companion runs Mesh as a container on the smart-node
# network listening on container port 8000 (no published host port).
MESH_CT="smart-node-mesh"
PLANNER_CT="smart-node-planner"
PLANNER_PORT="8080"
MESH_PORT=8000

# lib.sh is installed by B01-core; the companion has no fallback.
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

# B2-Integrations phase 4: optional integration helper.
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

# python_bin -- the interpreter to build the venv with. Plain python3;
# ensure_python guarantees it plus the venv module exist.
PYTHON_BIN="python3"

# ensure_python -- the node needs python3 and the stdlib venv module.
# Debian splits venv into its own package; install it if `python3 -m
# venv` is unavailable. pip arrives inside the venv via ensurepip.
ensure_python() {
    command -v "$PYTHON_BIN" >/dev/null 2>&1 \
        || die "python3 not found on the node"
    if ! "$PYTHON_BIN" -m venv --help >/dev/null 2>&1; then
        log "python venv module missing -- installing python3-venv"
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null 2>&1 \
            || die "could not install python3-venv"
    fi
}

# resolve_mesh_url -- the URL the memory companion's Mesh container is
# reachable at. The container publishes no host port; a native process
# reaches it at the container's IP on the smart-node bridge. Echoes the
# URL on success, nothing on failure (caller treats empty as degraded).
resolve_mesh_url() {
    local net="${SMART_NODE_DOCKER_NETWORK:-smart-node}" ip="" url=""
    # Read the contract memory.sh leaves on disk first. Since the Mesh service is a
    # native systemd service on loopback, not a container, so the docker lookup
    # below answers nothing on a fresh node and dispatcher.sh installed
    # "degraded" with no MESH_API_URL at all -- which is how the returning-user
    # acceptance check came back "mesh entry was not seeded". core-api.sh
    # resolve_mesh_url already reads this file; this one was never taught to.
    if [[ -r /etc/smart-node/mesh.env ]]; then
        url=$(sed -n 's/^MESH_API_URL=//p' /etc/smart-node/mesh.env | head -1)
        if [[ -n "$url" ]]; then
            echo "$url"
            return 0
        fi
    fi
    command -v docker >/dev/null 2>&1 || return 0
    # IP on the smart-node network specifically -- fall back to the
    # first network if the named lookup yields nothing.
    ip=$(docker inspect -f \
        "{{with index .NetworkSettings.Networks \"$net\"}}{{.IPAddress}}{{end}}" \
        "$MESH_CT" 2>/dev/null || true)
    if [[ -z "$ip" ]]; then
        ip=$(docker inspect -f \
            '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
            "$MESH_CT" 2>/dev/null || true)
    fi
    # `[[ ... ]] && echo` as the LAST command of a function makes the FUNCTION
    # return 1 when there is no ip. The caller assigns from it, so `set -e` kills
    # dispatcher.sh right there, silently, on any node whose mesh container is
    # not up -- and the WARN written for exactly that case, two lines below the
    # assignment, has therefore never run. That defect predates this
    # file was vendored, and the same shape as
    #
    # Return 0 explicitly: "no url" is an answer, not a failure, and the caller
    # already treats an empty string as one.
    if [[ -n "$ip" ]]; then
        echo "http://$ip:$MESH_PORT"
    fi
    return 0
}

# resolve_planner_url -- where the planner answers on THIS node. Same shape as
# resolve_mesh_url: ask docker rather than assume a subnet.
#
# Assuming cost this node a day. Both units carried hardcoded IPs while the
# planner actually sat on a different Docker subnet -- docker gave the node's
# network a different range because the assumed one was already taken on that
# host. Neither address answered. The dispatcher unit set nothing at
# all, so brin's task watcher used the code default and logged a ConnectError
# with a full traceback several times a minute, all day, for a planner that
# was up the whole time.
resolve_planner_url() {
    local net="${SMART_NODE_DOCKER_NETWORK:-smart-node}" ip="" url=""
    # Ask the contract tracking.sh leaves on disk BEFORE asking docker. Since
    # tracking.sh 0.5.0 the planner is a NATIVE systemd service on loopback, not
    # a container, so the lookup below finds nothing on any fresh node and the
    # caller used to fall through to a literal. That literal, 172.19.0.1, is a
    # bridge address that exists on the node this was written on and nowhere
    # else: on a stand it routes OFF THE MACHINE, and the stand spent forty
    # minutes polling the PRODUCTION planner every five seconds.
    # tracking runs before dispatcher in Phase 2, so the file is already there.
    if [[ -r /etc/smart-node/planner.env ]]; then
        url=$(sed -n 's/^PLANNER_API_URL=//p' /etc/smart-node/planner.env | head -1)
        if [[ -n "$url" ]]; then
            echo "$url"
            return 0
        fi
    fi
    command -v docker >/dev/null 2>&1 || return 0
    ip=$(docker inspect -f         "{{with index .NetworkSettings.Networks \"$net\"}}{{.IPAddress}}{{end}}"         "$PLANNER_CT" 2>/dev/null || true)
    if [[ -z "$ip" ]]; then
        ip=$(docker inspect -f             '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'             "$PLANNER_CT" 2>/dev/null || true)
    fi
    # Same trap as resolve_mesh_url: a bare `[[ ]] && echo` would return 1 here
    # and kill the run under set -e. "No url" is an answer.
    if [[ -n "$ip" ]]; then
        echo "http://$ip:$PLANNER_PORT"
    fi
    return 0
}

# ---------- modes ----------

# probe_before -- a re-run (our service/tree present) proceeds
# idempotently: the tree is re-synced, the venv reused, the unit
# rewritten. The generate-once BRIN_API_KEY in .env is preserved, and
# _storage/ (brin's SQLite stores) is kept.
probe_before() {
    if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1 \
       && systemctl cat "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        log "dispatcher service already present -- idempotent re-run"
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "WARN: $INSTALL_DIR exists but the service unit does not -- treating as a resumed run"
    fi
}

# install_snapshot -- copy the vendored brin snapshot into the install
# tree. --delete-style refresh: the tree mirrors the snapshot, but .env,
# venv/, logs/ and _storage/ (runtime state, not snapshot content) are
# kept.
install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/main.py" && -f "$SRC_SNAPSHOT/requirements.txt" ]] \
        || die "vendored snapshot incomplete ($SRC_SNAPSHOT) -- no main.py / requirements.txt"
    [[ ! -e "$SRC_SNAPSHOT/.git" ]] \
        || die "vendored snapshot carries a .git/ tree ($SRC_SNAPSHOT/.git) -- the brin repo's git remote holds a plaintext GitLab token; re-vendor without .git/"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
    log "syncing brin snapshot -> $INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude 'venv/' --exclude '.env' \
            --exclude 'logs/' --exclude '_storage/' --exclude 'data/' \
            "$SRC_SNAPSHOT"/ "$INSTALL_DIR"/
    else
        # rsync-free fallback: wipe everything but the kept runtime
        # state, recopy.
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name venv ! -name .env ! -name logs ! -name _storage ! -name data \
            -exec rm -rf {} + 2>/dev/null || true
        cp -a "$SRC_SNAPSHOT"/. "$INSTALL_DIR"/
    fi
    # Commit hash marker for sn-version (vendored snapshot has no .git).
    echo "${SN_BUILD_COMMIT:-unknown}" > "$INSTALL_DIR/.sn-commit"
    # brin's SQLite stores and its file log live in the install tree;
    # the parent dirs must exist and be service-user writable (SQLite
    # does not create parent directories).
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$STORAGE_DIR"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$DATA_DIR"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$BRIN_LOG_DIR"
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$INSTALL_DIR"
}

# build_venv -- create (or reuse) the venv and install the pinned
# requirements. pip runs as the service user; the venv is owned by it.
build_venv() {
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        log "creating venv -> $VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR" \
            || die "venv creation failed ($VENV_DIR)"
    else
        log "venv already present -- reusing"
    fi
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$VENV_DIR"
    log "installing requirements (pip)"
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    runuser -u "$SERVICE_USER" -- \
        "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
        || die "pip install of requirements failed"
}

# write_env -- the runtime config. systemd loads it via EnvironmentFile
# (brin has no python-dotenv). Generate-once for BRIN_API_KEY: a re-run
# re-reads the existing .env and keeps the key, so clients that stored
# it keep working. The file is 0600, service-user owned.
write_env() {
    local api_key=""
    if [[ -f "$ENV_FILE" ]]; then
        api_key=$(sed -n -E 's/^BRIN_API_KEY=//p' "$ENV_FILE" | tail -n1)
    fi
    if [[ -z "$api_key" ]]; then
        command -v openssl >/dev/null 2>&1 \
            || die "openssl not found -- B03 installs it; run B03 first"
        api_key=$(openssl rand -hex 24)
        log "generated a new BRIN_API_KEY"
    else
        log "preserving the existing BRIN_API_KEY (generate-once)"
    fi
    # Resolve the memory companion's Mesh URL. memory is optional (see
    # dispatcher.manifest.json): without it agents answer but keep no
    # long-term memory. Installing memory later re-runs this script, which
    # is how the URL gets here then.
    local mesh_url; mesh_url=$(resolve_mesh_url)
    if [[ -n "$mesh_url" ]]; then
        log "memory companion resolved -- MESH_API_URL=$mesh_url"
    elif stage_status memory >/dev/null 2>&1; then
        log "WARN: memory is installed but the $MESH_CT container IP did not resolve -- installing degraded (brin keeps its default MESH_API_URL)"
    else
        log "memory companion not installed -- agents run without long-term memory"
    fi

    # Which agent the message watcher polls the core API as. Resolved from the
    # node rather than assumed: see the note where it is written below.
    # `|| true` is load-bearing: under `set -euo pipefail` a grep that matches
    # nothing exits 1 and takes the whole companion down. A node with no agents
    # yet is a normal state, not an install failure -- it must fall through to
    # the warning below.
    local WATCHER_ACTOR=""
    WATCHER_ACTOR=$(curl -s --max-time 4 \
        --unix-socket "${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}" \
        "http://core-api/api/agents" 2>/dev/null \
        | grep -oE '"slug"[[:space:]]*:[[:space:]]*"[^"]*"' \
        | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"$/\1/' || true)
    if [[ -n "$WATCHER_ACTOR" ]]; then
        log "message watcher will poll as '$WATCHER_ACTOR' (an agent this node actually has)"
    else
        log "WARN: no agent resolved for the message watcher -- inbound routing will be REFUSED on every poll until BRIN_DEFAULT_AGENT is set in $ENV_FILE"
    fi

    # The service credential brin proves itself with when it reads an agent's own
    # ledger. core-api refuses an agent identity asserted over TCP and
    # brin is not the agent's process, so without this its inbox, tool-call and
    # activity fetches all come back 403 -- and are read as an empty ledger,
    # because a ledger failure is deliberately silent.
    local svc_secret=""
    if declare -f _envfile_get >/dev/null 2>&1; then
        svc_secret=$(_envfile_get /etc/smart-node/core-api-service-auth.env SN_SERVICE_AUTH_SECRET 2>/dev/null || true)
    fi
    if [[ -z "$svc_secret" ]]; then
        log "WARN: no service credential at /etc/smart-node/core-api-service-auth.env -- brin will read every agent ledger as empty (run core-api first)"
    fi
    if [[ -z "$(_envfile_get /etc/smart-node/operator.env SN_OPERATOR_ACTOR 2>/dev/null || true)" ]]; then
        log "WARN: no owner in /etc/smart-node/operator.env -- desk delivery, owner alerts and approved replies will be REFUSED by core-api (run core-api first)"
    fi

    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node dispatcher companion runtime config -- dispatcher.sh@$STAGE_VERSION."
        echo "# Mode 0600. Loaded by systemd via EnvironmentFile= (brin has no dotenv)."
        echo "# LLM provider keys live on the gateway companion, not here."
        echo "HOST=$DISP_HOST"
        echo "PORT=$DISP_PORT"
        echo "BRIN_API_KEY=$api_key"
        if [[ -n "$svc_secret" ]]; then
            echo "# Service auth -- brin reads an agent's ledger AS that agent."
            echo "SN_SERVICE_AUTH_SECRET=$svc_secret"
        fi
        echo "AI_GATEWAY_URL=$GATEWAY_URL"
        # Root brin's runtime data/ under the install tree -- otherwise
        # it falls back to a host path that does not exist on the node.
        echo "BRIN_DATA_DIR=$DATA_DIR"
        if [[ -n "$mesh_url" ]]; then
            # MESH_API_URL -- the memory services / smart-search path.
            # MESH_URL -- the dependency health probe (core/health.py).
            echo "MESH_API_URL=$mesh_url"
            echo "MESH_URL=$mesh_url"
        else
            echo "# MESH_API_URL unresolved at install -- memory companion"
            echo "# container IP not found; brin falls back to its default."
        fi
        # Node identity for the core API: the assistant mailbox it notifies as,
        # its bot handle, the console base for task links, the Doci and webchat
        # bases, the Google OAuth callback, and the mail domain stamped into a
        # minted Message-ID. Every one of these is the operator's own, so the
        # code ships examples and the real values are configured here. Anything
        # left unset stays an example, which is visibly unconfigured rather
        # than quietly somebody else's.
        if [[ -n "${SMART_NODE_MSGID_DOMAIN:-}" ]]; then
            echo "SN_MSGID_DOMAIN=$SMART_NODE_MSGID_DOMAIN"
        fi
        if [[ -n "${SMART_NODE_NOTIFY_EMAIL_SENDER:-}" ]]; then
            echo "SN_NOTIFY_EMAIL_SENDER=$SMART_NODE_NOTIFY_EMAIL_SENDER"
        fi
        if [[ -n "${SMART_NODE_NOTIFY_TELEGRAM_SENDER:-}" ]]; then
            echo "SN_NOTIFY_TELEGRAM_SENDER=$SMART_NODE_NOTIFY_TELEGRAM_SENDER"
        fi
        if [[ -n "${SMART_NODE_TASK_URL_BASE:-}" ]]; then
            echo "SN_TASK_URL_BASE=$SMART_NODE_TASK_URL_BASE"
        fi
        if [[ -n "${SMART_NODE_DOCI_URL:-}" ]]; then
            echo "SN_DOCI_URL=$SMART_NODE_DOCI_URL"
        fi
        if [[ -n "${SMART_NODE_GOOGLE_REDIRECT_URI:-}" ]]; then
            echo "SN_GOOGLE_REDIRECT_URI=$SMART_NODE_GOOGLE_REDIRECT_URI"
        fi
        if [[ -n "${SMART_NODE_WEBCHAT_BASE_URL:-}" ]]; then
            echo "WEBCHAT_BASE_URL=$SMART_NODE_WEBCHAT_BASE_URL"
        fi
        # The identity the message watcher polls the core API as. Its code
        # default is a literal agent name, and a node that has no agent by that
        # name is REFUSED on every poll: inbound mail stops, the log fills with
        # 403s nobody watches, and the node merely looks quiet. Ask the node
        # which agents it has, exactly as the Studio had to stop guessing which
        # agent owns the main desk.
        if [[ -n "$WATCHER_ACTOR" ]]; then
            echo "BRIN_DEFAULT_AGENT=$WATCHER_ACTOR"
        fi
    } > "$tmp"
    install -m 0600 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$tmp" "$ENV_FILE"
    rm -f "$tmp"
    manifest_record file "$ENV_FILE"
}

open_firewall() {
    # brin binds 0.0.0.0; access boundary is ufw alone. A container
    # reaching brin crosses the host INPUT chain; under ufw default-deny that
    # is a silent TIMEOUT. Open the bridge subnet to brin's port only. The rule
    # must never be broadened to Anywhere -- that would expose Remote-User
    # assertions to the internet.
    # Idempotent; no-op if ufw is absent or inactive.
    command -v ufw >/dev/null 2>&1 || return 0
    ufw status 2>/dev/null | grep -q "Status: active" || return 0
    local subnet
    subnet=$(docker network inspect "${SMART_NODE_DOCKER_NETWORK:-smart-node}" \
        --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -m1 . || true)
    [[ -n "$subnet" ]] || { log "ufw: cannot determine bridge subnet -- skipping"; return 0; }
    if ufw status | grep -qE "${DISP_PORT}.*${subnet}"; then
        log "ufw: bridge -> :$DISP_PORT already allowed"
        return 0
    fi
    ufw allow from "$subnet" to any port "$DISP_PORT" proto tcp \
        comment "brin: docker bridge only" >/dev/null 2>&1 || true
    log "ufw: allowed $subnet -> :$DISP_PORT"
}

# install_service -- write and activate the systemd unit. The service
# runs as the unprivileged service user; the runtime config is loaded
# from .env via EnvironmentFile (not python-dotenv -- brin has none).
# render_unit <path> -- write the unit THIS version of the companion wants.
#
# Split out of install_service so --verify can render the same text and diff it
# against the unit the node is running. A release that changes a unit is
# deployed as files, nothing re-runs the stage that generates it, and the node
# keeps the old unit while running the new code without saying so.
render_unit() {
    # Resolved here rather than at the top of the run so --verify renders the
    # same text as an install does, and so a planner that moved is picked up on
    # the next companion run instead of needing the unit edited by hand.
    # An explicit input still wins; an empty answer keeps the old literal so a
    # node whose planner container is down is not silently left with no address
    # at all.
    local PLANNER_URL_RESOLVED
    PLANNER_URL_RESOLVED="${PLANNER_API_URL:-$(resolve_planner_url)}"
    # An unresolved address is left EMPTY on purpose. The literal that used to
    # stand here named another machine, which is worse than no address at all:
    # nothing fails, the service just talks to the wrong node. Empty makes the
    # service refuse at startup and name the missing variable.
    [[ -n "$PLANNER_URL_RESOLVED" ]]         || log "WARN planner address unresolved; the unit gets an empty PLANNER_API_URL and the service will refuse loudly"
    cat > "$1" <<EOF
[Unit]
Description=Smart Node dispatcher companion (brin -- realtime trigger)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
# The node owner, written by the core-api companion. The message watcher makes
# its delivery calls as the owner; '-' so a node without the file still starts
# and each such call fails naming SN_OPERATOR_ACTOR.
EnvironmentFile=-/etc/smart-node/operator.env
# The core-api address, written by the installer as a node contract. The
# service refuses to start without it; '-' means an older node that
# has no such file still boots and refuses loudly rather than silently.
EnvironmentFile=-/etc/smart-node/core-api.env
# The planner credential -- the task watcher inside this service reads
# the task list. Leading '-' so a node missing the file still starts and the
# planner's own 401 says what is wrong, rather than the whole dispatcher
# refusing to run.
EnvironmentFile=-$PLANNER_KEY_ENV
# The task watcher inside brin needs the ADDRESS as well as the credential. It
# had neither line until now and fell back to a code default pointing at a demo
# node, so it never once reached the planner on this machine.
Environment=PLANNER_API_URL=$PLANNER_URL_RESOLVED
# Desk messages wait for the watcher's delivery decision: poll every 2 s,
# not the code default of 10, so an answer in a chat is not held for 10 seconds.
Environment=MESSAGE_POLL_INTERVAL=${MESSAGE_POLL_INTERVAL:-2}
# A chat on screen keeps its live line open (/api/desks/{id}/live), and a graceful
# stop waits for every open response: a restart hung 90 s until systemd killed it.
# Five seconds, then the streams are cut and the pages reconnect.
ExecStart=$VENV_DIR/bin/python -m uvicorn main:app --host $DISP_HOST --port $DISP_PORT --timeout-graceful-shutdown 5
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$INSTALL_DIR $LOG_DIR
StandardOutput=append:$LOG_DIR/service.log
StandardError=append:$LOG_DIR/service.log

[Install]
WantedBy=multi-user.target
EOF
}

# unit_diff_text -- how the live unit differs from what this version generates,
# or nothing if they agree. It PRINTS rather than returning a verdict, because
# verify_check swallows both streams and a difference nobody can read is only
# half of removing the silence.
unit_diff_text() {
    if [[ ! -f "$SYSTEMD_PATH" ]]; then
        echo "the unit is not installed at all ($SYSTEMD_PATH)"
        return 0
    fi
    local want; want=$(mktemp)
    # render_unit logs (e.g. "planner address unresolved" on a node without the
    # planner); this function's stdout IS the diff, so a log line read as a
    # difference and failed every dispatcher run without tracking.
    render_unit "$want" >/dev/null
    if ! diff -q "$want" "$SYSTEMD_PATH" >/dev/null 2>&1; then
        diff -u "$SYSTEMD_PATH" "$want" 2>/dev/null | sed -n '3,30p' | sed 's/^/      /'
    fi
    rm -f "$want"
}

install_service() {
    local tmp; tmp=$(mktemp)
    render_unit "$tmp"
    install -m 0644 -o root -g root "$tmp" "$SYSTEMD_PATH"
    rm -f "$tmp"
    manifest_record systemd-unit "$SYSTEMD_UNIT"
    systemctl daemon-reload
    systemctl enable "$SYSTEMD_UNIT" >/dev/null 2>&1 \
        || die "systemctl enable $SYSTEMD_UNIT failed"
    systemctl restart "$SYSTEMD_UNIT" \
        || die "systemctl restart $SYSTEMD_UNIT failed"
    install_runner
}

# install_runner -- the agent launcher. Without it an agent is woken
# only by its per-minute cron, so a colleague's answer, or an answer from an
# agent's own back office, cannot come back inside a live conversation: the
# caller is left holding the line for up to a minute per hop. This polls the
# bus every few seconds and launches the agent that has work, which is what
# makes a spoken exchange feel like one. Same tree, same service user, same
# .env as the dispatcher; runner.py has shipped in the snapshot all along and
# simply was never started by an installer.
install_runner() {
    local unit="smart-node-runner.service"
    local tmp; tmp=$(mktemp)
    local PLANNER_URL_RESOLVED
    PLANNER_URL_RESOLVED="${PLANNER_API_URL:-$(resolve_planner_url)}"
    # An unresolved address is left EMPTY on purpose. The literal that used to
    # stand here named another machine, which is worse than no address at all:
    # nothing fails, the service just talks to the wrong node. Empty makes the
    # service refuse at startup and name the missing variable.
    [[ -n "$PLANNER_URL_RESOLVED" ]]         || log "WARN planner address unresolved; the unit gets an empty PLANNER_API_URL and the service will refuse loudly"
    cat > "$tmp" <<EOF
[Unit]
Description=Smart Node runner
After=network-online.target $SYSTEMD_UNIT
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
# The core-api address, as a node contract. The dispatcher unit above has had
# this line now; the runner did not, and the runner is the ONLY waker
# Without it CORE was empty, every poll of with-pending-work built a
# relative URL and died, and a fresh node looked healthy while no inbound
# message woke anybody: 414 failures in 35 minutes on the first stand to prove
# it. Leading '-' so an older node without the file still starts and refuses
# out loud rather than the unit failing to load.
EnvironmentFile=-/etc/smart-node/core-api.env
# The node owner. The runner recognises the owner's approval by this name and
# releases the approved reply as the owner; same '-' as the dispatcher unit.
EnvironmentFile=-/etc/smart-node/operator.env
Environment=RUNNER_POLL_INTERVAL=${RUNNER_POLL_INTERVAL:-5}
Environment=RUNNER_MAX_CONCURRENCY=${RUNNER_MAX_CONCURRENCY:-6}
Environment=RUNNER_ACTOR=runner
Environment=RUNNER_DRY_RUN=0
# The planner on THIS node, resolved from docker at install time rather than
# assumed. runner.py's own default is a demo node's address and reaches nothing
# here; so did the literal that used to be on this line.
Environment=PLANNER_API_URL=$PLANNER_URL_RESOLVED
# The planner credential. Same file as the dispatcher unit above: one
# key for brin, bound to the runner actor, so everything brin writes is
# authored by brin. Leading '-' for the same reason.
#
# No backticks anywhere in this heredoc -- it is UNQUOTED so the variables
# above expand, which means a backtick in a COMMENT is still a command
# substitution. One here cost a "runner: command not found" on the first node
# this ran on.
EnvironmentFile=-$PLANNER_KEY_ENV
# brin on THIS node, at the address it actually binds. runner.py defaults to
# loopback, which reaches nothing here -- and a wake that cannot connect
# leaves the agent marked broken, so its cron wake skips it too and the
# message is picked up by nobody at all.
Environment=BRIN_SELF_URL=http://127.0.0.1:$DISP_PORT
ExecStart=$VENV_DIR/bin/python runner.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$INSTALL_DIR $LOG_DIR
StandardOutput=append:$LOG_DIR/runner.log
StandardError=append:$LOG_DIR/runner.log

[Install]
WantedBy=multi-user.target
EOF
    install -m 0644 -o root -g root "$tmp" "/etc/systemd/system/$unit"
    rm -f "$tmp"
    manifest_record systemd-unit "$unit"
    systemctl daemon-reload
    systemctl enable "$unit" >/dev/null 2>&1 || die "systemctl enable $unit failed"
    systemctl restart "$unit" || die "systemctl restart $unit failed"
    log "runner started (poll ${RUNNER_POLL_INTERVAL:-5}s)"
    install_flow_sweep "$PLANNER_URL_RESOLVED"
}

# install_flow_sweep -- the clock for flow time budgets. A flow run lives
# nowhere between two steps, so a block that waits for an agent that never
# answers waits for good unless something looks at the clock: one task stood
# eight hours that way. flow_sweep.py has the engine check every run once a
# minute and records each sweep in the event log.
#
# It is written here, beside the runner unit, because it must run exactly as
# the Runner runs the engine: same user, same environment files, same planner
# address and key. The engine the sweep brings up calls the flow's own error
# handler, and that handler writes to the planner with the key the Runner
# passes down. A sweep without that key expires the block and then cannot
# tell anybody: the handler is refused and the task stays where it was.
#
# A timer and not a pass of the Runner: the Runner wakes agents in one loop
# every few seconds, and a walk over every run directory inside that loop
# would hold every wake for as long as the walk takes.
#
# Same rule as the heredoc above: UNQUOTED, so no backticks, not in a comment.
install_flow_sweep() {
    local planner_url="$1"
    local service="smart-node-flow-sweep.service" timer="smart-node-flow-sweep.timer"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
[Unit]
Description=Smart Node flow sweep -- enforce the time budgets of flow runs
After=network-online.target
# The engine is the workflows companion's. On a node without it there are no
# flow runs to check, and the unit is skipped rather than failed every minute.
ConditionPathExists=$FLOW_SWEEP_PY
# The same for an engine that has no sweep yet: skipped until the workflows
# companion has installed one, instead of one failed run that the validation
# at the end of the update then reports as a dead unit.
ConditionPathExists=$FLOW_SWEEP_ENGINE

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
EnvironmentFile=-/etc/smart-node/core-api.env
EnvironmentFile=-/etc/smart-node/operator.env
Environment=PLANNER_API_URL=$planner_url
EnvironmentFile=-$PLANNER_KEY_ENV
ExecStart=$FLOW_SWEEP_PY $INSTALL_DIR/flow_sweep.py
NoNewPrivileges=true
# The engine keeps a scratch directory under /tmp. The Runner has its own
# private one; without the same here the sweep meets a directory another
# user made and cannot check a single run.
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
SyslogIdentifier=smart-node-flow-sweep
EOF
    install -m 0644 -o root -g root "$tmp" "/etc/systemd/system/$service"
    cat > "$tmp" <<EOF
[Unit]
Description=Run the Smart Node flow sweep every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
    install -m 0644 -o root -g root "$tmp" "/etc/systemd/system/$timer"
    rm -f "$tmp"
    manifest_record systemd-unit "$service"
    manifest_record systemd-unit "$timer"
    systemctl daemon-reload
    systemctl enable --now "$timer" >/dev/null 2>&1 || die "systemctl enable --now $timer failed"
    log "flow sweep timer enabled (every minute)"
}

# wait_for_dispatcher -- poll the public health endpoint (auth-exempt)
# until the uvicorn app answers, against a 60s deadline. /health
# reports dependency status but always answers HTTP 200 once the app is
# up -- a 2xx means brin itself is alive, which is what we wait for.
wait_for_dispatcher() {
    local deadline=$(( $(date +%s) + 60 )) code
    log "waiting for the dispatcher to answer /health"
    while (( $(date +%s) < deadline )); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            "http://127.0.0.1:$DISP_PORT/health" 2>/dev/null || true)
        if [[ "$code" =~ ^2 ]]; then
            log "dispatcher is up (HTTP $code)"
            return 0
        fi
        sleep 2
    done
    die "dispatcher did not answer /health within 60s (unit $SYSTEMD_UNIT)"
}

# the API-gated path replaces the temporary direct-store workaround with an API-gated
# reader. Retire only those exact ACL/settings entries after the new dispatcher
# is healthy; preserve every unrelated per-agent setting.
retire_attachment_stopgaps() {
    "$VENV_DIR/bin/python" "$INSTALL_DIR/retire_attachment_stopgaps.py" \
        || die "could not retire the direct attachment-store workaround"
}

write_report_section() {
    local mesh_line
    if [[ -f "$ENV_FILE" ]] && grep -q '^MESH_API_URL=' "$ENV_FILE"; then
        mesh_line="memory:     $(sed -n -E 's/^MESH_API_URL=//p' "$ENV_FILE" | tail -n1)"
    else
        mesh_line="memory:     unresolved at install (degraded -- see WARN in the log)"
    fi
    local body
    body=$(cat <<EOF
**Version:** dispatcher@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
component:  brin (vendored snapshot, no .git/)
service:    $SYSTEMD_UNIT  (native systemd, user $SERVICE_USER)
install:    $INSTALL_DIR
bind:       0.0.0.0:$DISP_PORT  (all interfaces, ufw restricts to docker bridge subnets)
config:     $ENV_FILE  (0600; BRIN_API_KEY generate-once)
gateway:    $GATEWAY_URL  (the gateway companion, loopback)
$mesh_line
\`\`\`

The dispatcher is the node's realtime trigger -- an inbound message on
a channel where someone waits (voice, web chat) wakes the brain now. It
is the sibling of the scheduled secretary. The messages-registry read
path and the MCP shim (:8102 -> mcp.d/) are v0.1.0 follow-ups.
EOF
)
    report_section "Companion dispatcher" "$body"
}

# The agents need a runtime to be woken into: Claude Code (agent-runtimes) or
# Flint, chosen at install, either or both (companions/*.manifest.json "runtime").
RUNTIMES="agent-runtimes flint"
installed_runtimes() {
    local r out=""
    for r in $RUNTIMES; do
        awk -F= '$1=="status"{print $2; exit}' "$SMART_NODE_STATE_DIR/$r.state" 2>/dev/null | grep -qx done && out="$out,$r"
    done
    printf '%s' "${out#,}"
}
require_runtime() {
    [[ -n "$(installed_runtimes)" ]] || die "no agent runtime installed (one of: $RUNTIMES)"
}

run_intent() {
    require_root
    log "companion dispatcher -- start"
    require_stage B02
    require_stage gateway
    require_runtime
    log_init "$STAGE_ID"
    ensure_python
    probe_before
    # Service account + its owned home/log dirs.
    create_service_user "$SERVICE_SHORT" >/dev/null
    install_snapshot
    build_venv
    write_env
    install_service
    # Before the health wait: that probe crosses the same INPUT chain, so a
    # closed firewall would fail the install with a timeout instead.
    open_firewall
    wait_for_dispatcher
    retire_attachment_stopgaps
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,gateway,$(installed_runtimes)"
    write_report_section
    # Final verify pass; record check count.
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=dispatcher@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=dispatcher@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"brin\",\"checks\":$VERIFY_TOTAL}"
    fi
    if declare -F sn_integration_register >/dev/null; then
        sn_integration_register \
            --slug             brin \
            --label            "Routing" \
            --purpose          "Finds who a message is for and wakes that agent" \
            --kind             app \
            --status           available \
            --install-uri      "http://brin.${SMART_NODE_DOMAIN:-localhost}" \
            --install-path     "$INSTALL_DIR" \
            --description      "Text classifier and dispatch agent." \
            --companion-slug   "$STAGE_ID" || true
    fi
    register_messaging_mcp
    log "companion dispatcher -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

# Internal messaging as a grantable MCP server.
#
# The messaging BACKEND ships with core-api (POST /api/messages/send,/reply,
# /dismiss; GET /inbox) but nothing made it CALLABLE by a narrow agent: a
# business agent has no bash, and the CLI runtime only offers what its .mcp.json
# lists. mcp_messaging.py (installed with this snapshot) wraps those four
# endpoints; registering it here under slug `messaging` makes it grantable, and
# a grant renders it into that agent's .mcp.json -- the same path `doci` takes.
# The tool row of the same name carries the contract text into CLAUDE.md.
register_messaging_mcp() {
    declare -F sn_mcp_server_register >/dev/null || return 0
    sn_mcp_server_register \
        --slug      "messaging" \
        --name      "Internal messaging" \
        --transport stdio \
        --default-on \
        --command   "$VENV_DIR/bin/python" \
        --args      "[\"$INSTALL_DIR/mcp_messaging.py\"]" || true

    # project_state for a CLAUDE CLI agent. The api tool of that name is run only
    # by brin's function-calling loop; a CLI agent sees Bash and what its
    # .mcp.json lists, so without this server it holds the grant and cannot call
    # the tool. mcp_project_state.py is a thin door to GET /api/project-state/collect.
    sn_mcp_server_register \
        --slug      "project-state" \
        --name      "Project state" \
        --transport stdio \
        --command   "$VENV_DIR/bin/python" \
        --args      "[\"$INSTALL_DIR/mcp_project_state.py\"]" || true

    declare -F sn_integration_register >/dev/null || return 0
    local tools_tmp usage_tmp usage_ps_tmp
    tools_tmp=$(mktemp); usage_tmp=$(mktemp); usage_ps_tmp=$(mktemp)
    cat > "$usage_ps_tmp" <<'USAGE_PS'
## project-state -- facts about one project over one window

One tool, `project_state`. Use it when you are asked for a stand-up, a status or "what happened" on a project.

Call: project_state(slug, since, until, fresh). slug is the planner project slug (for example smart-node, flint). since is where the window starts: an ISO 8601 UTC time, a bare date (2026-10-10), or a relative value (-72h, -7d, -30m); use -72h when nothing was said. until is optional and defaults to now. fresh=true skips the 30-second cache; leave it off unless asked for the very latest.

The answer is raw facts, not prose: goals, linked tasks, what moved, comments, mentions from other projects, mesh memory, Doci documents and owner channels, plus two derived fields: waiting_for_owner (anything waiting on the owner, whatever its age) and where_it_stopped (an anchor when the window was quiet). Write the summary yourself from these facts, put waiting_for_owner first, and state only what the data shows. The answer can be long; read it, do not paste it.

The field `notes` lists sources that were not read or were cut short (a source that failed, a result that hit its row limit). Say so in the summary instead of passing a partial picture off as complete. If the call fails, say what the error says; do not make up a status.
USAGE_PS
    cat > "$usage_tmp" <<'USAGE'
## messaging -- colleagues and your inbox

Seven tools: send_message, inbox_read, inbox_reply, inbox_dismiss, chat_history, message_search, recall.

What the tool descriptions do not say:
- Looking for something you do not have in front of you (a list, a decision, a promise, a person, a document): call recall(q) FIRST. It searches your tasks, your conversations and your memory at once; if more than one could be what was meant, say so and ask which. Documents it does not search.
- Answer a colleague's message with inbox_reply on it; the node puts your answer where whoever asked is waiting. Never write into a chat that is not yours.
- A message from your own slug that carries an origin is a person waiting on your own line (a call): answer it with inbox_reply the same way.
- An answer listed under to_relay is one you asked for on someone's behalf: send it to that origin in your own words, then dismiss it.
- A request to do something is finished by doing it and saying so in the same thread.
- Thanks and acknowledgements: dismiss them, never answer, so no courtesy loop forms.
- Never narrate checking your inbox or name a tool to a person.
USAGE
    if command -v jq >/dev/null; then
        jq -n --rawfile u "$usage_tmp" --rawfile p "$usage_ps_tmp" \
            '[{name:"messaging",kind:"mcp",title:"Internal messaging",
               description:"Write to a colleague on this node and work your own inbox.",
               usage_md:$u},
              {name:"project-state",kind:"mcp",title:"Project state",
               description:"Collect the facts about one project over one time window, in one call.",
               usage_md:$p}]' > "$tools_tmp"
        # core-api is a native service, and every reader of this row is
        # native too -- brin is a systemd unit, not a container, and so is the
        # update-on.sh Phase 4 smoke check, which curls this endpoint from the
        # node host. host.docker.internal is a container-side name and does not
        # resolve on the host (getent exits 2), so storing it here breaks those
        # callers. 127.0.0.1 is just as stable against bridge renumbering, which
        # was the reason to resolve the URL at runtime, and matches tracking.sh resolve_core_api_url()
        # and the SN_CORE_API= line in core-api.sh.
        sn_integration_register \
            --slug         core-api \
            --label        "Internal messaging" \
            --purpose      "Messages between agents on this node" \
            --kind         system \
            --status       available \
            --endpoint     "http://127.0.0.1:${SMART_NODE_CORE_API_PORT:-8030}" \
            --description  "Node internal messaging: agents write to colleagues and work their inbox." \
            --tools-json   "$tools_tmp" || true
    fi
    rm -f "$tools_tmp" "$usage_tmp" "$usage_ps_tmp"
}

run_verify_checks() {
    # 1. B02 precondition.
    verify_check "stage B02 done" require_stage B02
    # 2. gateway companion precondition.
    verify_check "companion gateway done" require_stage gateway
    # 3. memory is optional (dispatcher.manifest.json), so it is not checked.
    # 4. an agent runtime is in place: Claude Code (agent-runtimes), Flint, or both.
    verify_check "an agent runtime is installed" require_runtime
    # 5. service user exists.
    verify_check "service user $SERVICE_USER exists" id -u "$SERVICE_USER"
    # 6. install tree present.
    verify_check "install dir $INSTALL_DIR exists" test -d "$INSTALL_DIR"
    # 7. vendored app present in the tree.
    verify_check "brin main.py installed" test -f "$INSTALL_DIR/main.py"
    # 8. no .git/ leaked into the install tree (the brin repo's git
    #    remote carries a plaintext GitLab token).
    verify_check "no .git/ in the install tree" bash -c "
        [[ ! -e '$INSTALL_DIR/.git' ]]"
    # 9. venv interpreter present.
    verify_check "venv interpreter present" test -x "$VENV_DIR/bin/python"
    # 10. core deps importable in the venv.
    verify_check "venv has uvicorn + fastapi + httpx" \
        "$VENV_DIR/bin/python" -c "import uvicorn, fastapi, httpx"
    # 10b. internal-messaging MCP present and runnable from this venv -- it is
    #      what makes send/inbox callable by an agent that has no bash.
    verify_check "mcp_messaging.py installed" test -f "$INSTALL_DIR/mcp_messaging.py"
    #      Import the PACKAGE, not a submodule path: the SDK renamed its
    #      high-level server between majors (2.x mcp.server.MCPServer vs 1.x
    #      mcp.server.fastmcp.FastMCP) and mcp_messaging.py binds whichever is
    #      present, so pinning the check to one path fails a healthy node.
    verify_check "venv can import the MCP SDK" \
        "$VENV_DIR/bin/python" -c "import mcp"
    # 11. runtime state dirs present (_storage + data) -- brin writes
    #     SQLite stores and JSON session state into them.
    verify_check "_storage + data dirs present" bash -c "
        [[ -d '$STORAGE_DIR' && -d '$DATA_DIR' ]]"
    # 12. .env present and 0600.
    verify_check ".env present 0600" bash -c "
        [[ -f '$ENV_FILE' && \$(stat -c '%a' '$ENV_FILE' 2>/dev/null) == 600 ]]"
    # 13. .env owned by the service user.
    verify_check ".env owned by $SERVICE_USER" bash -c "
        [[ \$(stat -c '%U' '$ENV_FILE' 2>/dev/null) == '$SERVICE_USER' ]]"
    # 14. .env carries a BRIN_API_KEY.
    verify_check ".env holds BRIN_API_KEY" bash -c "
        grep -qE '^BRIN_API_KEY=.+' '$ENV_FILE'"
    # 15. .env wires the gateway.
    verify_check ".env holds AI_GATEWAY_URL" bash -c "
        grep -qE '^AI_GATEWAY_URL=.+' '$ENV_FILE'"
    # 16. systemd unit file installed.
    verify_check "unit $SYSTEMD_UNIT installed" test -f "$SYSTEMD_PATH"
    # "installed" and "active" are both true of a node whose unit is a release
    # behind, which is why neither has ever caught it.
    local unit_diff; unit_diff="$(unit_diff_text)"
    verify_check "unit matches what this version generates (not a release behind)" \
        test -z "$unit_diff"
    if [[ -n "$unit_diff" ]]; then
        echo "       the live unit differs from what this version generates." >&2
        echo "       Re-run this companion to regenerate it; nothing else will." >&2
        echo "$unit_diff" >&2
    fi
    # 17. unit enabled.
    verify_check "unit $SYSTEMD_UNIT enabled" \
        systemctl is-enabled "$SYSTEMD_UNIT"
    # 18. unit active.
    verify_check "unit $SYSTEMD_UNIT active" \
        systemctl is-active "$SYSTEMD_UNIT"
    # 19. dispatcher answers /health.
    verify_check "dispatcher answers /health" bash -c "
        code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
            'http://127.0.0.1:$DISP_PORT/health' 2>/dev/null)
        [[ \"\$code\" =~ ^2 ]]"
    # 20. companion manifest written.
    verify_check "companions.d/dispatcher.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/dispatcher.json"
    # 21. install-report has the dispatcher section.
    verify_check "install-report has the dispatcher section" \
        bash -c "grep -qFx '## Companion dispatcher' '$SMART_NODE_REPORT'"
    # 22. manifest records the install dir + the unit.
    verify_check "manifest records the dispatcher artifacts" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"dir\",\"value\":\"$INSTALL_DIR\",' \"\$m\" &&
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$SYSTEMD_UNIT\",' \"\$m\""
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

# run_uninstall_mode -- reverse the install. Companion installs are
# operator-reversible (the Cloudron card model), so the companion owns
# an explicit --uninstall in addition to the manifest replay that
# uninstall-from.sh performs for a whole-node teardown.
run_uninstall_mode() {
    require_root
    log_init "$STAGE_ID"
    log "companion dispatcher -- uninstall"
    if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1; then
        systemctl disable --now "$SYSTEMD_UNIT" >/dev/null 2>&1 || true
    fi
    rm -f "$SYSTEMD_PATH"
    systemctl daemon-reload >/dev/null 2>&1 || true
    rm -rf "$INSTALL_DIR"
    unregister_companion "$STAGE_ID"
    if declare -F sn_integration_unregister >/dev/null; then
        sn_integration_unregister --slug brin || true
    fi
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        userdel "$SERVICE_USER" >/dev/null 2>&1 || true
    fi
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion dispatcher -- uninstalled (log dir $LOG_DIR left for inspection)"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written),
#   probe_before (log-only, no state written), create_service_user (OS
#   account, one-time), install_snapshot (extract application files,
#   version-gated), build_venv (pip install, version-gated),
#   wait_for_dispatcher (health probe, no state written).
#
# CONTRACT (run): write_env (env file read by the dispatcher service on
#   every restart -- must be current), install_service (systemd unit file --
#   must be current), open_firewall (ufw rule for docker bridge -> DISP_PORT
#   -- if removed the agents cannot reach the dispatcher and classification
#   silently times out; idempotent), register_companion (companions.d record
#   for version tracking), sn_integration_register (sn_config.integrations
#   row for brin -- must be current), register_messaging_mcp (sn_config mcp
#   row -- must be current), stage_mark done.
run_contracts_only() {
    require_root
    log "companion dispatcher -- contracts only (v$STAGE_VERSION)"
    write_env
    install_service
    open_firewall
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,gateway,$(installed_runtimes)"
    if declare -F sn_integration_register >/dev/null; then
        sn_integration_register \
            --slug             brin \
            --label            "Routing" \
            --purpose          "Finds who a message is for and wakes that agent" \
            --kind             app \
            --status           available \
            --install-uri      "http://brin.${SMART_NODE_DOMAIN:-localhost}" \
            --install-path     "$INSTALL_DIR" \
            --description      "Text classifier and dispatch agent." \
            --companion-slug   "$STAGE_ID" || true
    fi
    register_messaging_mcp
    stage_mark "$STAGE_ID" done \
        "version=dispatcher@$STAGE_VERSION"
    log "companion dispatcher -- contracts only done"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
