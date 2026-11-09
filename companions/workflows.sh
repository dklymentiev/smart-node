#!/bin/bash
#
# workflows.sh -- Smart Node installer, Layer C Phase 2 companion:
# workflows.
#
# Runs INSIDE the target node as root. Idempotent. Hard dependency: B02
# (the messages registry the secretary scans). Companion-graph
# dependencies: gateway, memory, tracking, agent-runtimes (recorded, not
# hard-gated -- the Rein engine installs standalone; the links are
# logical ordering).
#
# Installs the workflows companion: declarative multi-agent workflow
# orchestration, plus the secretary -- the runtime model's SCHEDULED
# trigger.
#
# Two parts:
#
#   1. Rein -- a declarative YAML workflow orchestrator for multi-agent
#      AI. Installed as a native Python package in a venv (the gateway /
#      dispatcher native-component pattern); the component ships as a
#      vendored snapshot in the sibling directory `rein/`.
#
#   2. The secretary -- the scheduled trigger. The brain is ephemeral;
#      two triggers wake it -- the dispatcher (realtime, its
#      own companion) and the secretary (scheduled, here). The secretary
#      is a cron job that scans the B02 messages registry (the
#      message_state work queue) for unhandled inbound work on channels
#      where nobody waits in real time -- email, scheduled work -- and
#      is the path that wakes the brain for it. Sibling of brin: same
#      brain, different alarm clock.
#
# Creates:
#   - /opt/smart-node/workflows -- the install tree (the vendored Rein
#     snapshot, a Python venv with Rein installed, the secretary script)
#   - /etc/cron.d/smart-node-secretary -- the secretary cron job (every
#     5 minutes, through the Smart Node cron-runner), via register_cron
#   - /var/log/smart-node/secretary.log -- the secretary's run log
#   - companions.d/workflows.json -- the manifest the B06 panel reads
#   - "Companion workflows" section in the install report
#   - /var/lib/smart-node/stages/workflows.state
#
# v0.1.0 scope: install the Rein engine + wire the secretary cron. The
# secretary scans the registry and probes the dispatcher; the brain-wake
# dispatch protocol (handing a message_state row to the brain and
# advancing its status) is a follow-up -- the same deferred item as the
# dispatcher companion's registry read path. No systemd service: Rein is
# invoked per workflow, not run as a daemon, in v0.1.0. The Rein MCP
# server is NOT wired into mcp.d/ in v0.1.0.
#
# Usage:
#   workflows.sh             full run: probe -> intent -> probe -> mark
#   workflows.sh --verify    idempotent post-check; exit 0 / N failures
#   workflows.sh --status    read /var/lib/smart-node/stages/workflows.state
#   workflows.sh --uninstall reverse the install
#   workflows.sh --help      this message
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

STAGE_ID="workflows"
STAGE_NAME="workflows"
STAGE_VERSION="0.2.3"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The vendored Rein snapshot + the secretary script ship beside this
# script.
SRC_SNAPSHOT="$SCRIPT_DIR/rein"
SECRETARY_SRC="$SCRIPT_DIR/workflows-secretary.sh"
FLOWS_SRC="$SCRIPT_DIR/flows"

# Install tree.
INSTALL_DIR="/opt/smart-node/workflows"
REIN_SRC_DIR="$INSTALL_DIR/rein-src"
VENV_DIR="$INSTALL_DIR/venv"
SECRETARY="$INSTALL_DIR/secretary.sh"

