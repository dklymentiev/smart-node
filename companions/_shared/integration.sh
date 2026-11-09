#!/bin/bash
#
# _shared/integration.sh -- companion integration helpers.
#
# Each companion that exposes agent-usable capabilities (Planner, Mesh,
# Doci, Screenbox, Rein, brin, ...) calls sn_integration_register at the
# end of its successful install, and sn_integration_unregister on
# uninstall. The helper POSTs to the smart-node-core-api which owns the
# sn_config.integrations registry.
#
# This is a thin wrapper around `curl`. Failures are non-fatal -- the
# companion install proceeds even if the registry is unreachable (e.g.
# core-api not yet installed on this node, or transient outage). The
# situation is logged so an operator can re-run later.
#
# Updated: capabilities + MCP server registration (documents.sh v0.3.0).
#
# Usage:
#   source "/usr/local/lib/smart-node/companions/_shared/integration.sh"
#
#   sn_integration_register \
#     --slug         planner \
#     --label        "Planner" \
#     --purpose      "Tasks" \
#     --kind         app \
#     --status       available \
#     --install-uri  "http://planner.${SN_HOST:-localhost}" \
#     --install-path "/opt/smart-node/tracking" \
#     --description  "Task and objective management." \
#     --companion-slug tracking \
#     [--capabilities '["knowledge"]'] \
#     [--tools-json /path/to/tools.json]
#
#   sn_mcp_server_register \
#     --slug      doci-mcp \
#     --name      "Doci MCP" \
#     --transport http \
#     --url       "http://localhost:8200/mcp"
#
#   sn_integration_unregister --slug planner
#   sn_mcp_server_unregister  --slug doci-mcp
#
# Endpoint and identity:
#   Companions run on the host as root and reach the controller over its unix
#   socket, where the kernel names the caller. The controller records each
#   action under the account it resolved for that caller.

# Already sourced? -- bail out so duplicate `source` calls are no-ops.
[[ -n "${SN_INTEGRATION_SH_LOADED:-}" ]] && return 0
SN_INTEGRATION_SH_LOADED=1

# pull in the node domain from the routing contract so callers can
# spell install_uri as "http://X.${SMART_NODE_DOMAIN}". Source routing.env
# first (the canonical source); fall back to node.env for
# compatibility with nodes that pre-date routing.env (pre-B03 state).
# shellcheck source=/dev/null
[[ -f /etc/smart-node/routing.env ]] && source /etc/smart-node/routing.env
[[ -z "${SMART_NODE_DOMAIN:-}" && -f /etc/smart-node/node.env ]] && source /etc/smart-node/node.env

# Registration goes over core-api's unix socket. The installer runs as root
# and has no account to name: on the socket the kernel names the caller, and
# core-api admits the node's own root account as the owner. A name typed in a
# header over TCP proves nothing and is refused.
SN_CORE_API_SOCKET="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
SN_CORE_API_TIMEOUT="${SN_CORE_API_TIMEOUT:-5}"

# Tiny log shim -- prefer the lib.sh `log` if available, else stderr.
_sn_intg_log() {
    if declare -F log >/dev/null; then
        log "$*"
    else
        echo "$(date +%H:%M:%S) [integration] $*" >&2
    fi
}

# Internal: POST/DELETE and return "<body>\n<http_code>".
_sn_intg_curl() {
    # The fourth argument named the caller in a header. It is accepted and
    # unused: the socket names the caller, and a header there is ignored.
    local method="$1" path="$2" body="${3:-}"
    local url="http://core-api${path}"
    local resp_file http
    resp_file="$(mktemp)"
    if [[ "$method" == "GET" || "$method" == "DELETE" ]]; then
        http=$(curl -sS -o "$resp_file" -w '%{http_code}' \
                    -m "$SN_CORE_API_TIMEOUT" \
                    --unix-socket "$SN_CORE_API_SOCKET" \
                    -X "$method" \
                    "$url" 2>/dev/null) || http=000
    else
        http=$(curl -sS -o "$resp_file" -w '%{http_code}' \
                    -m "$SN_CORE_API_TIMEOUT" \
                    --unix-socket "$SN_CORE_API_SOCKET" \
                    -X "$method" \
                    -H "Content-Type: application/json" \
                    --data "$body" \
                    "$url" 2>/dev/null) || http=000
    fi
    cat "$resp_file"
    printf '\n%s\n' "$http"
    rm -f "$resp_file"
}

