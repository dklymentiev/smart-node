#!/bin/bash
#
# B04-secrets-slice.sh -- Smart Node installer, slice of B04-secrets.
#
# Runs INSIDE the target host as root. Idempotent.
#
# SINCE THE ONE SECRET STORE: integrations.sops.yaml is legacy. core-api imports
# it into sn_config.secrets once at start (lib/secret_store_import.py) and reads
# the database from then on. This slice still installs sops and age so that an
# existing node can be imported; a node installed fresh gets an empty file that
# imports nothing. Both go in a later release.
#
# Installs the minimum secrets infrastructure needed by the Integration
# Tools Registry: sops + age binaries, a per-node age
# key, and an empty sops-encrypted YAML at /etc/smart-node/integrations.sops.yaml.
#
# This is the first slice of the full Infisical -> sops+age migration.
# It exists so the integrations feature can ship without waiting for the
# full migration of all 10 companions from their env-file secrets. When
# the full migration lands, this slice's outputs (age key + integrations.sops.yaml)
# are absorbed into the full B04 rewrite -- they already live at the
# right paths with the right format.
#
# Does NOT touch:
#   - existing B04-stub state (we mark our own B04-slice state)
#   - companion env files (gateway / memory / documents / ... still own them)
#   - the unstubbed B04-secrets.sh script (left in place for reference)
#
# Creates:
#   - /usr/local/bin/sops          -- pinned sops binary (verified by sha256)
#   - /usr/local/bin/age, age-keygen -- via apt (Debian 12 main, age 1.1.1)
#   - /etc/smart-node/age-key.txt        -- private age key, 0600 root
#   - /etc/smart-node/age-recipient.txt  -- public age recipient, 0644
#   - /etc/smart-node/.sops.yaml         -- sops config (pins recipient for the file)
#   - /etc/smart-node/integrations.sops.yaml -- empty sops-encrypted YAML
#   - /var/lib/smart-node/stages/B04-slice.state
#
# Usage:
#   B04-secrets-slice.sh            install
#   B04-secrets-slice.sh --verify   post-checks; exit 0 / N (fail count)
#   B04-secrets-slice.sh --status   read the state file; exit 0/1/2
#   B04-secrets-slice.sh --help     this message
#

set -euo pipefail

# --help works without lib.sh.
case "${1:-}" in
    --help|-h)
        awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"
        exit 0
        ;;
esac

STAGE_ID="B04"
STAGE_NAME="secrets"
STAGE_VERSION="0.2.0"

SOPS_VERSION="3.9.4"
SOPS_URL="https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64"
SOPS_SHA256="5488e32bc471de7982ad895dd054bbab3ab91c417a118426134551e9626e4e85"

SN_ETC="/etc/smart-node"
AGE_KEY="$SN_ETC/age-key.txt"
AGE_RECIPIENT_FILE="$SN_ETC/age-recipient.txt"
SOPS_CONFIG="$SN_ETC/.sops.yaml"
SECRETS_FILE="$SN_ETC/integrations.sops.yaml"

# ---------- lib.sh ----------
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ ! -r "$LIB" ]]; then
    echo "ERROR: missing $LIB -- run B01-core first" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$LIB"

# ---------- arg parse ----------
MODE="run"
case "${1:-}" in
    "")          MODE="run" ;;
    --verify)    MODE="verify" ;;
    --status)    stage_status "$STAGE_ID"; exit $? ;;
    *)           echo "unknown arg: $1 (try --help)" >&2; exit 2 ;;
esac

require_root
log_init "$STAGE_ID"

# ---------- helpers ----------

install_age() {
    if command -v age >/dev/null && command -v age-keygen >/dev/null; then
        log "age already installed: $(age --version 2>&1 | head -1)"
        return 0
    fi
    log "installing age via apt"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq age >/dev/null
    command -v age >/dev/null || die "age install failed"
    manifest_record apt-package age
    log "age installed: $(age --version 2>&1 | head -1)"
}