# The runtime tree. Separate from the engine install on purpose: the engine is
# ours and is replaced wholesale on every upgrade, while this is where the owner
# authors their own flows and must survive one. The paths are not a local choice
# either -- companions/brin/runner.py and core-api/routes/workflows.py name them,
# and until 1.12.14 nothing created them, so the runtime read from a tree that
# existed on no node built from a release.
FLOWS_DIR="/opt/smart-node/flows"
FLOWS_AGENTS="$FLOWS_DIR/agents"
RUNNER_AGENTS="$FLOWS_DIR/runner-agents"
# The node's own flows, as the Runner runs them. core-api's import door publishes
# a flow here: a copy of what its author wrote and the door checked. It is in
# the node's state and not under /opt: /opt is replaced by an update, and a flow
# the node made must outlive one and travel with the node's backup. This
# companion creates the tree and NEVER writes inside flows/, on install or on
# update.
AUTHORED_AGENTS="/var/lib/smart-node/workflows"
RUN_WRAPPER="$FLOWS_DIR/run-workflow.py"
# Owns the dispatcher's run directories (runner-agents/). Created by the
# dispatcher companion, which may not have run yet; the tree is laid down either
# way and adopted when it appears.
DISPATCHER_USER="smart-node-dispatcher"
# Owns the authoring side's run directories (agents/tasks, agents/state), because
# it is the user that runs run-workflow.py: core-api spawns the wrapper for
# POST /api/workflows/{name}/run. These used to go to the dispatcher too, relying
# on group write for core-api, but the permissions stage declares the flows tree
# 2755 `tree` and strips group write, so every run started through core-api died
# on PermissionError before its first block.
CORE_API_USER="smart-node-core-api"

# The secretary cron job: name (cron.d file = smart-node-secretary),
# schedule, the command the cron-runner wraps.
CRON_NAME="secretary"
CRON_SCHEDULE="*/5 * * * *"

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
    --uninstall) MODE="uninstall" ;;
    --contracts-only) MODE="contracts-only" ;;
    *)           die "unknown arg: $1 (use --help)" ;;
esac

# ---------- helpers ----------
PYTHON_BIN="python3"

# ensure_python -- the node needs python3 and the stdlib venv module.
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

# ---------- modes ----------

probe_before() {
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        log "workflows venv already present -- idempotent re-run"
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "WARN: $INSTALL_DIR exists but no venv -- treating as a resumed run"
    fi
}

ensure_dirs() {
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
}

# install_snapshot -- copy the vendored Rein snapshot into the install
# tree. --delete-style refresh; the venv (runtime state) is untouched.
install_snapshot() {
    [[ -d "$SRC_SNAPSHOT" ]] \
        || die "vendored Rein snapshot missing ($SRC_SNAPSHOT) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_SNAPSHOT/pyproject.toml" && -d "$SRC_SNAPSHOT/rein" ]] \
        || die "vendored Rein snapshot incomplete ($SRC_SNAPSHOT)"
    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$REIN_SRC_DIR"
    log "syncing the Rein snapshot -> $REIN_SRC_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "$SRC_SNAPSHOT"/ "$REIN_SRC_DIR"/
    else
        find "$REIN_SRC_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
        cp -a "$SRC_SNAPSHOT"/. "$REIN_SRC_DIR"/
    fi
    # rsync -a preserves source ownership, which may be wrong (e.g. the checkout
    # is admin:admin).  Normalise to root:group -- same pattern as voice.sh
    chown -R "root:$SMART_NODE_GROUP" "$REIN_SRC_DIR"
    chmod -R go-w "$REIN_SRC_DIR"
    manifest_record dir "$REIN_SRC_DIR"
}

# build_venv -- create (or reuse) the venv and install Rein into it.
build_venv() {
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        log "creating venv -> $VENV_DIR"
        "$PYTHON_BIN" -m venv "$VENV_DIR" \
            || die "venv creation failed ($VENV_DIR)"
    else
        log "venv already present -- reusing"
    fi
    log "installing Rein into the venv (pip)"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$VENV_DIR/bin/pip" install --quiet "$REIN_SRC_DIR" \
        || die "pip install of Rein failed"
}