# Build a JSON payload for POST /api/integrations.
_sn_intg_build_payload() {
    local slug="" label="" purpose="" description="" kind="app" status="available"
    local install_uri="" install_path="" companion_slug="" tools_json=""
    local capabilities="[]"
    # Base URL this integration's `api` tools are CALLED on (not the browsable
    # install-uri). An api tool without it is advertised and uncallable.
    local endpoint=""
    while (( $# > 0 )); do
        case "$1" in
            --slug)            slug="$2";            shift 2 ;;
            --label)           label="$2";           shift 2 ;;
            --purpose)         purpose="$2";         shift 2 ;;
            --description)     description="$2";     shift 2 ;;
            --kind)            kind="$2";            shift 2 ;;
            --status)          status="$2";          shift 2 ;;
            --install-uri)     install_uri="$2";     shift 2 ;;
            --install-path)    install_path="$2";    shift 2 ;;
            --endpoint)        endpoint="$2";        shift 2 ;;
            --companion-slug)  companion_slug="$2";  shift 2 ;;
            --tools-json)      tools_json="$2";      shift 2 ;;
            --capabilities)    capabilities="$2";    shift 2 ;;
            *) _sn_intg_log "unknown arg: $1"; return 2 ;;
        esac
    done
    [[ -z "$slug"  ]] && { _sn_intg_log "missing --slug";  return 2; }
    [[ -z "$label" ]] && { _sn_intg_log "missing --label"; return 2; }
    python3 - "$slug" "$label" "$purpose" "$description" "$kind" "$status" \
                       "$install_uri" "$install_path" "$companion_slug" \
                       "$tools_json" "$capabilities" "$endpoint" <<'PY'
import json, sys, pathlib
slug, label, purpose, desc, kind, status, uri, path, comp, tools_path, caps_json, endpoint = sys.argv[1:13]
payload = {
    "slug": slug,
    "label": label,
    "purpose": purpose,
    "description": desc,
    "kind": kind,
    "status": status,
    "install_uri": uri,
    "install_path": path,
    "endpoint": endpoint,
    "companion_slug": comp or None,
}
# capabilities: JSON array string (e.g. '["knowledge"]')
try:
    payload["capabilities"] = json.loads(caps_json)
except (json.JSONDecodeError, ValueError):
    payload["capabilities"] = []
# tools: from a file path
if tools_path:
    p = pathlib.Path(tools_path)
    if p.is_file():
        try:
            payload["tools"] = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[integration] tools-json invalid: {e}\n")
print(json.dumps(payload))
PY
}

# Public: register (upsert) an integration with the core-api.
sn_integration_register() {
    local slug=""
    local i
    for ((i=1; i<=$#; i++)); do
        if [[ "${!i}" == "--slug" ]]; then
            local j=$((i+1)); slug="${!j}"; break
        fi
    done
    local actor="installer:${slug:-unknown}"

    local payload http body
    payload="$(_sn_intg_build_payload "$@")" || {
        _sn_intg_log "sn_integration_register: invalid args"
        return 1
    }

    body="$(_sn_intg_curl POST /api/integrations "$payload" "$actor" 2>&1)" || true
    http="$(printf '%s' "$body" | tail -n 1)"

    case "$http" in
        200|201)
            _sn_intg_log "integration '$slug' registered (HTTP $http)"
            return 0
            ;;
        000)
            _sn_intg_log "WARN integration '$slug' NOT registered -- core-api unreachable ($SN_CORE_API_SOCKET)"
            return 11
            ;;
        *)
            _sn_intg_log "WARN integration '$slug' NOT registered -- HTTP $http"
            _sn_intg_log "body: $(printf '%s' "$body" | head -c 400)"
            return 12
            ;;
    esac
}