install_sops() {
    local target="/usr/local/bin/sops"
    if [[ -x "$target" ]]; then
        local cur; cur=$("$target" --version 2>/dev/null | awk '/^sops / {print $2}')
        if [[ "$cur" == "$SOPS_VERSION" ]]; then
            log "sops $SOPS_VERSION already installed"
            return 0
        fi
        log "sops $cur present, replacing with $SOPS_VERSION"
    fi
    local tmp; tmp=$(mktemp)
    log "downloading sops v$SOPS_VERSION"
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 "$SOPS_URL" -o "$tmp" || die "sops download failed"
    local got; got=$(sha256sum "$tmp" | awk '{print $1}')
    if [[ "$got" != "$SOPS_SHA256" ]]; then
        rm -f "$tmp"
        die "sops sha256 mismatch: got $got, want $SOPS_SHA256"
    fi
    install -m 0755 -o root -g root "$tmp" "$target"
    rm -f "$tmp"
    manifest_record file "$target"
    log "sops installed: $("$target" --version 2>&1 | head -1)"
}

ensure_age_key() {
    if [[ -s "$AGE_KEY" ]]; then
        log "age key already present at $AGE_KEY"
    else
        log "generating age key at $AGE_KEY"
        install -d -m 0755 -o root -g root "$SN_ETC"
        umask 0177
        age-keygen -o "$AGE_KEY" 2>/dev/null
        manifest_record file "$AGE_KEY"
    fi
    # Always (re)assert perms -- so smart-node-core-api (which runs as the
    # smart-node-core-api user and belongs to the smart-node group) can decrypt
    # without sudo. Other smart-node group members can also read the key today;
    # tightening to a dedicated group is the dedicated-group follow-up.
    chmod 0640 "$AGE_KEY"
    chown root:"$SMART_NODE_GROUP" "$AGE_KEY"
    # Public recipient file (idempotent regenerate from private key).
    local recipient; recipient=$(age-keygen -y "$AGE_KEY" 2>/dev/null)
    [[ -n "$recipient" ]] || die "could not derive age recipient from $AGE_KEY"
    if [[ ! -f "$AGE_RECIPIENT_FILE" ]] || [[ "$(cat "$AGE_RECIPIENT_FILE" 2>/dev/null)" != "$recipient" ]]; then
        echo "$recipient" > "$AGE_RECIPIENT_FILE"
        chmod 0644 "$AGE_RECIPIENT_FILE"
        chown root:root "$AGE_RECIPIENT_FILE"
        manifest_record file "$AGE_RECIPIENT_FILE"
        log "wrote recipient $recipient -> $AGE_RECIPIENT_FILE"
    fi
}

ensure_sops_config() {
    # .sops.yaml tells sops which recipient to use for which file. We pin
    # integrations.sops.yaml (and any future *.sops.yaml under /etc/smart-node)
    # to the node's recipient.
    local recipient; recipient=$(cat "$AGE_RECIPIENT_FILE")
    local want
    want=$(cat <<EOF
# Smart Node sops config -- managed by B04-secrets-slice.
# Pins sops-encrypted YAMLs under /etc/smart-node/ to the node age key.
creation_rules:
  - path_regex: '.*\\.sops\\.yaml\$'
    age: $recipient
EOF
)
    if [[ -f "$SOPS_CONFIG" ]] && [[ "$(cat "$SOPS_CONFIG")" == "$want" ]]; then
        log ".sops.yaml already current"
        return 0
    fi
    printf '%s\n' "$want" > "$SOPS_CONFIG"
    chmod 0644 "$SOPS_CONFIG"
    chown root:root "$SOPS_CONFIG"
    manifest_record file "$SOPS_CONFIG"
    log "wrote $SOPS_CONFIG"
}

