#!/bin/bash
#
# validate.sh -- Smart Node installer, Phase 3: validate.
#
# Runs INSIDE the target node as root. The final installer stage --
# runnable from the CLI or the B06 web panel, any time after B06. It
# does not install anything; it confirms the node the installer built is
# actually healthy, and writes a pass/fail report.
#
# Three check groups:
#
#   1. Stage states -- every /var/lib/smart-node/stages/*.state must
#      report status=done. A `failed` (or absent expected) stage is the
#      loudest signal an install went wrong.
#
#   2. Artifact liveness -- the install manifest
#      (/var/lib/smart-node/manifest.jsonl) records every host artifact
#      each stage created. validate.sh walks it and confirms each
#      artifact is still live: containers running, systemd units active,
#      packages installed, dirs/files present, and so on.
#
#      The manifest keeps rows for stages that were later uninstalled.
#      Each row carries the stage that created it, so validate.sh checks
#      only rows whose stage is still installed (its state file still
#      present); rows from uninstalled stages are skipped, not failed.
#      Rows a stage has stopped creating are retired by lib.sh at the end
#      of that stage (see manifest_retire_stale) and carry a
#      "retired" timestamp -- those are skipped here too.
#
#   3. Security baseline -- a handful of live host-hardening checks
#      (UFW, SSH root login + password auth, fail2ban, auto-updates)
#      that Layer A is supposed to have established.
#
# Output:
#   - /etc/smart-node/validation-report.md -- the full result, rewritten
#     on every run (always reflects the latest validation).
#   - a one-time "## Validation" pointer section in the install report.
#   - /var/lib/smart-node/stages/validate.state -- done / failed.
#
# Usage:
#   validate.sh          run validation; exit 0 = all passed, 1 = failures
#   validate.sh --json   machine-readable one-line JSON summary
#   validate.sh --help   this message
#
#
# Exits 0 when every check passes, 1 otherwise.

set -euo pipefail

# --help works without lib.sh -- handle it before anything else.
case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="validate"
STAGE_VERSION="0.3.3"

# lib.sh is installed by B01-core; the stage has no fallback.
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run the bootstrap first" >&2
    exit 1
fi

VALIDATION_REPORT="/etc/smart-node/validation-report.md"
VALIDATION_SCOPE="/etc/smart-node/validation-scope.env"

# ---------- arg parse ----------
MODE="run"
case "${1:-}" in
    "")       MODE="run" ;;
    --json)   MODE="json" ;;
    *)        die "unknown arg: $1 (use --help)" ;;
esac

# ---------- the done-stage set ----------
# Associative array: a key per currently-installed stage whose state
# file reports done. Used to scope the manifest walk.
declare -A DONE_STAGES=()
VALIDATION_SELECTED_STAGES=()
VALIDATION_OMITTED_STAGES=()
VALIDATION_SCOPE_KNOWN=0

load_validation_scope() {
    [[ -r "$VALIDATION_SCOPE" ]] || return 0
    local selected omitted
    selected=$(_envfile_get "$VALIDATION_SCOPE" SN_VALIDATION_SELECTED_STAGES)
    omitted=$(_envfile_get "$VALIDATION_SCOPE" SN_VALIDATION_OMITTED_STAGES)
    IFS=',' read -ra VALIDATION_SELECTED_STAGES <<< "$selected"
    IFS=',' read -ra VALIDATION_OMITTED_STAGES <<< "$omitted"
    VALIDATION_SCOPE_KNOWN=1
}

