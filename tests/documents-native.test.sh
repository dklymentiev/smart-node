#!/usr/bin/env bash
#
# The documents companion runs natively, and an existing node migrates to it.
#
# WHAT CHANGED. Doci (about a megabyte of PHP) shipped as an 801 MB image built
# on php:8.2-apache, and its MCP sidecar (one 555-line script) as a 261 MB
# image. A node installed from source built both, for minutes. documents.sh now
# installs Apache with mod_php from the distribution and the sidecar as a venv,
# as memory and planner already do, and runs both under systemd.
#
# THE PROPERTIES this test holds, each one a way the change could quietly
# regress:
#
#   1. no image is loaded, pulled or built for doci, and no compose file is
#      written: the only docker calls left remove the old containers
#   2. both services are systemd units; the app runs as www-data, which owns
#      the store bootstrap/permissions.list declares
#   3. apt runs with --no-install-recommends: the apache2 package would start
#      the distribution's Apache on :80, where Traefik already listens
#   4. the MCP sidecar binds loopback only, as the container published it
#   5. Traefik reaches the app through host.docker.internal, not a container
#      name that no longer resolves
#   6. contracts-only falls through to the full install while the containers
#      are still there: an update without a STAGE_VERSION bump runs
#      contracts-only, and would otherwise leave every existing node on the
#      containers with a config written for the native service
#   7. the migration never removes the store, the secrets or the database, and
#      removes the old images only after the native service has answered
#   8. the native callers find the service: Studio, the acceptance tool,
#      sn-version and the node status page no longer ask docker for it
#   9. the orchestrator no longer ships the image directory to the companion
#  10. validate survives the cluster default: documents was the only caller of
#      grant_pg_docker_access, so a clean install no longer sets
#      listen_addresses, and validate died reading the absent line
#  11. nothing builds, pushes, attaches or documents the doci images: no
#      build-images.sh, no IMAGE_DIGESTS, no doci-image/, no Dockerfile.mcp,
#      release.sh does not push images and install-from-release fetches none
#
#   tests/documents-native.test.sh                  # working tree, PASS
#   tests/documents-native.test.sh --baseline REF   # git:REF, expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() { # PATH -> stdout
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

# body FUNC: the body of a shell function in documents.sh, up to its closing
# brace at column 0.
body() {
    printf '%s\n' "$DOC" | awk -v f="$1" '
        $0 ~ "^" f "\\(\\) \\{" { on = 1; next }
        on && /^}/ { exit }
        on { print }'
}

has()    { if grep -qE -- "$2" <<<"$3"; then ok "$1"; else bad "$1"; fi; }
hasnot() { if grep -qE -- "$2" <<<"$3"; then bad "$1"; else ok "$1"; fi; }

if [[ -n "$BASELINE" ]]; then label="git:$BASELINE"; else label="working tree"; fi
echo "documents companion runs natively ($label)"

DOC=$(read_src companions/documents.sh)
[[ -n "$DOC" ]] || { echo "  FAIL companions/documents.sh unreadable"; exit 1; }
CODE=$(grep -vE '^[[:space:]]*#' <<<"$DOC")

# 1 -------------------------------------------------------------------------
hasnot "no image is loaded"                  'docker load|gunzip -c "\$tarball"'                "$CODE"
hasnot "no image is pulled"                  'docker pull'                                      "$CODE"
hasnot "no image is built"                   'docker build'                                     "$CODE"
hasnot "no container is started"             'docker (compose [^|]*up|run )'                    "$CODE"
hasnot "no compose file is written"          'write_compose_file'                               "$CODE"

# 2 -------------------------------------------------------------------------
has "the app is a systemd unit"              '^DOCI_UNIT="smart-node-doci\.service"'            "$DOC"
has "the sidecar is a systemd unit"          '^DOCI_MCP_UNIT="smart-node-doci-mcp\.service"'    "$DOC"
has "the app runs as www-data"               '^DOCI_USER="www-data"'                            "$DOC"
has "the app unit runs Apache in the foreground" \
    'ExecStart=/usr/sbin/apache2 -f \$\{HTTPD_CONF\} -DFOREGROUND'                              "$DOC"
has "the sidecar runs from its venv"         'ExecStart=\$\{MCP_VENV\}/bin/python'              "$DOC"
PERM=$(read_src bootstrap/permissions.list)
has "permissions.list still declares the store www-data" \
    '^/opt/smart-node/documents/files[[:space:]]+www-data[[:space:]]+www-data'                  "$PERM"

# 3 -------------------------------------------------------------------------
has "apt keeps the apache2 service package out" \
    'apt-get install[^#]*--no-install-recommends'                                               "$(body install_packages)"
has "the packages name mod_php and pdo_pgsql's provider" \
    '^DOCI_PACKAGES="[^"]*libapache2-mod-php[^"]*php-pgsql'                                     "$DOC"

# 4 -------------------------------------------------------------------------
has "the sidecar binds 127.0.0.1"            "DOCI_MCP_HOST='127\.0\.0\.1'"                     "$(body write_runtime_env)"

# 5 -------------------------------------------------------------------------
has "Traefik is pointed at host.docker.internal" \
    'url: "http://host\.docker\.internal:\$\{DOCI_HTTP_PORT\}"'                                 "$(body write_traefik_route)"