# Public: soft-delete an integration on companion uninstall.
sn_integration_unregister() {
    local slug="" hard="false"
    while (( $# > 0 )); do
        case "$1" in
            --slug)  slug="$2";   shift 2 ;;
            --hard)  hard="true"; shift   ;;
            *) _sn_intg_log "unknown arg: $1"; return 2 ;;
        esac
    done
    [[ -z "$slug" ]] && { _sn_intg_log "missing --slug"; return 2; }
    local actor="installer:$slug"
    local path="/api/integrations/$slug"
    [[ "$hard" == "true" ]] && path="${path}?hard=true"

    local body http
    body="$(_sn_intg_curl DELETE "$path" "" "$actor" 2>&1)" || true
    http="$(printf '%s' "$body" | tail -n 1)"

    case "$http" in
        200)
            _sn_intg_log "integration '$slug' unregistered (HTTP 200)"
            return 0
            ;;
        404)
            _sn_intg_log "integration '$slug' already absent (HTTP 404)"
            return 0
            ;;
        000)
            _sn_intg_log "WARN integration '$slug' unregister: core-api unreachable"
            return 11
            ;;
        *)
            _sn_intg_log "WARN integration '$slug' unregister: HTTP $http"
            return 12
            ;;
    esac
}

# Public: register (upsert) an MCP server with the core-api.
#
# sn_mcp_server_register \
#   --slug      doci-mcp \
#   --name      "Doci MCP" \
#   --transport http \
#   --url       "http://localhost:8200/mcp"
#
# For transport=stdio, use --command and --args instead of --url.
sn_mcp_server_register() {
    local slug="" name="" transport="sse" url="" command="" args="[]"
    local autoinstall="false" default_on="false"
    # Pattern A provisioning metadata (optional; stored in config_jsonb alongside
    # the transport fields). Pass --package-name + --version-pin + --package-source
    # to enable `integration provision <slug>` for this server.
    local package_name="" version_pin="" package_source=""
    while (( $# > 0 )); do
        case "$1" in
            --slug)           slug="$2";           shift 2 ;;
            --name)           name="$2";           shift 2 ;;
            --transport)      transport="$2";      shift 2 ;;
            --url)            url="$2";            shift 2 ;;
            --command)        command="$2";        shift 2 ;;
            --args)           args="$2";           shift 2 ;;
            --autoinstall)    autoinstall="true";  shift   ;;
            --default-on)     default_on="true";   shift   ;;   # in every agent's .mcp.json
            --package-name)   package_name="$2";   shift 2 ;;
            --version-pin)    version_pin="$2";    shift 2 ;;
            --package-source) package_source="$2"; shift 2 ;;
            *) _sn_intg_log "sn_mcp_server_register: unknown arg: $1"; return 2 ;;
        esac
    done
    [[ -z "$slug" ]] && { _sn_intg_log "missing --slug"; return 2; }
    [[ -z "$name" ]] && name="$slug"

    local config_json
    case "$transport" in
        sse|http)
            [[ -z "$url" ]] && { _sn_intg_log "sn_mcp_server_register: --url required for transport=$transport"; return 2; }
            config_json=$(python3 -c "import json,sys; print(json.dumps({'url': sys.argv[1]}))" "$url")
            ;;
        stdio)
            [[ -z "$command" ]] && { _sn_intg_log "sn_mcp_server_register: --command required for transport=stdio"; return 2; }
            config_json=$(python3 -c "import json,sys; print(json.dumps({'command': sys.argv[1], 'args': json.loads(sys.argv[2])}))" "$command" "$args")
            ;;
        *)
            _sn_intg_log "sn_mcp_server_register: unknown transport '$transport'"; return 2 ;;
    esac

    # Merge Pattern A provisioning keys into config_json when supplied.
    if [[ -n "$package_name" ]]; then
        config_json=$(python3 -c "
import json, sys
cfg = json.loads(sys.argv[1])
cfg['package_name'] = sys.argv[2]
if sys.argv[3]: cfg['version_pin']    = sys.argv[3]
if sys.argv[4]: cfg['package_source'] = sys.argv[4]
print(json.dumps(cfg))
" "$config_json" "$package_name" "$version_pin" "$package_source")
    fi

    local payload
    payload=$(python3 -c "
import json, sys
print(json.dumps({
    'slug':        sys.argv[1],
    'name':        sys.argv[2],
    'transport':   sys.argv[3],
    'config':      json.loads(sys.argv[4]),
    'enabled':     True,
    'autoinstall': sys.argv[5] == 'true',
    'default_on':  sys.argv[6] == 'true',
}))" "$slug" "$name" "$transport" "$config_json" "$autoinstall" "$default_on")

    local body http
    body="$(_sn_intg_curl POST /api/mcp/servers "$payload" "installer:$slug" 2>&1)" || true
    http="$(printf '%s' "$body" | tail -n 1)"

    case "$http" in
        200|201)
            _sn_intg_log "MCP server '$slug' registered (HTTP $http)"
            return 0
            ;;
        000)
            _sn_intg_log "WARN MCP server '$slug' NOT registered -- core-api unreachable"
            return 11
            ;;
        *)
            _sn_intg_log "WARN MCP server '$slug' NOT registered -- HTTP $http"
            _sn_intg_log "body: $(printf '%s' "$body" | head -c 400)"
            return 12
            ;;
    esac
}