load_done_stages() {
    local f id st
    shopt -s nullglob
    for f in "$SMART_NODE_STATE_DIR"/*.state; do
        id=$(basename "$f" .state)
        st=$(awk -F= '$1=="status"{print $2; exit}' "$f")
        [[ "$st" == done ]] && DONE_STAGES["$id"]=1
    done
    shopt -u nullglob
}

# validation-scope.env is written only by install-on. A companion it recorded
# as omitted can be installed later from the B06 panel (the installer log says
# so), which leaves the file stale. Such a stage is done and fully checked, so
# it must not be reported as "not installed and not checked".
drop_installed_from_omitted() {
    local id kept=()
    for id in "${VALIDATION_OMITTED_STAGES[@]}"; do
        [[ -n "$id" && -z "${DONE_STAGES[$id]:-}" ]] && kept+=("$id")
    done
    VALIDATION_OMITTED_STAGES=("${kept[@]}")
}

# ---------- check group 1: stage states ----------
check_stage_states() {
    local f id st
    shopt -s nullglob
    for f in "$SMART_NODE_STATE_DIR"/*.state; do
        id=$(basename "$f" .state)
        # validate.sh's own state file does not exist yet on a first run
        # and is irrelevant to validation -- skip it either way.
        [[ "$id" == "$STAGE_ID" ]] && continue
        st=$(awk -F= '$1=="status"{print $2; exit}' "$f")
        verify_check "stage $id reports done" test "$st" = done
    done
    shopt -u nullglob

    # Existing state files prove only what is present. The durable scope names
    # what this install selected, so deletion or accidental omission of an
    # expected companion cannot make its own validation checks disappear.
    for id in "${VALIDATION_SELECTED_STAGES[@]}"; do
        [[ -n "$id" ]] || continue
        if [[ ! -f "$SMART_NODE_STATE_DIR/$id.state" ]]; then
            VERIFY_TOTAL=$((VERIFY_TOTAL + 1))
            VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
            echo "[VERIFY-FAIL] selected companion stage $id missing -- validation scope requires it" >&2
        fi
    done
}

# ---------- check group 2: artifact liveness ----------

# check_one_artifact TYPE VALUE STAGE -- one verify_check for one
# manifest row. Types that cannot be meaningfully re-verified after the
# fact (pg-ident map lines, individual ufw rules) are treated as
# informational and skipped.
#
# Liveness itself lives in lib.sh (artifact_alive), because
# manifest_retire_stale decides with it too and the two must not be able
# to disagree. What stays here is the wording of the check, which
# is what the validation report reads back.
check_one_artifact() {
    local mtype="$1" mvalue="$2" mstage="$3"
    artifact_verifiable "$mtype" || return 0
    local verb
    case "$mtype" in
        docker-container)              verb="running" ;;
        docker-compose-project)        verb="has running containers" ;;
        docker-network|docker-volume)  verb="exists" ;;
        systemd-unit)                  verb="active" ;;
        package)                       verb="installed" ;;
        dir|file|user|group)           verb="present" ;;
        pg-cluster)                    verb="online" ;;
        *)                             verb="live" ;;
    esac
    verify_check "$mtype $mvalue ($mstage) $verb" artifact_alive "$mtype" "$mvalue"
}

check_artifacts() {
    [[ -f "$SMART_NODE_MANIFEST" ]] || {
        echo "[VERIFY-FAIL] manifest missing ($SMART_NODE_MANIFEST)" >&2
        VERIFY_TOTAL=$((VERIFY_TOTAL + 1))
        VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
        return 0
    }
    local mtype mvalue mstage
    while IFS=$'\t' read -r mtype mvalue mstage; do
        [[ -n "$mtype" && -n "$mvalue" ]] || continue
        # Scope: only artifacts created by a stage still installed.
        [[ -n "$mstage" && -n "${DONE_STAGES[$mstage]:-}" ]] || continue
        check_one_artifact "$mtype" "$mvalue" "$mstage"
        # Retired rows (.retired set) are dropped by the jq below: the
        # stage that owned them has since stopped creating them, and
        # asserting them is what broke every updating node previously.
    done < <(jq -r 'select(.retired == null)
                    | [.type, .value, (.stage // "")] | @tsv' \
                "$SMART_NODE_MANIFEST" 2>/dev/null)
}

# ---------- advisory: can this node's agents actually run? ----------
# NOT a check, on purpose: a headless install legitimately defers the LLM login
# (claude-auth-capture is TTY-gated), so failing here would fail every headless
# install and every golden bake. Staying silent is worse than either, though:
# without a credential the node installs 144/144, the panel is green, the agents
# get created, granted and put on a timer -- and then every wake ends with "Not
# logged in", exit 0, empty log, indistinguishable from an idle wake. Nothing
# anywhere says why the node never does any work. So say it here, once, loudly,
# in the run output AND in the validation report.
ADVISORIES=()
# ---------- check group 6: the node's public face ----------
#
# A node with a public domain has names this validation never looked at. On
# 2026-08-14 an update recreated the authum container, its Traefik label came
# back carrying only the internal name, `auth.<public>` began answering 404, and
# every other public name kept redirecting to it -- so sign-in was down while
# this script reported 189/189 passed. Checking only the internal estate is what
# made that possible.
#
# THE SPLIT BETWEEN FAIL AND ADVISORY IS DELIBERATE, and follows the reasoning
# of the LLM-credential advisory above:
#
#   a public name on a CONTAINER LABEL  -> FAIL. It is always wrong and always
#       actionable: the installer owns those containers and rewrites their
#       labels, so the name will disappear at the next update, silently. Moving
#       it into a dynamic file costs nothing and there is no case for leaving it.
#
#   a public name that does not ANSWER  -> advisory, not a failure. DNS, the CDN
#       and the edge are outside the node, and a domain being set up legitimately
#       does not resolve yet. Failing here would fail every install performed
#       mid-migration, which is how a check earns its way into being ignored.
#
# The node is asked what it claims rather than told: every Host() in the dynamic
# files and in the container labels, minus this node's own domain.
check_public_names() {
    local tool="${SN_PUBLIC_NAMES_TOOL:-/tmp/public-names.sh}"
    [[ -x "$tool" ]] || return 0          # not shipped to this node: say nothing
    # `|| rc=$?` and not a bare assignment: this script runs under `set -e`, the
    # tool exits non-zero by design when a name does not answer, and an
    # assignment whose command substitution fails takes the whole validation
    # down before it prints a verdict. That happened here on the first run, and
    # it is the same shape that stopped a companion mid-update this morning.
    local out rc=0
    out=$("$tool" --quiet 2>&1) || rc=$?
    # `grep -c` already prints 0 when nothing matches; it just exits 1 doing it.
    # An `|| echo 0` here appends a SECOND zero and the arithmetic below dies
    # with "syntax error in expression" -- which validate swallows, so the check
    # reports nothing and passes. That is exactly the shape this group exists to
    # catch, and it was in this function for one run.
    local on_labels
    on_labels=$(grep -c 'ON A CONTAINER LABEL' <<<"$out" 2>/dev/null || true)
    on_labels=${on_labels:-0}

    if (( on_labels > 0 )); then
        VERIFY_TOTAL=$((VERIFY_TOTAL + 1))
        VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
        echo "[VERIFY-FAIL] $on_labels public hostname(s) live on a container label;" >&2
        echo "              the installer rewrites those on the next update and the" >&2
        echo "              name will disappear without a word. Move them into" >&2
        echo "              /etc/smart-node/traefik/dynamic -- see docs/public-domain.md" >&2
        grep 'ON A CONTAINER LABEL' <<<"$out" >&2
    fi

    if (( rc != 0 )); then
        local dead
        dead=$(grep -c '^  FAIL' <<<"$out" 2>/dev/null || true)
        dead=${dead:-0}
        ADVISORIES+=("$dead public hostname(s) do not answer. That may be DNS or the edge rather than this node, but nothing else on this node will tell you: run tools/public-names.sh for the list.")
    fi
    return 0
}

# ---------- check group 5: the cluster listens where it claims ----------
#
# Companions in containers reach PostgreSQL over the docker bridge, so
# grant_pg_docker_access puts the bridge gateway in listen_addresses. Postgres
# does not fail when it cannot bind a listed address: it logs `could not bind
# IPv4 address` at WARNING, serves localhost alone, and exits zero. The unit
# then reports active and pg_lsclusters reports online while nothing inside a
# container can connect, which is what a node looks like after a boot that
# started the cluster before docker built the bridge.
#
# So do not ask systemd whether the cluster started. Ask the kernel whether it
# is listening on every address its own config claims.
check_pg_listen_addresses() {
    local env_file="/etc/smart-node/persistence.env"
    local hba port conf declared addr
    hba=$(sed -nE 's/^SMART_NODE_PG_HBA=//p'   "$env_file" 2>/dev/null | tr -d '"' | tail -n1)
    port=$(sed -nE 's/^SMART_NODE_PG_PORT=//p' "$env_file" 2>/dev/null | tr -d '"' | tail -n1)
    # No B02 contract on this node: there is no cluster to assert.
    [[ -n "$hba" ]] || return 0
    port="${port:-5432}"
    conf="$(dirname "$hba")/postgresql.conf"
    [[ -r "$conf" ]] || return 0

    # The last active assignment wins; absent one, the default is localhost,
    # which cannot fail to bind and so needs no check.
    # `|| true`: no active line is the normal state of a cluster nothing has
    # opened to the docker bridge, and under pipefail a grep that matches
    # nothing would end validate here with no message. That was every clean
    # install once documents stopped calling grant_pg_docker_access, the only
    # caller left.
    declared=$(grep -E '^[[:space:]]*listen_addresses[[:space:]]*=' "$conf" 2>/dev/null \
        | tail -n1 | sed -E "s/^[^=]*=[[:space:]]*'?//; s/'?[[:space:]]*(#.*)?\$//" || true)
    [[ -n "$declared" ]] || return 0

    # Split on commas WITHOUT letting the shell glob. the parser sets
    # listen_addresses = '*', and `for addr in ${declared//,/ }` is an
    # unquoted expansion: bash pathname-expands the '*' against the current
    # directory, so the guard below never sees a '*' to skip and the check
    # instead asserts that postgres is listening on every filename in $PWD.
    # It passed on a stand because an unmatched glob stays literal in an empty
    # directory, and failed on the live node with four filenames named as
    # addresses. read -ra splits without expanding.
    local -a _declared_addrs=()
    IFS=',' read -ra _declared_addrs <<< "$declared"
    for addr in "${_declared_addrs[@]}"; do
        addr="${addr//[[:space:]]/}"
        # localhost always binds; '*' means every address and cannot go missing.
        case "$addr" in
            ''|localhost|127.0.0.1|::1|'*'|'0.0.0.0') continue ;;
        esac
        verify_check "postgres: actually listening on $addr:$port (postgresql.conf declares it)" \
            bash -c "ss -ltnH 2>/dev/null | awk '{print \$4}' | grep -qx '$addr:$port'"
    done
}

check_llm_credential() {
    local env_file="${SMART_NODE_AGENT_ENV:-/etc/smart-node/agent-runtime.env}"
    if grep -qE '^(export +)?CLAUDE_CODE_OAUTH_TOKEN=.+' "$env_file" 2>/dev/null; then
        log "advisory: LLM credential present in $env_file -- agents can run"
        return 0
    fi
    local msg="NO LLM CREDENTIAL on this node. The install is complete and healthy, but every agent wake will end in \"Not logged in\" with exit 0 and an empty log, so the node will silently do NO work. Connect it in the panel (AI Gateway -> Connect); if the token is already stored, re-render it with POST /api/gateway/cli/claude-cli/project, then confirm ${env_file} holds CLAUDE_CODE_OAUTH_TOKEN."
    ADVISORIES+=("$msg")
    echo "[ADVISORY] $msg" >&2
    log "advisory: no LLM credential -- agents cannot run until one is connected"
}

# ---------- check group 4: the declared permission surface ----------
#
# Asserts the node still matches bootstrap/permissions.list: declared paths
# have their declared owner/group/mode, agents are not in the service group,
# and nothing writable is undeclared.
#
# This is the read-only half of the permissions stage. validate.sh never
# repairs -- it reports, so a drifted node says so instead of being quietly
# corrected during a health check.
check_permission_surface() {
    local stage="/usr/local/lib/smart-node/permissions.sh"
    if [[ ! -x "$stage" ]]; then
        # A node installed previously has no permission stage. Say so
        # rather than passing silently: the surface is simply unchecked.
        ADVISORIES+=("NO PERMISSION STAGE on this node ($stage absent). The declared permission surface is not being asserted; this node lacks the permission stage. Re-run the installer, or copy bootstrap/permissions.{sh,list} into /usr/local/lib/smart-node.")
        echo "[ADVISORY] permission surface unchecked -- $stage absent" >&2
        return 0
    fi
    verify_check "permissions: node matches the declared surface" "$stage" --verify
}

# ---------- check group 3: security baseline ----------
check_security_baseline() {
    # UFW enabled.
    verify_check "security: UFW active" bash -c \
        "ufw status 2>/dev/null | grep -qi '^Status: active'"
    # SSH root login not password-enabled (no / prohibit-password).
    verify_check "security: SSH root login hardened" bash -c \
        "sshd -T 2>/dev/null | grep -qiE '^permitrootlogin (no|prohibit-password)'"
    # SSH password authentication disabled.
    verify_check "security: SSH password auth disabled" bash -c \
        "sshd -T 2>/dev/null | grep -qi '^passwordauthentication no'"
    # fail2ban running.
    verify_check "security: fail2ban active" systemctl is-active --quiet fail2ban
    # Unattended security updates installed.
    verify_check "security: unattended-upgrades installed" \
        dpkg -s unattended-upgrades
}

# ---------- check group 7: no stale bridge-address literals in generated state ----------
#
# Bridge gateway addresses (Docker 172.16.0.0/12 range) must not be stored as
# literals that differ from the node's current live gateway. Such stored
# addresses become silent lies when the docker network is recreated -- five
# separate failures on 2026-09-04/05 proved this, including the case where the
# owner's Telegram messages were silently dropped all morning.
#
# Definition of "stale": a bridge-range IP in a config file or database column
# that does NOT equal the current live gateway of the smart-node docker network.
# An address that equals the live gateway is still stored (not ideal; the companion configuration
# avoids this coupling), but it is currently correct and not causing
# failures.
#
# Scope:
#   - systemd units and drop-ins under /etc/systemd/system
#   - all text config files under /etc/smart-node (traefik dynamic, mcp.d
#     shims, *.env, *.md, ...)
#   - component .env files under /opt/smart-node
#   - database: sn_core.tools.usage_md, sn_config.integrations.endpoint,
#               sn_config.policies.body_md
#
# Every finding is named with file:line or table.column (slug).
# One passing check is registered when the entire scope is clean.

_BRIDGE_RE='172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}'

# Print the live gateway of the smart-node docker network. Empty if unknown.
_bridge_live_gw() {
    local routing_env="${SMART_NODE_ROUTING_ENV:-/etc/smart-node/routing.env}"
    local net
    net=$(_envfile_get "$routing_env" SMART_NODE_DOCKER_NETWORK 2>/dev/null)
    # Strip surrounding quotes (envfile may or may not quote the value).
    net="${net#\"}" ; net="${net%\"}"
    net="${net:-smart-node}"
    docker network inspect \
        --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' \
        "$net" 2>/dev/null \
        | tr ' ' '\n' | grep -m1 '[0-9]' || true
}

# _bridge_scan_files LIVE_GW DIR [grep --include flags ...]
# Grep DIR for bridge-range IPs that differ from LIVE_GW. Prints
# [VERIFY-FAIL] file:line:content for each stale address and increments
# VERIFY_TOTAL / VERIFY_ERRORS. Returns 0 always.
_bridge_scan_files() {
    local live_gw="$1" dir="$2"; shift 2
    [[ -d "$dir" ]] || return 0
    local grepline content ip stale
    while IFS= read -r grepline; do
        content="${grepline#*:*:}"
        stale=0
        for ip in $(printf '%s' "$content" | grep -oE "$_BRIDGE_RE" 2>/dev/null); do
            [[ -n "$live_gw" && "$ip" == "$live_gw" ]] && continue
            # Skip network addresses in CIDR notation (e.g., 172.19.0.0/16):
            # a network address followed by '/' is a correct declaration,
            # not a baked contact point.
            [[ "$content" =~ (^|[^0-9.])${ip//./\\.}/[0-9] ]] && continue
            stale=$(( stale + 1 ))
        done
        (( stale > 0 )) || continue
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        VERIFY_ERRORS=$(( VERIFY_ERRORS + 1 ))
        echo "[VERIFY-FAIL] bridge literal: $grepline" >&2
    done < <(grep -rn -E "$_BRIDGE_RE" "$@" "$dir" 2>/dev/null || true)
    return 0
}

# _bridge_scan_db LIVE_GW
# Scan three database columns for stale bridge-address literals.
# Prints [VERIFY-FAIL] table.column slug=... for each stale row found.
_bridge_scan_db() {
    local live_gw="$1"
    [[ -r "$SMART_NODE_PERSISTENCE_ENV" ]] || return 0
    local pg_cluster pg_db
    pg_cluster=$(sed -nE 's/^SMART_NODE_PG_CLUSTER=//p' \
                     "$SMART_NODE_PERSISTENCE_ENV" 2>/dev/null \
                 | tr -d '"' | tail -n1)
    pg_db=$(sed -nE 's/^SMART_NODE_PG_CORE_DB=//p' \
                    "$SMART_NODE_PERSISTENCE_ENV" 2>/dev/null \
                | tr -d '"' | tail -n1)
    [[ -n "$pg_cluster" && -n "$pg_db" ]] || return 0

    local pg_role="${SMART_NODE_PG_CORE_ROLE:-smartnode}"

    # Build a per-column stale filter. When the live gateway is known: a row
    # is stale only if, after removing all occurrences of the live gateway,
    # the column still contains a bridge-range IP. That means a DIFFERENT
    # bridge IP is present.
    local stale_cond_tmpl=""
    if [[ -n "$live_gw" ]]; then
        local esc="${live_gw//./\\.}"
        stale_cond_tmpl="AND regexp_replace(COL, '${esc}', '', 'g') ~ '${_BRIDGE_RE}'"
    fi

    local sql="
SELECT 'sn_core.tools.usage_md',          slug
  FROM sn_core.tools
 WHERE usage_md  ~ '${_BRIDGE_RE}' ${stale_cond_tmpl//COL/usage_md}
UNION ALL
SELECT 'sn_config.integrations.endpoint', slug
  FROM sn_config.integrations
 WHERE endpoint  ~ '${_BRIDGE_RE}' ${stale_cond_tmpl//COL/endpoint}
UNION ALL
SELECT 'sn_config.policies.body_md',      slug
  FROM sn_config.policies
 WHERE body_md   ~ '${_BRIDGE_RE}' ${stale_cond_tmpl//COL/body_md};"

    local tbl slug
    while IFS='|' read -r tbl slug; do
        [[ -n "$tbl" ]] || continue
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        VERIFY_ERRORS=$(( VERIFY_ERRORS + 1 ))
        echo "[VERIFY-FAIL] bridge literal in database: $tbl slug=${slug}" >&2
    done < <(
        PGCONNECT_TIMEOUT=3 psql --cluster "$pg_cluster" \
            -U "$pg_role" -d "$pg_db" \
            -X -q -A -t --no-psqlrc \
            -c "$sql" 2>/dev/null || true
    )
    return 0
}

check_no_bridge_literals() {
    local live_gw
    live_gw=$(_bridge_live_gw)

    local initial_errors=$VERIFY_ERRORS

    # systemd units and drop-ins
    _bridge_scan_files "$live_gw" /etc/systemd/system \
        --include="*.service" --include="*.timer" --include="*.socket" \
        --include="*.conf"

    # /etc/smart-node: env, traefik dynamic, MCP shims, capabilities.md, ...
    # Exclude *-report.md: historical install records contain addresses that
    # were correct at the time they were written and must not be falsified.
    _bridge_scan_files "$live_gw" /etc/smart-node \
        --include="*.env" --include="*.conf" --include="*.json" \
        --include="*.yml" --include="*.yaml" --include="*.toml" \
        --include="*.md" --exclude="*-report.md"

    # component .env files under /opt/smart-node
    _bridge_scan_files "$live_gw" /opt/smart-node --include="*.env"

    # database columns that compose agent prompts
    _bridge_scan_db "$live_gw"

    # Register one passing check when no stale addresses were found.
    if [[ $VERIFY_ERRORS -eq $initial_errors ]]; then
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        if [[ "${SMART_NODE_VERIFY_VERBOSE:-0}" -eq 1 ]]; then
            echo "[ok] no stale bridge-address literals in generated state"
        fi
    fi
    # The function must not end on a test that can be false. `[[ ... ]] && echo`
    # as the last statement returns 1 when the condition fails, and under set -e
    # that killed validate.sh outright -- but only on a CLEAN node, which is the
    # worst possible time. Found by the 1.12.29 acceptance run.
    return 0
}

# ---------- check group 8: no bridge literals in companion artifacts ----------
#
# Companion-written artifacts must not contain a literal 172.x.x.x bridge address. The correct forms are 127.0.0.1 (native callers) and
# host.docker.internal (container callers).
#
# Scope:
#   /etc/smart-node/           -- companion env, config, and route files
#   /etc/systemd/system/       -- companion-written units and drop-ins
#   sn_config.integrations     -- endpoint column (registered by companions)
#
# Explicit exclusions (not in scope here):
#   routing.env                -- records the live gateway on purpose (the source of truth)
#   core-api.env               -- orchestrator-written, still carries bridge address
#   capabilities.md            -- orchestrator-written, still carries bridge address
#   *-report.md                -- historical install records, must not be falsified
#
# Complement to check group 7 (check_no_bridge_literals), which detects STALE
# addresses (those that differ from the current live gateway after a renumber).
# This check detects any bridge literal regardless of whether it matches the
# current gateway, verifying that companion endpoints use stable hostnames instead of bridge IPs.

_bridge_literal_scan_files() {
    # _bridge_literal_scan_files DIR [grep --include/--exclude flags ...]
    # Grep DIR for any bridge-range IP. Print [VERIFY-FAIL] file:line:content
    # for each line containing one and increment VERIFY_TOTAL / VERIFY_ERRORS.
    local dir="$1"; shift
    [[ -d "$dir" ]] || return 0
    local grepline content ip has_literal
    while IFS= read -r grepline; do
        content="${grepline#*:*:}"
        has_literal=0
        for ip in $(printf '%s' "$content" | grep -oE "$_BRIDGE_RE" 2>/dev/null); do
            # Skip network addresses in CIDR notation (e.g. 172.19.0.0/16):
            # a subnet declaration is a valid subnet declaration.
            [[ "$content" =~ (^|[^0-9.])${ip//./\\.}/[0-9] ]] && continue
            has_literal=$(( has_literal + 1 ))
        done
        (( has_literal > 0 )) || continue
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        VERIFY_ERRORS=$(( VERIFY_ERRORS + 1 ))
        echo "[VERIFY-FAIL] bridge literal in companion artifact: $grepline" >&2
    done < <(grep -rn -E "$_BRIDGE_RE" "$@" "$dir" 2>/dev/null || true)
    return 0
}

_bridge_literal_scan_db_integrations() {
    # Scan sn_config.integrations.endpoint for any bridge-range IP.
    [[ -r "$SMART_NODE_PERSISTENCE_ENV" ]] || return 0
    local pg_cluster pg_db
    pg_cluster=$(sed -nE 's/^SMART_NODE_PG_CLUSTER=//p' \
                     "$SMART_NODE_PERSISTENCE_ENV" 2>/dev/null \
                 | tr -d '"' | tail -n1)
    pg_db=$(sed -nE 's/^SMART_NODE_PG_CORE_DB=//p' \
                    "$SMART_NODE_PERSISTENCE_ENV" 2>/dev/null \
                | tr -d '"' | tail -n1)
    [[ -n "$pg_cluster" && -n "$pg_db" ]] || return 0

    local pg_role="${SMART_NODE_PG_CORE_ROLE:-smartnode}"
    local sql="
SELECT slug, endpoint
  FROM sn_config.integrations
 WHERE endpoint ~ '${_BRIDGE_RE}';"

    local slug endpoint
    while IFS='|' read -r slug endpoint; do
        [[ -n "$slug" ]] || continue
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        VERIFY_ERRORS=$(( VERIFY_ERRORS + 1 ))
        echo "[VERIFY-FAIL] bridge literal in sn_config.integrations.endpoint: slug=${slug} endpoint=${endpoint}" >&2
    done < <(
        PGCONNECT_TIMEOUT=3 psql --cluster "$pg_cluster" \
            -U "$pg_role" -d "$pg_db" \
            -X -q -A -t --no-psqlrc \
            -c "$sql" 2>/dev/null || true
    )
    return 0
}

check_bridge_literals() {
    local initial_errors=$VERIFY_ERRORS

    # /etc/smart-node/: companion env, config, route, and MCP shim files.
    # routing.env is excluded -- it holds the live gateway address deliberately.
    # core-api.env and capabilities.md are excluded -- orchestrator-written.
    # *-report.md files are excluded -- historical records that must not be falsified.
    _bridge_literal_scan_files /etc/smart-node \
        --include="*.env" --include="*.conf" --include="*.json" \
        --include="*.yml" --include="*.yaml" --include="*.toml" \
        --include="*.md" \
        --exclude="routing.env" \
        --exclude="core-api.env" \
        --exclude="capabilities.md" \
        --exclude="*-report.md"

    # Companion-written systemd units and drop-ins.
    _bridge_literal_scan_files /etc/systemd/system \
        --include="*.service" --include="*.timer" --include="*.socket" \
        --include="*.conf"

    # Integration endpoint column registered by companions.
    _bridge_literal_scan_db_integrations

    # Register one passing check when no bridge literals were found.
    if [[ $VERIFY_ERRORS -eq $initial_errors ]]; then
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        if [[ "${SMART_NODE_VERIFY_VERBOSE:-0}" -eq 1 ]]; then
            echo "[ok] no bridge IP literals in companion artifacts"
        fi
    fi
    # Same as check_no_bridge_literals: the function must not end on a test that
    # can be false. A `[[ ... ]] && echo` as the last statement returns 1 when the
    # condition fails, and under set -e that kills validate.sh on a CLEAN node.
    return 0
}

# ---------- check group 9: no unexpected node domain literals ----------
#
# SMART_NODE_DOMAIN must never appear as a literal in database prompt columns or
# component env files. The routing contract (routing.env) is the canonical source;
# companions read it at install/update time and write domain-bearing artifacts from
# it. A domain literal in a prompt column or a component .env is a companion
# violation (or a hand-written row that bypassed the companion), and it will be
# stale the moment the node is renamed or moved.
#
# Scope (only places where the domain must NOT appear as a literal):
#   - database: sn_config.policies.body_md  (policy text; never domain-specific)
#   - database: sn_core.tools.usage_md      (tool usage; must use abstract paths)
#   - /opt/smart-node component .env files  (source files; must not bake a domain)
#
# Explicitly EXCLUDED -- these exclusions are deliberate, not oversights:
#
#   - routing.env: the canonical source; the domain lives there by design.
#   - /etc/smart-node/traefik/dynamic/: Traefik route files. studio.sh and
#     companion scripts write SMART_NODE_DOMAIN here from routing.env at install
#     time. The domain is supposed to appear in these files. Flagging it would
#     fail every correctly-configured node.
#   - /etc/systemd/system/: unit files and drop-ins. Companions embed the domain
#     in ExecStart= and environment blocks, again sourced from routing.env. Same
#     reasoning as Traefik routes: presence is correct, not a violation.
#   - sn_config.integrations.install_uri: companion-owned column; the install URI
#     contains the domain by construction.
#
# This check is therefore categorically different from the bridge literal
# check (group 7/8). A 172.x.x.x address must never appear in a companion-written
# artifact -- that is the migration result being enforced. The node domain is
# supposed to appear in Traefik routes and unit files. This gate enforces
# the narrower claim: the domain must not be baked into rows or source env files
# that no companion owns, because those are the locations where a stale literal
# would silently survive a node rename.
#
# If SMART_NODE_DOMAIN is empty in routing.env, this check is skipped (no string
# to search for; a node without a domain has no domain literals to flag).
#
# The routing contract defines the node-domain ownership rule enforced here.

_node_domain_scan_db() {
    local domain="$1"
    [[ -r "$SMART_NODE_PERSISTENCE_ENV" ]] || return 0
    local pg_cluster pg_db
    pg_cluster=$(sed -nE 's/^SMART_NODE_PG_CLUSTER=//p' \
                     "$SMART_NODE_PERSISTENCE_ENV" 2>/dev/null \
                 | tr -d '"' | tail -n1)
    pg_db=$(sed -nE 's/^SMART_NODE_PG_CORE_DB=//p' \
                    "$SMART_NODE_PERSISTENCE_ENV" 2>/dev/null \
                | tr -d '"' | tail -n1)
    [[ -n "$pg_cluster" && -n "$pg_db" ]] || return 0

    local pg_role="${SMART_NODE_PG_CORE_ROLE:-smartnode}"
    # Escape dots in the domain for use in a SQL LIKE pattern.
    local esc_domain="${domain//./\\.}"
    local sql="
SELECT 'sn_config.policies.body_md', slug
  FROM sn_config.policies
 WHERE body_md ~ '${esc_domain}'
UNION ALL
SELECT 'sn_core.tools.usage_md', slug
  FROM sn_core.tools
 WHERE usage_md ~ '${esc_domain}';"

    local tbl slug
    while IFS='|' read -r tbl slug; do
        [[ -n "$tbl" ]] || continue
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        VERIFY_ERRORS=$(( VERIFY_ERRORS + 1 ))
        echo "[VERIFY-FAIL] node domain literal in database: $tbl slug=${slug}" >&2
    done < <(
        PGCONNECT_TIMEOUT=3 psql --cluster "$pg_cluster" \
            -U "$pg_role" -d "$pg_db" \
            -X -q -A -t --no-psqlrc \
            -c "$sql" 2>/dev/null || true
    )
    return 0
}

_node_domain_scan_env_files() {
    local domain="$1"
    [[ -d /opt/smart-node ]] || return 0
    local grepline
    while IFS= read -r grepline; do
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        VERIFY_ERRORS=$(( VERIFY_ERRORS + 1 ))
        echo "[VERIFY-FAIL] node domain literal in component env file: $grepline" >&2
    done < <(
        # SMART_NODE_DOMAIN=<domain> written by a companion
        # that sourced routing.env is the AUTHORIZED form. Exclude that exact pattern
        # (key is SMART_NODE_DOMAIN, value is the domain and nothing else) so the
        # scanner does not flag its own install. Every other occurrence -- the domain
        # embedded in a URL, in a non-SMART_NODE_DOMAIN key, or as a suffix -- is
        # still caught.
        grep -rn -F "$domain" /opt/smart-node --include="*.env" 2>/dev/null \
            | grep -v ":SMART_NODE_DOMAIN=${domain}$" \
            || true
    )
    return 0
}

check_no_node_domain_literals() {
    local routing_env="${SMART_NODE_ROUTING_ENV:-/etc/smart-node/routing.env}"
    local domain=""
    if [[ -r "$routing_env" ]]; then
        domain=$(_envfile_get "$routing_env" SMART_NODE_DOMAIN 2>/dev/null || true)
    fi
    # No domain configured: nothing to scan for.
    if [[ -z "$domain" ]]; then
        return 0
    fi

    local initial_errors=$VERIFY_ERRORS

    _node_domain_scan_db      "$domain"
    _node_domain_scan_env_files "$domain"

    if [[ $VERIFY_ERRORS -eq $initial_errors ]]; then
        VERIFY_TOTAL=$(( VERIFY_TOTAL + 1 ))
        if [[ "${SMART_NODE_VERIFY_VERBOSE:-0}" -eq 1 ]]; then
            echo "[ok] no unexpected node domain literals in prompt columns or component env files"
        fi
    fi
    return 0
}

# ---------- report ----------
write_validation_report() {
    local passed=$1 total=$2 failed=$3 verdict="$4"
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node -- validation report"
        echo ""
        echo "**Generated:** $(date -Iseconds)"
        echo "**Verdict:** $verdict"
        echo "**Checks:** $passed/$total passed ($failed failed)"
        if (( VALIDATION_SCOPE_KNOWN )); then
            if (( ${#VALIDATION_OMITTED_STAGES[@]} > 0 )); then
                echo "**Coverage:** scoped -- ${#VALIDATION_OMITTED_STAGES[@]} optional companion(s) not installed"
            else
                echo "**Coverage:** every companion selected by the installer"
            fi
        else
            echo "**Coverage:** undeclared -- this node predates validation-scope.env"
        fi
        echo ""
        echo "## Stages"
        echo ""
        local f id st
        shopt -s nullglob
        for f in "$SMART_NODE_STATE_DIR"/*.state; do
            id=$(basename "$f" .state)
            st=$(awk -F= '$1=="status"{print $2; exit}' "$f")
            echo "- $id: $st"
        done
        shopt -u nullglob
        echo ""
        echo "## Coverage"
        echo ""
        if (( VALIDATION_SCOPE_KNOWN )); then
            echo "- Selected companion stages: ${VALIDATION_SELECTED_STAGES[*]:-(none)}"
            echo "- Optional companions not installed and not checked: ${VALIDATION_OMITTED_STAGES[*]:-(none)}"
        else
            echo "- Installer scope unavailable; the denominator contains only stages and artifacts visible on this node."
        fi
        echo ""
        if (( failed > 0 )); then
            echo "## Failures"
            echo ""
            echo "$failed check(s) failed -- see the validate.sh run output"
            echo "(\`/var/log/smart-node/validate.log\`) for the \`[VERIFY-FAIL]\`"
            echo "lines."
        else
            echo "All checks passed."
        fi
        # Advisories are NOT failures: the node is correctly installed, but
        # something it needs from the operator is missing. They must survive in
        # the report -- an advisory only printed to a log nobody reads is the
        # silent failure this project keeps producing.
        if (( ${#ADVISORIES[@]} > 0 )); then
            echo ""
            echo "## Advisories -- installed correctly, but not yet able to work"
            echo ""
            local a
            for a in "${ADVISORIES[@]}"; do
                echo "- $a"
            done
        fi
    } > "$tmp"
    install -m 0644 -o root -g root "$tmp" "$VALIDATION_REPORT"
    rm -f "$tmp"
    # The validation report is ephemeral run output, not an installed
    # artifact -- it is deliberately NOT recorded in the manifest.
    # One-time pointer in the cumulative install report.
    report_section "Validation" \
"Phase 3 validation runs via \`validate.sh\`. The latest result is in
[\`validation-report.md\`]($VALIDATION_REPORT) -- rewritten on every run."
}

# ---------- main ----------
run_validate() {
    require_root
    log_init "$STAGE_ID"
    log "Phase 3 validate -- start"
    command -v jq >/dev/null 2>&1 \
        || die "jq not available -- B01-core should have installed it"
    load_validation_scope
    load_done_stages
    drop_installed_from_omitted
    (( ${#DONE_STAGES[@]} > 0 )) \
        || die "no done stages found -- run the bootstrap first"

    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    if (( ${#VALIDATION_OMITTED_STAGES[@]} > 0 )); then
        echo "[COVERAGE] optional companions not installed and not checked: ${VALIDATION_OMITTED_STAGES[*]}" >&2
    fi
    check_stage_states
    check_artifacts
    check_no_bridge_literals
    check_bridge_literals
    check_no_node_domain_literals
    check_security_baseline
    check_permission_surface
    check_pg_listen_addresses
    check_llm_credential
    check_public_names

    local total=$VERIFY_TOTAL failed=$VERIFY_ERRORS
    local passed=$(( total - failed )) verdict
    if (( failed == 0 )); then
        verdict="PASS"
    else
        verdict="FAIL"
    fi

    write_validation_report "$passed" "$total" "$failed" "$verdict"

    if (( failed == 0 )); then
        stage_mark "$STAGE_ID" done \
            "version=validate@$STAGE_VERSION" \
            "checks_passed=$passed/$total"
    else
        stage_mark "$STAGE_ID" failed \
            "version=validate@$STAGE_VERSION" \
            "checks_passed=$passed/$total"
    fi

    if declare -F log_event >/dev/null; then
        log_event "system:installer" "node.validated" "node" "$STAGE_ID" \
            "{\"verdict\":\"$verdict\",\"passed\":$passed,\"total\":$total}"
    fi

    if [[ "$MODE" == json ]]; then
        printf '{"verdict":"%s","passed":%d,"total":%d,"failed":%d}\n' \
            "$verdict" "$passed" "$total" "$failed"
    else
        echo ""
        echo "Validation $verdict -- $passed/$total checks passed ($failed failed)"
        echo "Report: $VALIDATION_REPORT"
    fi

    log "Phase 3 validate -- done ($verdict, $passed/$total)"
    (( failed == 0 )) || exit 1
}

run_validate