# install_secretary -- place the secretary script and register its cron
# job. register_cron writes /etc/cron.d/smart-node-secretary and records
# it in the manifest; cron rescans cron.d on its own.
install_secretary() {
    [[ -f "$SECRETARY_SRC" ]] \
        || die "secretary script missing ($SECRETARY_SRC)"
    install -m 0755 -o root -g root "$SECRETARY_SRC" "$SECRETARY"
    manifest_record file "$SECRETARY"
    log "registering the secretary cron job ($CRON_SCHEDULE)"
    register_cron "$CRON_NAME" "$CRON_SCHEDULE" "$SECRETARY" root
}

# Flows, teams and specialists a release used to ship and no longer does. The
# tree sync below is additive, so without this list a node that was updated
# would keep them for good, unmaintained and failing the flow contract.
# Paths are relative to the agents tree.
RETIRED_FLOW_PATHS=(
    "flows/example-review"
    "teams/team-example.yaml"
    "specialists/drafter.md"
    "specialists/reviewer.md"
)

# install_flows_tree -- lay down /opt/smart-node/flows: the tree the runner and
# core-api read from. Authored content is the owner's, so the sync of the tree
# is additive: anything the owner wrote is left alone. A --delete sync of the
# whole tree would eat a node's own flows on every upgrade.
#
# A flow the release ships is different: it is ours, and it is replaced whole,
# so a file a new version dropped does not linger beside the new ones. A node
# that wants its own version copies the flow under another name.
install_flows_tree() {
    [[ -d "$FLOWS_SRC" ]] \
        || die "flows tree missing ($FLOWS_SRC) -- the companion ships it as a sibling dir"
    [[ -f "$FLOWS_SRC/run-workflow.py" ]] \
        || die "flows tree incomplete: run-workflow.py missing ($FLOWS_SRC)"
    [[ -x "$FLOWS_SRC/tools/flow-lint.py" ]] \
        || die "flows tree incomplete: tools/flow-lint.py missing ($FLOWS_SRC)"

    install -d -m 0750 -o root -g "$SMART_NODE_GROUP" "$FLOWS_DIR"
    manifest_record dir "$FLOWS_DIR"

    local d
    for d in flows specialists teams state tasks; do
        install -d -m 2770 -o root -g "$SMART_NODE_GROUP" "$FLOWS_AGENTS/$d"
    done

    log "syncing the flows tree -> $FLOWS_AGENTS"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a "$FLOWS_SRC/agents"/ "$FLOWS_AGENTS"/
    else
        cp -a "$FLOWS_SRC/agents"/. "$FLOWS_AGENTS"/
    fi
    local shipped name
    for shipped in "$FLOWS_SRC"/agents/flows/*/; do
        name="$(basename "$shipped")"
        # No rsync, no whole-flow replace: a file a new version dropped stays,
        # and that is said out loud.
        if command -v rsync >/dev/null 2>&1; then
            rsync -a --delete "$shipped" "$FLOWS_AGENTS/flows/$name/"
        else
            log "WARNING: rsync not found: shipped flow '$name' refreshed without removing files it no longer has"
        fi
    done
    local retired
    for retired in "${RETIRED_FLOW_PATHS[@]}"; do
        if [[ -e "$FLOWS_AGENTS/$retired" ]]; then
            rm -rf -- "${FLOWS_AGENTS:?}/$retired"
            log "retired: $retired (no longer shipped)"
        fi
    done
    # rsync -a preserves source ownership -- normalise before _chown_run_dirs
    # refines tasks/ and state/ to the dispatcher user.  No chmod here: the
    # subdirectories are 2770 (group-write) intentionally.
    chown -R "root:$SMART_NODE_GROUP" "$FLOWS_AGENTS"
    install -m 0755 -o root -g "$SMART_NODE_GROUP" "$FLOWS_SRC/run-workflow.py" "$RUN_WRAPPER"

    # The flow tools travel with the tree, because the people who need them are
    # on the node: an owner checking a flow they just wrote in the panel, an
    # agent asked to author one, a run that wants to know whether the flow it is
    # about to start is well formed. Shipped, not synced: these are ours, and a
    # node has no reason to edit them.
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$FLOWS_DIR/tools"
    manifest_record dir "$FLOWS_DIR/tools"
    local tool
    for tool in "$FLOWS_SRC"/tools/*; do
        [[ -f "$tool" ]] || continue
        install -m 0755 -o root -g "$SMART_NODE_GROUP" "$tool" "$FLOWS_DIR/tools/$(basename "$tool")"
        manifest_record file "$FLOWS_DIR/tools/$(basename "$tool")"
    done
    install -m 0644 -o root -g "$SMART_NODE_GROUP" "$FLOWS_SRC/README.md" "$FLOWS_DIR/README.md"
    manifest_record file "$RUN_WRAPPER"

    # The dispatcher's view of the same tree. flows and teams are shared with the
    # authoring side by link, so a flow written once is visible to both;
    # specialists is its own directory because runner-driven blocks and
    # flow-authored blocks are not the same prompts.
    install -d -m 2770 -o root -g "$SMART_NODE_GROUP" "$RUNNER_AGENTS"
    for d in specialists tasks state; do
        install -d -m 2770 -o root -g "$SMART_NODE_GROUP" "$RUNNER_AGENTS/$d"
    done
    ln -sfn ../agents/flows "$RUNNER_AGENTS/flows"
    ln -sfn ../agents/teams "$RUNNER_AGENTS/teams"
    manifest_record dir "$RUNNER_AGENTS"

    # The second agents directory, for the node's own flows. The engine resolves
    # a flow only as <agents-dir>/flows/<name>/<name>.yaml, so the Runner hands
    # it this directory for a flow published here. flows/ is a real directory,
    # owned by core-api (see _chown_run_dirs), and its contents are the node's:
    # created if absent, otherwise left exactly as found. Teams and the Runner's
    # specialists are the shipped ones, by link.
    install -d -m 2755 -o root -g "$SMART_NODE_GROUP" "$AUTHORED_AGENTS"
    local d
    for d in flows tasks state; do
        [[ -d "$AUTHORED_AGENTS/$d" ]] \
            || install -d -m 2755 -o root -g "$SMART_NODE_GROUP" "$AUTHORED_AGENTS/$d"
    done
    ln -sfn "$FLOWS_AGENTS/teams" "$AUTHORED_AGENTS/teams"
    ln -sfn "$RUNNER_AGENTS/specialists" "$AUTHORED_AGENTS/specialists"

    _chown_run_dirs
    # Commit hash marker for sn-version (flows tree has no .git).
    echo "${SN_BUILD_COMMIT:-unknown}" > "$FLOWS_DIR/.sn-commit"
    register_flows
}

# register_flows -- project what is on disk into sn_core.workflows.
#
# Without this the tree is laid down correctly and the catalog stays empty, which
# reads exactly like the defect this release fixes. The registrar belongs to
# core-api (it owns the schema and the script) but nothing has ever called it:
# before 1.12.14 its only mentions in this repository were documentation.
#
# core-api installs before workflows in the --all order, so it is normally there.
# A workflows-only install is the exception, and is told what it is missing
# rather than left looking complete.
register_flows() {
    local registrar="/opt/smart-node/core-api/companions/register-workflows.py"
    local api_py="/opt/smart-node/core-api/venv/bin/python"
    if [[ ! -f "$registrar" || ! -x "$api_py" ]]; then
        log "WARN: core-api is not installed, so the flow catalog stays empty."
        log "WARN: install core-api, then re-run this companion to register them."
        return 0
    fi
    log "registering flows into sn_core.workflows"
    SN_FLOWS_DIR="$FLOWS_AGENTS/flows" SN_AUTHORED_FLOWS_DIR="$AUTHORED_AGENTS/flows" "$api_py" "$registrar" \
        || die "the flow registrar failed -- the tree is installed but the catalog is empty"
}

# Each side's run directories belong to the user that writes them, so neither
# depends on group write, which the permissions stage removes.
_chown_run_dirs() {
    if id -u "$DISPATCHER_USER" >/dev/null 2>&1; then
        chown -R "$DISPATCHER_USER":"$SMART_NODE_GROUP" \
            "$RUNNER_AGENTS/tasks" "$RUNNER_AGENTS/state" \
            "$AUTHORED_AGENTS/tasks" "$AUTHORED_AGENTS/state"
    else
        log "WARN: $DISPATCHER_USER does not exist yet -- runner-agents run dirs left group-writable for it"
    fi
    if id -u "$CORE_API_USER" >/dev/null 2>&1; then
        chown -R "$CORE_API_USER":"$SMART_NODE_GROUP" \
            "$FLOWS_AGENTS/tasks" "$FLOWS_AGENTS/state"
        # The published flows belong to the service that publishes them. Not
        # recursive: what is inside is the node's and already has its owner.
        chown "$CORE_API_USER":"$SMART_NODE_GROUP" "$AUTHORED_AGENTS/flows"
    else
        log "WARN: $CORE_API_USER does not exist yet -- install core-api, then re-run this companion, or no workflow can start from the panel"
    fi
}

write_report_section() {
    local rein_ver
    rein_ver=$("$VENV_DIR/bin/python" -m rein --version 2>/dev/null | head -n1 || echo "unknown")
    local body
    body=$(cat <<EOF
**Version:** workflows@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
component:  Rein ($rein_ver) -- native, vendored snapshot
install:    $INSTALL_DIR  (venv + the Rein source)
secretary:  $SECRETARY  (cron $CRON_SCHEDULE, via /etc/cron.d/smart-node-secretary)
log:        /var/log/smart-node/secretary.log
\`\`\`

Declarative multi-agent workflow orchestration (Rein) plus the
secretary -- the runtime model's scheduled trigger. The secretary scans
the B02 messages registry for unhandled inbound work every 5 minutes.
v0.1.0 installs the engine and wires the cron; the brain-wake dispatch
protocol is a follow-up.
EOF
)
    report_section "Companion workflows" "$body"
}

run_intent() {
    require_root
    log "companion workflows -- start"
    require_stage B02
    log_init "$STAGE_ID"
    ensure_python
    probe_before
    ensure_dirs
    install_snapshot
    build_venv
    install_secretary
    install_flows_tree
    # Optional: a fresh node may not have native HITL yet. The HITL companion
    # retries after installing its caller. A poller error must not fail the
    # workflows companion.
    if ! python3 "$SCRIPT_DIR/hitl/install-poller.py"; then
        log "WARN: optional HITL poller setup failed; workflows remain installed"
    fi
    register_companion "$STAGE_ID" "$STAGE_VERSION" \
        "B02,gateway,memory,tracking,agent-runtimes"
    write_report_section
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=workflows@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=workflows@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"rein\",\"checks\":$VERIFY_TOTAL}"
    fi
    if declare -F sn_integration_register >/dev/null; then
        sn_integration_register \
            --slug             rein \
            --label            "Workflows" \
            --purpose          "Runs a sequence of agent steps" \
            --kind             app \
            --status           available \
            --install-uri      "http://rein.${SMART_NODE_DOMAIN:-localhost}" \
            --install-path     "$INSTALL_DIR" \
            --description      "Multi-agent workflow orchestrator." \
            --companion-slug   "$STAGE_ID" || true
    fi
    log "companion workflows -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    # 1. B02 precondition.
    verify_check "stage B02 done" require_stage B02
    # 2. install tree present.
    verify_check "install dir $INSTALL_DIR exists" test -d "$INSTALL_DIR"
    # 3. venv interpreter present.
    verify_check "venv interpreter present" test -x "$VENV_DIR/bin/python"
    # 4. Rein importable in the venv.
    verify_check "Rein importable in the venv" \
        "$VENV_DIR/bin/python" -c "import rein"
    # 5. Rein answers --version.
    verify_check "Rein answers --version" \
        "$VENV_DIR/bin/python" -m rein --version
    # 6. secretary script installed + executable.
    verify_check "secretary script installed executable" \
        test -x "$SECRETARY"
    # 7. secretary cron file present.
    verify_check "secretary cron file present" \
        test -f "/etc/cron.d/smart-node-$CRON_NAME"
    # 8. cron file invokes the secretary.
    verify_check "cron file invokes the secretary" bash -c "
        grep -qF '$SECRETARY' '/etc/cron.d/smart-node-$CRON_NAME'"
    # 9. the secretary runs cleanly (a cron job must exit 0).
    verify_check "secretary runs cleanly" "$SECRETARY"
    # 10. companion manifest written.
    verify_check "companions.d/workflows.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/workflows.json"
    # 11. install-report has the workflows section.
    verify_check "install-report has the workflows section" \
        bash -c "grep -qFx '## Companion workflows' '$SMART_NODE_REPORT'"
    # 13. the runtime tree the runner reads from exists.
    verify_check "flows tree $FLOWS_DIR exists" test -d "$FLOWS_AGENTS/flows"
    # 14. the run wrapper is installed and executable.
    verify_check "run-workflow.py installed executable" test -x "$RUN_WRAPPER"
    # 15. the dispatcher's view links back to the authored flows, not a copy.
    #     Checked by resolved target: a dangling or wrongly-aimed link passes a
    #     plain -e test when the name happens to exist.
    verify_check "runner-agents/flows resolves to the authored flows" bash -c "
        [[ \"\$(readlink -f '$RUNNER_AGENTS/flows')\" == \"\$(readlink -f '$FLOWS_AGENTS/flows')\" ]]"
    verify_check "runner-agents/teams resolves to the authored teams" bash -c "
        [[ \"\$(readlink -f '$RUNNER_AGENTS/teams')\" == \"\$(readlink -f '$FLOWS_AGENTS/teams')\" ]]"
    # 15b. the tree for the node's own flows: in the node's state, a real
    #      directory the publishing service owns, never a link into /opt, which
    #      an update replaces.
    verify_check "the node's own flows are kept outside /opt" bash -c "
        [[ \"\$(readlink -f '$AUTHORED_AGENTS/flows')\" != /opt/* ]]"
    verify_check "$AUTHORED_AGENTS/flows is a directory of its own, owned by $CORE_API_USER" bash -c "
        [[ -d '$AUTHORED_AGENTS/flows' && ! -L '$AUTHORED_AGENTS/flows' \\
           && \"\$(stat -c %U '$AUTHORED_AGENTS/flows')\" == '$CORE_API_USER' ]]"
    verify_check "$AUTHORED_AGENTS/teams resolves to the shipped teams" bash -c "
        [[ \"\$(readlink -f '$AUTHORED_AGENTS/teams')\" == \"\$(readlink -f '$FLOWS_AGENTS/teams')\" ]]"
    # 16. a shipped flow is where register-workflows.py will look for it.
    verify_check "shipped flow at the registrable path" \
        test -f "$FLOWS_AGENTS/flows/task-simple/task-simple.yaml"
    # 17. Rein's own resolver can load a shipped flow through this layout. This
    #     is the check that distinguishes a tree that exists from a tree that
    #     works: it fails if the flow, its team or a specialist of that team is
    #     missing or sits somewhere Rein does not look. Deliberately not run_preflight_validation:
    #     that one returns True when its own validator raises, so it cannot fail
    #     for the reason we care about.
    verify_check "Rein resolves a shipped flow, its team and its specialists" \
        "$VENV_DIR/bin/python" -c "
import yaml
from rein.config import ConfigLoader
cfg = ConfigLoader('$FLOWS_AGENTS')
assert cfg.flow_exists('task-simple'), 'flow not found by Rein'
wf = cfg.load_workflow(cfg.get_flow_path('task-simple'))
assert wf.get('blocks'), 'flow loaded but declares no blocks'
names = set(yaml.safe_load(open('$FLOWS_AGENTS/teams/' + wf['team'] + '.yaml'))['specialists'])
assert names, 'the team of the shipped flow names no specialist'
for n in sorted(names):
    assert cfg.load_specialist(n).strip(), 'specialist not resolvable: ' + n
"
    # 18. the wrapper actually starts. Run with no arguments it prints its usage
    #     and exits 2, which it can only reach after its interpreter has loaded
    #     the file and its imports. A plain -x test passed while the shebang
    #     named an interpreter without PyYAML, and the first real run died on
    #     the import.
    verify_check "run-workflow.py starts under its own interpreter" bash -c "
        '$RUN_WRAPPER' >/dev/null 2>&1; [[ \$? -eq 2 ]]"
    # 19a. the user that runs the wrapper can create a run. Measured by doing it,
    #      not by reading modes: the modes looked right in workflows.sh and were
    #      rewritten afterwards by the permissions stage.
    verify_check "core-api can create a run under $FLOWS_AGENTS/tasks" bash -c "
        probe='$FLOWS_AGENTS/tasks/.verify-\$\$'
        sudo -u '$CORE_API_USER' mkdir \"\$probe\" 2>/dev/null && rmdir \"\$probe\""
    # 19. manifest records the install dir + the cron file.
    verify_check "manifest records the workflows artifacts" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"dir\",\"value\":\"$INSTALL_DIR\",' \"\$m\" &&
        grep -qF '{\"type\":\"file\",\"value\":\"/etc/cron.d/smart-node-$CRON_NAME\",' \"\$m\""
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

# run_uninstall_mode -- reverse the install: the cron job, the install
# tree (venv + Rein source + the secretary).
run_uninstall_mode() {
    require_root
    log_init "$STAGE_ID"
    log "companion workflows -- uninstall"
    unregister_companion "$STAGE_ID"
    if declare -F sn_integration_unregister >/dev/null; then
        sn_integration_unregister --slug rein || true
    fi
    rm -f "/etc/cron.d/smart-node-$CRON_NAME"
    rm -rf "$INSTALL_DIR"
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion workflows -- uninstalled (secretary.log left for inspection)"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written), ensure_dirs
#   (directory create, one-time), install_snapshot (extract application
#   files, version-gated), build_venv (pip install, version-gated),
#   install_flows_tree (copy flow definition files to /opt, version-gated).
#
# CONTRACT (run): install_secretary (cron entry at /etc/cron.d/ and the
#   secretary script -- must be current; if the cron entry drifts the
#   scheduled secretary runs silently stop), register_companion (companions.d
#   record for version tracking), sn_integration_register (sn_config.integrations
#   row for rein -- must be current), stage_mark done.
run_contracts_only() {
    require_root
    log "companion workflows -- contracts only (v$STAGE_VERSION)"
    require_stage B02
    log_init "$STAGE_ID"
    install_secretary
    register_companion "$STAGE_ID" "$STAGE_VERSION" \
        "B02,gateway,memory,tracking,agent-runtimes"
    if declare -F sn_integration_register >/dev/null; then
        sn_integration_register \
            --slug             rein \
            --label            "Workflows" \
            --purpose          "Runs a sequence of agent steps" \
            --kind             app \
            --status           available \
            --install-uri      "http://rein.${SMART_NODE_DOMAIN:-localhost}" \
            --install-path     "$INSTALL_DIR" \
            --description      "Multi-agent workflow orchestrator." \
            --companion-slug   "$STAGE_ID" || true
    fi
    stage_mark "$STAGE_ID" done \
        "version=workflows@$STAGE_VERSION" \
        "contracts-only=true"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
esac