# Public: disable an MCP server on companion uninstall.
sn_mcp_server_unregister() {
    local slug=""
    while (( $# > 0 )); do
        case "$1" in
            --slug) slug="$2"; shift 2 ;;
            *) _sn_intg_log "sn_mcp_server_unregister: unknown arg: $1"; return 2 ;;
        esac
    done
    [[ -z "$slug" ]] && { _sn_intg_log "missing --slug"; return 2; }

    # Soft-disable: PATCH enabled=false. The server row stays for traceability.
    local payload='{"enabled": false}'
    local body http
    body="$(_sn_intg_curl POST /api/mcp/servers "$payload" "installer:$slug" 2>&1)" || true
    # Fall back to DELETE if PATCH unavailable (forward-compat).
    body="$(_sn_intg_curl DELETE "/api/mcp/servers/$slug" "" "installer:$slug" 2>&1)" || true
    http="$(printf '%s' "$body" | tail -n 1)"
    case "$http" in
        200|204|404)
            _sn_intg_log "MCP server '$slug' unregistered (HTTP $http)"
            return 0
            ;;
        000)
            _sn_intg_log "WARN MCP server '$slug' unregister: core-api unreachable"
            return 11
            ;;
        *)
            _sn_intg_log "WARN MCP server '$slug' unregister: HTTP $http"
            return 12
            ;;
    esac
}

# Public: register a RUNTIME (agent engine) with the core-api.
sn_runtime_register() {
    local slug=""
    local i
    for ((i=1; i<=$#; i++)); do
        if [[ "${!i}" == "--slug" ]]; then
            local j=$((i+1)); slug="${!j}"; break
        fi
    done
    local actor="installer:${slug:-unknown}"

    local payload body http
    payload="$(_sn_intg_build_payload "$@")" || {
        _sn_intg_log "sn_runtime_register: invalid args"
        return 1
    }
    body="$(_sn_intg_curl POST /api/runtimes "$payload" "$actor" 2>&1)" || true
    http="$(printf '%s' "$body" | tail -n 1)"

    case "$http" in
        200|201) _sn_intg_log "runtime '$slug' registered (HTTP $http)"; return 0 ;;
        000)     _sn_intg_log "WARN runtime '$slug' NOT registered -- core-api unreachable"; return 11 ;;
        *)       _sn_intg_log "WARN runtime '$slug' NOT registered -- HTTP $http"
                 _sn_intg_log "body: $(printf '%s' "$body" | head -c 400)"; return 12 ;;
    esac
}

sn_runtime_unregister() {
    local slug="" hard="false"
    while (( $# > 0 )); do
        case "$1" in
            --slug) slug="$2"; shift 2 ;;
            --hard) hard="true"; shift ;;
            *) _sn_intg_log "unknown arg: $1"; return 2 ;;
        esac
    done
    [[ -z "$slug" ]] && { _sn_intg_log "missing --slug"; return 2; }
    local actor="installer:$slug"
    local path="/api/runtimes/$slug"
    [[ "$hard" == "true" ]] && path="${path}?hard=true"
    local body http
    body="$(_sn_intg_curl DELETE "$path" "" "$actor" 2>&1)" || true
    http="$(printf '%s' "$body" | tail -n 1)"
    case "$http" in
        200) _sn_intg_log "runtime '$slug' unregistered (HTTP 200)"; return 0 ;;
        404) _sn_intg_log "runtime '$slug' already absent (HTTP 404)"; return 0 ;;
        000) _sn_intg_log "WARN runtime '$slug' unregister: core-api unreachable"; return 11 ;;
        *)   _sn_intg_log "WARN runtime '$slug' unregister: HTTP $http"; return 12 ;;
    esac
}