has "ufw admits the smart-node subnet to the app port" \
    'ufw allow from "\$sn_cidr" to any port "\$DOCI_HTTP_PORT"'                                 "$(body open_firewall)"

# 6 -------------------------------------------------------------------------
CO=$(body run_contracts_only)
has "contracts-only checks for the old containers" 'legacy_containers_present'                  "$CO"
has "contracts-only falls through to the full install" '^[[:space:]]*run_intent$'               "$CO"

# 7 -------------------------------------------------------------------------
MIG=$(body migrate_from_container)
[[ -n "$MIG" ]] && ok "a migration step exists" || bad "a migration step exists"
hasnot "the migration does not remove the store"   'rm[^#]*(FILES_DIR|INSTALL_DIR)'             "$MIG"
hasnot "the migration does not touch the secrets"  'DOCUMENTS_ENV'                              "$MIG"
hasnot "the migration does not drop the database"  'DROP|psql'                                  "$MIG"
INTENT=$(body run_intent)
order=$(grep -nE '^[[:space:]]*(migrate_from_container|wait_for_doci|remove_legacy_images)$' <<<"$INTENT" \
        | sed 's/^[0-9]*:[[:space:]]*//' | tr '\n' ' ')
if [[ "$order" == "migrate_from_container wait_for_doci remove_legacy_images " ]]; then
    ok "images are removed only after the native service answered"
else
    bad "images are removed only after the native service answered (order: ${order:-none})"
fi

# 8 -------------------------------------------------------------------------
has "documents.sh writes the DOCI_URL contract" '^DOCI_CONTRACT="/etc/smart-node/doci\.env"'    "$DOC"
has "Studio reads the contract first"         'sed -n .s/\^DOCI_URL=//p. /etc/smart-node/doci\.env' \
    "$(read_src companions/studio.sh)"
has "the acceptance tool reads the contract"  '/etc/smart-node/doci\.env' \
    "$(read_src tools/acceptance/returning-user.py)"
hasnot "sn-version no longer reads doci from an image" \
    '\("doci(-mcp)?",[[:space:]]+"image"'    "$(read_src node-versioning/sn-version)"
hasnot "the status page no longer asks docker for doci" \
    'docker ps --filter name=\^\{container\}' "$(read_src companions/node-ops/health-checker/node-status.py)"

# 9 -------------------------------------------------------------------------
hasnot "install-on no longer ships doci-image to the companion" \
    'run_companion documents[^#]*doci-image' "$(read_src orchestrator/install-on.sh)"

# 10 ------------------------------------------------------------------------
# documents was the only caller of grant_pg_docker_access. Without it a clean
# install leaves postgresql.conf with no listen_addresses line, and validate
# read that line through a grep under pipefail: it ended there, no report.
VAL=$(read_src validate.sh \
    | awk '/^check_pg_listen_addresses\(\) \{/ { on = 1 } on { print } on && /^}/ { exit }' \
    | grep -A1 'declared=\$(grep' | tail -n1)
has "validate survives a cluster with no listen_addresses line" '\|\| true\)$' "$VAL"

# 11 ------------------------------------------------------------------------
gone() { # LABEL PATH
    if [[ -n "$BASELINE" ]]; then
        if git -C "$REPO" cat-file -e "$BASELINE:$2" 2>/dev/null; then bad "$1"; else ok "$1"; fi
    else
        if [[ -e "$REPO/$2" ]]; then bad "$1"; else ok "$1"; fi
    fi
}
gone "tools/build-images.sh is retired"          tools/build-images.sh
gone "companions/IMAGE_DIGESTS is gone"          companions/IMAGE_DIGESTS
gone "companions/doci-image/ is gone"            companions/doci-image/README.md
gone "the doci-mcp Dockerfile is gone"           companions/doci/Dockerfile.mcp
REL=$(read_src tools/release.sh | grep -vE '^[[:space:]]*#')
hasnot "release.sh does not build or push images" 'build-images\.sh|IMAGE_DIGESTS|docker push' "$REL"
hasnot "release.sh does not require GHCR_TOKEN"   'GHCR_TOKEN'                                "$REL"
hasnot "install-from-release fetches no image"    'doci-image|SHA256SUMS|releases/assets'     "$(read_src tools/install-from-release.sh)"
DOCS=$(for f in README.md CONTRIBUTING.md docs/install-from-source.md docs/runbook-update.md \
            docs/runbook-release.md docs/install-from-github.md; do read_src "$f"; done)
hasnot "no doc tells anyone to build or load doci images" \
    'build-images\.sh|doci-image/|smart-node-doci(-mcp)?-<|--publish --assets' "$DOCS"

echo
# The runtime envs carry DOCI_DOMAIN and secrets: they live in /etc/smart-node,
# never under /opt/smart-node, where validate refuses a node domain literal.
if grep -q 'DOCI_RUNTIME_ENV="/etc/smart-node/' <<<"$DOC"    && grep -q 'MCP_RUNTIME_ENV="/etc/smart-node/' <<<"$DOC"; then
    ok "the runtime env files live in /etc/smart-node"
else
    bad "the runtime env files live in /etc/smart-node"
fi

if (( FAILED > 0 )); then
    echo "FAIL: $FAILED check(s)"
    exit 1
fi
echo "PASS"