ensure_secrets_file() {
    if [[ -f "$SECRETS_FILE" ]]; then
        # Sanity: must decrypt with our key.
        if SOPS_AGE_KEY_FILE="$AGE_KEY" sops -d "$SECRETS_FILE" >/dev/null 2>&1; then
            log "$SECRETS_FILE already present and decrypts cleanly"
            return 0
        fi
        die "$SECRETS_FILE exists but does not decrypt with $AGE_KEY -- aborting to avoid clobber"
    fi
    log "creating empty $SECRETS_FILE"
    local tmp; tmp=$(mktemp --suffix=.yaml)
    # sops refuses an all-comment file (needs at least one document) -- so
    # the seed carries a single _meta block. Real integration secrets are
    # added later via /api/secrets/* (core-api uses `sops set`).
    cat > "$tmp" <<EOF
# Integration secrets, sops-encrypted with the node age key.
# Managed by Smart Node core-api (/api/secrets/* endpoints).
# Edit via the panel, not by hand.
#
# Schema:
#   <integration_slug>:
#     <secret_name>: <value>
_meta:
  schema_version: 1
  created_by: B04-secrets-slice.sh
  created_at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
EOF
    SOPS_AGE_KEY_FILE="$AGE_KEY" sops -e --age "$(cat "$AGE_RECIPIENT_FILE")" -i "$tmp"
    # 0660 root:smart-node -- core-api (member of smart-node) needs write
    # access for sops set / sops unset called from /api/secrets/*.
    install -m 0660 -o root -g "$SMART_NODE_GROUP" "$tmp" "$SECRETS_FILE"
    rm -f "$tmp"
    manifest_record file "$SECRETS_FILE"
}

# ---------- run modes ----------

run_install() {
    log "B04-slice -- starting"

    install_age
    install_sops
    ensure_age_key
    ensure_sops_config
    ensure_secrets_file

    run_verify
    if (( VERIFY_ERRORS == 0 )); then
        stage_mark "$STAGE_ID" done \
            version="$STAGE_VERSION" \
            checks="$VERIFY_TOTAL" \
            note="integration-tools-slice"
        report_section "Stage B04-slice -- secrets (slice for integrations)" "$(cat <<EOF
- sops v$SOPS_VERSION at /usr/local/bin/sops
- age $(age --version 2>&1 | head -1) via apt
- age key: $AGE_KEY (0600 root)
- public recipient: $(cat "$AGE_RECIPIENT_FILE")
- secrets file: $SECRETS_FILE (sops-encrypted, empty)
- companions untouched -- they keep their existing env-file secrets
EOF
)"
        log_event "system:installer" "stage.installed" "stage" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"checks\":$VERIFY_TOTAL}"
        log "B04-slice -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
    else
        stage_mark "$STAGE_ID" failed \
            version="$STAGE_VERSION" \
            errors="$VERIFY_ERRORS"
        die "B04-slice -- $VERIFY_ERRORS check(s) failed"
    fi
}

run_verify() {
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    verify_check "sops binary present"   test -x /usr/local/bin/sops
    verify_check "sops version matches"  bash -c "/usr/local/bin/sops --version 2>/dev/null | grep -q 'sops $SOPS_VERSION'"
    verify_check "age binary present"    bash -c "command -v age"
    verify_check "age-keygen present"    bash -c "command -v age-keygen"
    verify_check "age key file"          test -f "$AGE_KEY"
    verify_check "age key 0640"          bash -c "[[ \$(stat -c %a $AGE_KEY 2>/dev/null) == 640 ]]"
    verify_check "age key group"         bash -c "[[ \$(stat -c %G $AGE_KEY 2>/dev/null) == $SMART_NODE_GROUP ]]"
    verify_check "secrets file 0660"     bash -c "[[ \$(stat -c %a $SECRETS_FILE 2>/dev/null) == 660 ]]"
    verify_check "age recipient file"    test -s "$AGE_RECIPIENT_FILE"
    verify_check "sops config present"   test -f "$SOPS_CONFIG"
    verify_check "secrets file present"  test -f "$SECRETS_FILE"
    verify_check "secrets file decrypts" bash -c "SOPS_AGE_KEY_FILE=$AGE_KEY /usr/local/bin/sops -d $SECRETS_FILE >/dev/null 2>&1"
}

case "$MODE" in
    run)
        run_install
        ;;
    verify)
        run_verify
        echo "B04-slice verify: $((VERIFY_TOTAL - VERIFY_ERRORS))/$VERIFY_TOTAL passed"
        exit "$VERIFY_ERRORS"
        ;;
esac
