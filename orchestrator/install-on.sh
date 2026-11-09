#!/bin/bash
#
# install-on.sh -- Smart Node end-to-end installer.
#
# Sequences Layer P (provision-*) + Layer A (configure-host) +
# Layer C bootstrap stage B01 (bootstrap/B01-core) into one command.
# The single entry point an operator or wizard backend calls.
#
# Usage:
# Tip: run tools/preflight.sh --provider <p> [...] first -- it catches host
# problems (missing LXD, Docker FORWARD DROP, low RAM) before this starts.
#
#   install-on.sh --provider lxd-local \
#                 --name NAME --ip IP --ssh-key /path/pub \
#                 [--allow-from CIDR] [--json]
#
# Required:
#   --provider          lxd-local | existing (others as added)
#                       existing = a machine you already have (bare metal, VPS,
#                       or an LXC/VM you made yourself); nothing is created
#   --name              short identifier for the node; also the node VPN
#                       name -- Layer A A6 gives the node the FQDN
#                       <name>.sn.vpn by default (must be a lowercase DNS
#                       label), unless --node-fqdn overrides it
#   --ip                target IP (provider-specific format)
#   --ssh-user USER     SSH user on the target (provider default; existing=admin)
#   --ssh-key           path to PUBLIC SSH key; private = same path minus .pub
#
# Optional:
#   --disk SIZE         root disk for Layer P (e.g. 20GB). Omitted -> the
#                       provisioner default. --all loads several GB of
#                       component images; size the node accordingly.
#   --allow-from CIDR   SSH inbound CIDR for Layer A UFW. Default: derived from
#                       the controller own source address toward the node, so the
#                       installer can never firewall itself out of the machine.
#   --ssh-port PORT     SSH port on the target (default: 22). Pass when the
#                       target is on a non-standard port. Layer A opens this
#                       port in UFW and connects through it.
#   --node-fqdn DOMAIN  override the node FQDN (default: <name>.sn.vpn). Threaded
#                       to Layer A; becomes the node hostname, so every Traefik
#                       Host rule (panel./auth./voice./doci.) uses it. Point your
#                       own DNS/hosts at the node IP. Usually paired with --no-vpn.
#   --no-vpn            skip Layer A A6 (WireGuard + wildcard dnsmasq DNS + phone
#                       QR). The node FQDN/hostname is still set so routing works.
#                       For a plain LAN node; typically with --node-fqdn. Default:
#                       A6 runs (WireGuard VPN + <name>.sn.vpn wildcard).
#   --subnet CIDR       (lxd-local) target subnet for --ip validation + gateway
#                       derivation. Default: 10.0.100.0/24. Ignored by 'existing'.
#   --network NAME      (lxd-local) LXD bridge to attach. Default: lxdbr0.
#                       Ignored by 'existing'.
#   --storage-pool NAME (lxd-local) LXD storage pool. Default: default.
#                       Ignored by 'existing'.
#   --vpn-endpoint H:P  host:port the onboarding phone uses to reach the
#                       node's WireGuard (Layer A A6). Default <node-ip>:51820;
#                       pass it for a NAT'd node whose IP is not reachable.
#   --timezone ZONE     host zone, IANA name (Layer A). Omitted -> the host
#                       keeps its zone; the node's "local time" is the host's.
#   --node-domain DOM   node domain -> B03 issues a real Let's Encrypt cert
#                       (ACME HTTP-01); omitted -> self-signed bootstrap cert
#   --acme-email ADDR   ACME registration email (used only with --node-domain)
#   --public-domain DOM the name the OUTSIDE world uses for this node, when it
#                       differs from --node-domain. The sign-in identity (auth
#                       URL, WebAuthn relying party, cookie domain) is built
#                       from this one, and the public auth host is routed to
#                       Authum. Pass it ONCE -- it is recorded in the routing
#                       contract and carried forward by later runs, so a
#                       routine update no longer resets a node's public face
#                       to its internal name. Pass '-' to drop it.
#   --ai WAY            how the AI will work: the runtime of a subscription
#                       (agent-runtimes = Claude subscription) or "key" (an API key,
#                       entered on the first-run page). Installs every module but the
#                       subscription runtimes not chosen; Flint always. A subscription
#                       is signed in at the terminal. Omitted at a terminal: the
#                       installer asks. Omitted with no terminal: every module.
#   --first-run-link-file F  on a fresh node with a setup token: write the first-run
#                       link (token included) to F, mode 600, for tools/web-install to
#                       open. Without it the link stays off this log, as before.
#   --components A,B    with --all or --companions-only: install these modules, what
#                       they require and the core, nothing else (overrides --ai). Names
#                       are in companions/*.manifest.json. Omitted with no terminal and
#                       no --ai: every module, as before.
#   --bootstrap-only    run Phase 1 bootstrap only, stop at the login screen
#   --all               run Phase 1 + Phase 2 companions + validate (headless)
#   --companions-only   skip Phase 1 (Layer A + B01-B06); run Phase 2 companions
#                       and Phase 3 validate only. Use after provision from a
#                       golden stand image (provision/build-stand-golden.sh)
#                       that already has the bootstrap complete. Refuses if
#                       B01.state is absent on the target. Accepts the same
#                       companion credential flags as --all.
#   --hitl-origin URL  install the optional native HITL decision service at
#                       this exact HTTPS origin (WebAuthn passkeys bind to it).
#                       On later runs, an installed HITL companion is updated
#                       using its saved origin if this option is omitted.
#   --hitl-adopt-legacy Explicitly stop and adopt a legacy Docker HITL after
#                       verified backup of SQLite and both caller states.
#                       Requires --hitl-origin; rollback is a companion mode.
#   --anthropic-key KEY Anthropic API key, threaded to the gateway companion
#                       (--all / --companions-only). Omitted -> the gateway installs
#                       degraded; a later re-run can configure it.
#   --openrouter-key KEY OpenRouter API key, threaded to the gateway companion
#                       (--all only). brin's default model is openrouter/...,
#                       so a node expected to answer needs this or --anthropic-key.
#   --secrets-operator-users CSV
#                       Existing OS users for node-owned service MCPs that may
#                       resolve credentials through the core-api Unix socket.
#                       Example: sn-google-ads-mcp,sn-analytics-mcp. The installer
#                       renders socket-group membership and the core-api allow
#                       list from this one declaration. Omitted on update means
#                       preserve the node's existing declaration; new nodes
#                       default to root only.
#   --gemini-key KEY    Gemini API key, threaded to the voice companion
#                       (--all only). Omitted -> voice installs degraded
#                       (browser UI loads, live conversation fails).
#   --tg-token TOKEN    Telegram bot token for the communication companion
#                       (--all only). Both --tg-token and --tg-channel-id
#                       must be supplied together, or the communication
#                       companion is skipped (install it later from B06).
#   --tg-channel-id ID  Telegram bot identity for the communication companion
#                       (--all only).
#   --mail-host HOST    external mailserver the node reads its boxes from
#                       (--all only). This is the topology both real nodes
#                       run: the node does not host mail, the MX target does,
#                       because that host holds the valid PTR/cert/DKIM.
#                       Sufficient on its own to wire the communication
#                       companion -- Telegram is NOT required.
#   --mail-imap-host H  read from this name instead of --mail-host, and
#   --mail-smtp-host H  send through this one. Most estates publish separate
#                       service names (imap./smtp.) and the companion has always
#                       supported the split; before these flags every node had
#                       to be corrected by hand after install. Omitted,
#                       both fall back to --mail-host.
#   --mail-account ADDR   a single mailbox on that host, and
#   --mail-password PW    its password
#   --mail-accounts-file F  a file of PW_<addr> lines (several mailboxes).
#                       Read on the CONTROLLER and pushed to the node.
#                       Give either the file or account+password.
#   --notify-email-sender ADDR  real configured mailbox used for system/owner
#                       alerts (including budget exhaustion). Required when a
#                       multi-mailbox fresh install cannot yet infer one from
#                       agent bindings; defaults to --mail-account for a single
#                       mailbox. The address must have a supplied credential.
#   --owner-password PW REQUIRED for --all/--bootstrap-only unless
#                       --setup-via-token is given: seed the super-admin (admin)
#                       password into Authum at
#                       install (B06), so the node is born able to log in with
#                       no manual /setup step. Absent it, B06 emits a one-time
#                       /setup token and the operator sets the password in the
#                       panel. Idempotent: an existing operator is preserved.
#   --force             (--provider existing) proceed on a machine that already
#                       carries Smart Node stages. This is how a node is
#                       UPDATED: re-running the installer over an installed node
#                       is the update mechanism, and the provisioner otherwise
#                       refuses. Skips one guard; destroys nothing. Not valid
#                       for lxd-local, whose --recreate DELETES the container.
#   --force-onboarding-reseed  intentionally replace the setup agent's grants
#                       with the release baseline. Ordinary updates merge the
#                       baseline and preserve additional or wider grants.
#   --set-identity      On an update (B01.state already on the node), allow
#                       identity changes: hostname rename, new --node-domain,
#                       new --public-domain. By default an update preserves
#                       the existing identity and routing.
#                       Pass --set-identity when you intend to rename the node
#                       or change its TLS domain. On a first install this flag
#                       has no effect.
#   --seed-manifest PATH  optional JSON file specifying credentials or the word
#                         "disconnected" for each channel/service. Overrides the
#                         corresponding CLI flags. node-provision passes one to
#                         enforce credential isolation at provision time.
#                         Fields: telegram, email, voice, anthropic, openrouter,
#                         node_fqdn, no_vpn. See docs/seed-manifest.md.
#   --json              print machine-parseable JSON on success
#   --help              this message
#
# Run modes (rev 5 canon -- see installer-stages.md):
#   default           Phase 1 bootstrap, then hand off to the web interface
#   --bootstrap-only  Phase 1 bootstrap only
#   --all             Phase 1 + Phase 2 + validate, fully headless
#   --companions-only Phase 2 + validate only; requires bootstrap already done
# --bootstrap-only, --all and --companions-only are mutually exclusive.
#
# --all runs the Phase 2 companions in dependency order
# (memory -> core-api -> gateway -> documents -> agent-runtimes ->
# tracking -> dispatcher -> workflows -> communication -> voice), then
# Phase 3 validate.sh. core-api goes up before gateway: the schema is baked by
# B02 and it serves the panel write-side, and both read sn_core. memory goes up
# before core-api, because core-api writes that container's address into its
# own unit at install time and cannot invent it later.
# Each companion is pushed with its vendored
# sidecar(s), run, and --verify'd -- the same flow as B01..B06. The
# communication companion needs at least ONE channel -- Telegram
# (--tg-token + --tg-channel-id) or an external mailserver (--mail-host with
# credentials). With neither it is skipped and the other 9 still install.
#
# Exit codes:
#   0  full success (A.state + B01.state + B02.state all done)
#   1  Layer P failed
#   2  Layer A failed (probe / configure)
#   3  Layer C bootstrap stage failed (B01..B06)
#   4  bad args
#   5  Phase 2 companion failed (--all)
#   6  Phase 3 validate reported failures (--all)
#   7  permissions stage refused (writable path not in permissions.list)
#   8  agent count = 0 after onboarding seed (--all)
#   9  partial completion: one or more companions that were previously installed
#      on the node were skipped on this run (no credentials given); those
#      companions did not receive updates from this release. The node is
#      functional but not fully updated.

set -euo pipefail

SCRIPT_VERSION="0.8.2"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
# Commit hash of the installer tree being deployed. Passed to every companion
# as SN_BUILD_COMMIT so each can write a .sn-commit marker into its install
# directory. sn-version reads that marker to detect vendored-snapshot drift
# (the directory has no .git, so git-describe cannot tell which commit is
# running; without this, reinstalling from a newer commit reports UNCHANGED).
SN_BUILD_COMMIT=$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo unknown)

PROVIDER=""
NAME=""
IP=""
SSH_KEY=""
SSH_USER=""
SSH_PORT=22
DISK=""
# Empty = derived after Layer P from the controller route to the node.
ALLOW_FROM=""
NODE_FQDN=""
NO_VPN=0
SUBNET=""
NETWORK=""
STORAGE_POOL=""
VPN_ENDPOINT=""
TIMEZONE=""
NODE_DOMAIN=""
# The outside world's name for this node, when it differs from NODE_DOMAIN
# Empty means "one name", which is the common case; B03 carries a
# previously configured value forward, so a re-run need not restate it.
PUBLIC_DOMAIN=""
ACME_EMAIL=""
JSON_OUTPUT=0
MODE="default"   # default | bootstrap-only | all | companions-only
HITL_ORIGIN=""
HITL_ADOPT_LEGACY=0
ANTHROPIC_KEY=""
OPENROUTER_KEY=""
GEMINI_KEY=""
TG_TOKEN=""
TG_CHANNEL_ID=""
# External mailserver. The topology both real nodes run: the node does
# not host mail, it reads mailboxes off the host holding the valid PTR/cert/DKIM.
MAIL_HOST=""
MAIL_IMAP_HOST=""
MAIL_SMTP_HOST=""
MAIL_ACCOUNT=""
MAIL_PASSWORD=""
MAIL_ACCOUNTS_FILE=""
NOTIFY_EMAIL_SENDER=""
SECRETS_OPERATOR_USERS=""
OWNER_PASSWORD=""
SETUP_VIA_TOKEN=0
FORCE=0
# skip install (venv/pip/apt) for companions whose STAGE_VERSION
# matches the live node, writing only contracts.  Override with --force-companions.
FORCE_COMPANIONS=0
# Deliberately separate from generic --force: this one can narrow grants.
FORCE_ONBOARDING_RESEED=0
# On an update, do not change identity by default.
# Pass --set-identity to explicitly allow hostname rename or domain changes.
SET_IDENTITY=0
SEED_MANIFEST=""
SEED_MANIFEST_VOICE_SKIP=0
COMPONENTS=""
AI_WAY=""              # --ai: a subscription runtime or "key"; see tools/sn-components ways
LINK_FILE=""           # --first-run-link-file: where the first-run link goes (tools/web-install)
WANTED_COMPONENTS=""   # " a b c ": what this run installs; empty means everything
MANIFEST_BUDGETS_JSON=""

die() { echo "[ERROR] $*" >&2; exit "${2:-4}"; }
log() { echo "[$(date '+%H:%M:%S')] [install-on] $*"; }
usage() { awk '/^#!/ {next} /^[^#]/ {exit} {sub(/^# ?/, ""); print}' "$0"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --provider)   PROVIDER="$2"; shift 2 ;;
        --name)       NAME="$2"; shift 2 ;;
        --ip)         IP="$2"; shift 2 ;;
        --ssh-key)    SSH_KEY="$2"; shift 2 ;;
        --ssh-user)   SSH_USER="$2"; shift 2 ;;
        --ssh-port)   SSH_PORT="$2"; shift 2 ;;
        --disk)       DISK="$2"; shift 2 ;;
        --allow-from)  ALLOW_FROM="$2"; shift 2 ;;
        --node-fqdn)   NODE_FQDN="$2"; shift 2 ;;
        --no-vpn)      NO_VPN=1; shift ;;
        --subnet)      SUBNET="$2"; shift 2 ;;
        --network)     NETWORK="$2"; shift 2 ;;
        --storage-pool) STORAGE_POOL="$2"; shift 2 ;;
        --vpn-endpoint) VPN_ENDPOINT="$2"; shift 2 ;;
        --timezone)    TIMEZONE="$2"; shift 2 ;;
        --node-domain) NODE_DOMAIN="$2"; shift 2 ;;
        --public-domain) PUBLIC_DOMAIN="$2"; shift 2 ;;
        --acme-email)  ACME_EMAIL="$2"; shift 2 ;;
        --bootstrap-only)
            [[ "$MODE" == "all" || "$MODE" == "companions-only" ]] && die "--bootstrap-only is mutually exclusive with --all and --companions-only"
            MODE="bootstrap-only"; shift ;;
        --all)
            [[ "$MODE" == "bootstrap-only" || "$MODE" == "companions-only" ]] && die "--all is mutually exclusive with --bootstrap-only and --companions-only"
            MODE="all"; shift ;;
        --companions-only)
            [[ "$MODE" == "all" || "$MODE" == "bootstrap-only" ]] && die "--companions-only is mutually exclusive with --all and --bootstrap-only"
            MODE="companions-only"; shift ;;
        --hitl-origin) HITL_ORIGIN="$2"; shift 2 ;;
        --hitl-adopt-legacy) HITL_ADOPT_LEGACY=1; shift ;;
        --anthropic-key)  ANTHROPIC_KEY="$2"; shift 2 ;;
        --openrouter-key) OPENROUTER_KEY="$2"; shift 2 ;;
        --gemini-key)     GEMINI_KEY="$2"; shift 2 ;;
        --tg-token)       TG_TOKEN="$2"; shift 2 ;;
        --mail-host)          MAIL_HOST="$2"; shift 2 ;;
        --mail-imap-host)     MAIL_IMAP_HOST="$2"; shift 2 ;;
        --mail-smtp-host)     MAIL_SMTP_HOST="$2"; shift 2 ;;
        --mail-account)       MAIL_ACCOUNT="$2"; shift 2 ;;
        --mail-password)      MAIL_PASSWORD="$2"; shift 2 ;;
        --mail-accounts-file) MAIL_ACCOUNTS_FILE="$2"; shift 2 ;;
        --notify-email-sender) NOTIFY_EMAIL_SENDER="$2"; shift 2 ;;
        --secrets-operator-users) SECRETS_OPERATOR_USERS="$2"; shift 2 ;;
        --tg-channel-id)  TG_CHANNEL_ID="$2"; shift 2 ;;
        --owner-password) OWNER_PASSWORD="$2"; shift 2 ;;
        --setup-via-token) SETUP_VIA_TOKEN=1; shift ;;
        --force)      FORCE=1; shift ;;
        --force-companions) FORCE_COMPANIONS=1; shift ;;
        --force-onboarding-reseed) FORCE_ONBOARDING_RESEED=1; shift ;;
        --set-identity) SET_IDENTITY=1; shift ;;
        --seed-manifest) SEED_MANIFEST="$2"; shift 2 ;;
        --components) COMPONENTS="$2"; shift 2 ;;
        --ai)         AI_WAY="$2"; shift 2 ;;
        --first-run-link-file) LINK_FILE="$2"; shift 2 ;;
        --json)       JSON_OUTPUT=1; shift ;;
        --help|-h)    usage; exit 0 ;;
        *)            die "Unknown arg: $1 (use --help)" ;;
    esac
done

[[ -n "$PROVIDER" ]] || die "--provider is required"
[[ -n "$NAME" ]]     || die "--name is required"
[[ -n "$IP" ]]       || die "--ip is required"
[[ -n "$SSH_KEY" ]]  || die "--ssh-key is required (public key path)"
[[ -r "$SSH_KEY" ]]  || die "SSH key not readable: $SSH_KEY"

# This value is threaded through `sudo env` below, so its validation is both a
# policy check and a command-boundary check. Only comma-separated Unix account
# names are accepted; no whitespace or shell syntax can enter runcmd.
if [[ -n "$SECRETS_OPERATOR_USERS" \
      && ! "$SECRETS_OPERATOR_USERS" =~ ^[a-z_][a-z0-9_-]*(,[a-z_][a-z0-9_-]*)*$ ]]; then
    die "--secrets-operator-users invalid (expected comma-separated Unix usernames)"
fi

# Effective node FQDN for reporting. Set to the default now; updated after the
# early B01.state probe (before Layer A) once we know if this is an update.
NODE_FQDN_EFFECTIVE="${NODE_FQDN:-$NAME.sn.vpn}"
# --vpn-endpoint is meaningless when A6 is skipped.
[[ "$NO_VPN" -eq 1 && -n "$VPN_ENDPOINT" ]] \
    && die "--vpn-endpoint conflicts with --no-vpn (no WireGuard is installed)"

# Derive private key path: strip .pub if present.
SSH_KEY_PUB="$SSH_KEY"
if [[ "$SSH_KEY" == *.pub ]]; then
    SSH_KEY_PRIV="${SSH_KEY%.pub}"
else
    SSH_KEY_PRIV="$SSH_KEY"   # caller passed private; we'll use it as-is
    SSH_KEY_PUB="${SSH_KEY}.pub"
fi
[[ -r "$SSH_KEY_PRIV" ]] || die "Private key not readable at $SSH_KEY_PRIV (derived from --ssh-key)"
[[ -r "$SSH_KEY_PUB"  ]] || die "Public key not readable at $SSH_KEY_PUB"

# An install that runs the bootstrap stands up the operator panel. Refuse to
# create a node with no way to log in: require either --owner-password (seed the
# super-admin now, recommended) or an explicit --setup-via-token opt-in. Without
# one of these the node comes up with an operator account but no credential, and
# the browser /setup flow cannot yet mint one -- a dead end.
# --force is how an installed node is UPDATED: re-running the installer over it
# is the update mechanism, and the 'existing' provisioner otherwise refuses a
# machine that already carries Smart Node stages. It skips that one guard and
# destroys nothing. Deliberately not accepted for lxd-local: that provisioner
# has no --force, and its nearest flag (--recreate) DELETES the container, which
# is the opposite of an update.
if [[ "$FORCE" -eq 1 && "$PROVIDER" != "existing" ]]; then
    die "--force applies to --provider existing (proceed on an already-installed machine). For lxd-local, --recreate DESTROYS the container and is not an update."
fi

if [[ "$MODE" == "all" || "$MODE" == "bootstrap-only" ]]; then
    if [[ -z "$OWNER_PASSWORD" && "$SETUP_VIA_TOKEN" -ne 1 ]]; then
        die "no first login chosen. Pass --owner-password <PW> to seed the super-admin (recommended), or --setup-via-token to finish onboarding manually via the one-time token in /etc/smart-node/install-report.md. See docs/install-from-source.md Step 3. Updating a node that already has an owner? Pass --setup-via-token: it satisfies this check without touching the existing operator store." 3
    fi
fi

command -v jq >/dev/null 2>&1 || die "jq not installed on controller (apt-get install jq)"

# ========================================================================
# Seed manifest -- load before companion-credential validation so the
# manifest values are visible to the checks below.
# ========================================================================
if [[ -n "$SEED_MANIFEST" ]]; then
    [[ -r "$SEED_MANIFEST" ]] \
        || die "--seed-manifest not readable: $SEED_MANIFEST"
    jq -e . "$SEED_MANIFEST" >/dev/null 2>&1 \
        || die "--seed-manifest is not valid JSON: $SEED_MANIFEST"

    # Return the raw jq -r value for key $1, or empty string if absent/null.
    _mval() {
        jq -r --arg k "$1" \
            'if (has($k) and .[$k] != null) then .[$k] else "" end' \
            "$SEED_MANIFEST"
    }

    # Resolve a secret reference through this node's core-api secret store.
    # The key never touches the network or a file; it is passed as a shell
    # variable that lives only in memory for the duration of this script.
    # Dies if core-api is unreachable or the ref is absent.
    _resolve_secret_ref() {
        local _ref="$1"
        local _sock="/run/smart-node/api/core-api.sock"
        local _out _val
        _out=$(curl -sf --unix-socket "$_sock" \
            "http://localhost/api/secrets/resolve?ref=${_ref}" 2>&1) \
            || die "seed-manifest: secret_ref resolve failed for '${_ref}' (is core-api running on this provisioning node?): ${_out}"
        _val=$(printf '%s' "$_out" | jq -r '.value // empty')
        [[ -n "$_val" ]] \
            || die "seed-manifest: secret_ref '${_ref}' resolved to empty -- add it to the node secret store first"
        printf '%s' "$_val"
    }

    # Telegram
    _m_tg=$(_mval telegram)
    if   [[ "$_m_tg" == "disconnected" ]]; then
        TG_TOKEN=""; TG_CHANNEL_ID=""
        log "seed-manifest: telegram=disconnected"
    elif [[ -n "$_m_tg" ]]; then
        TG_TOKEN=$(jq -r '.telegram.token // ""' "$SEED_MANIFEST")
        TG_CHANNEL_ID=$(jq -r '.telegram.channel_id // ""' "$SEED_MANIFEST")
        log "seed-manifest: telegram credentials loaded"
    fi

    # Email
    _m_em=$(_mval email)
    if   [[ "$_m_em" == "disconnected" ]]; then
        MAIL_HOST=""; MAIL_ACCOUNT=""; MAIL_PASSWORD=""
        MAIL_IMAP_HOST=""; MAIL_SMTP_HOST=""; MAIL_ACCOUNTS_FILE=""
        log "seed-manifest: email=disconnected"
    elif [[ -n "$_m_em" ]]; then
        MAIL_HOST=$(jq -r '.email.host // ""' "$SEED_MANIFEST")
        MAIL_ACCOUNT=$(jq -r '.email.account // ""' "$SEED_MANIFEST")
        MAIL_PASSWORD=$(jq -r '.email.password // ""' "$SEED_MANIFEST")
        MAIL_IMAP_HOST=$(jq -r '.email.imap_host // ""' "$SEED_MANIFEST")
        MAIL_SMTP_HOST=$(jq -r '.email.smtp_host // ""' "$SEED_MANIFEST")
        log "seed-manifest: email credentials loaded"
    fi

    # Voice (Gemini key). When disconnected, also skip the companion in Phase 2.
    _m_vo=$(_mval voice)
    if   [[ "$_m_vo" == "disconnected" ]]; then
        GEMINI_KEY=""
        SEED_MANIFEST_VOICE_SKIP=1
        log "seed-manifest: voice=disconnected (companion will be skipped)"
    elif [[ -n "$_m_vo" ]]; then
        GEMINI_KEY=$(jq -r '.voice.gemini_key // ""' "$SEED_MANIFEST")
        log "seed-manifest: voice credentials loaded"
    fi

    # Anthropic
    _m_an=$(_mval anthropic)
    if   [[ "$_m_an" == "disconnected" ]]; then
        ANTHROPIC_KEY=""
        log "seed-manifest: anthropic=disconnected"
    elif [[ -n "$_m_an" ]]; then
        _m_an_ref=$(jq -r '.anthropic.secret_ref // ""' "$SEED_MANIFEST")
        if [[ -n "$_m_an_ref" ]]; then
            ANTHROPIC_KEY=$(_resolve_secret_ref "$_m_an_ref")
            log "seed-manifest: anthropic key resolved from secret_ref=${_m_an_ref}"
        else
            ANTHROPIC_KEY=$(jq -r '.anthropic.key // ""' "$SEED_MANIFEST")
            log "seed-manifest: anthropic credentials loaded"
        fi
    fi

    # OpenRouter
    _m_or=$(_mval openrouter)
    if   [[ "$_m_or" == "disconnected" ]]; then
        OPENROUTER_KEY=""
        log "seed-manifest: openrouter=disconnected"
    elif [[ -n "$_m_or" ]]; then
        _m_or_ref=$(jq -r '.openrouter.secret_ref // ""' "$SEED_MANIFEST")
        if [[ -n "$_m_or_ref" ]]; then
            OPENROUTER_KEY=$(_resolve_secret_ref "$_m_or_ref")
            log "seed-manifest: openrouter key resolved from secret_ref=${_m_or_ref}"
        else
            OPENROUTER_KEY=$(jq -r '.openrouter.key // ""' "$SEED_MANIFEST")
            log "seed-manifest: openrouter credentials loaded"
        fi
    fi

    # node_fqdn: only applied when not already set on the CLI, so an explicit
    # --node-fqdn argument always wins over the manifest.
    _m_fqdn=$(jq -r '.node_fqdn // ""' "$SEED_MANIFEST")
    if [[ -n "$_m_fqdn" && -z "$NODE_FQDN" ]]; then
        NODE_FQDN="$_m_fqdn"
        log "seed-manifest: node_fqdn=$NODE_FQDN"
    fi

    # no_vpn: manifest can set it; CLI --no-vpn already sets NO_VPN=1.
    _m_nvpn=$(jq -r '.no_vpn // false' "$SEED_MANIFEST")
    [[ "$_m_nvpn" == "true" ]] && NO_VPN=1

    # Budgets: optional array applied to the target node after Phase 3.5 (DB up).
    # Capture here; the actual INSERT runs inside the --all block below.
    MANIFEST_BUDGETS_JSON=$(jq -c '.budgets // null' "$SEED_MANIFEST")

    log "Seed manifest applied: $SEED_MANIFEST"
fi

# Resolve provision script.
case "$PROVIDER" in
    lxd-local) PROVISION_SCRIPT="$ROOT_DIR/provision/provision-lxd-local.sh" ;;
    existing)  PROVISION_SCRIPT="$ROOT_DIR/provision/provision-existing.sh" ;;
    *)         die "Unknown --provider '$PROVIDER' (supported: lxd-local, existing)" ;;
esac
[[ -x "$PROVISION_SCRIPT" ]] || die "Provision script missing or not executable: $PROVISION_SCRIPT"

CONFIGURE_SCRIPT="$ROOT_DIR/configure/configure-host.sh"
[[ -x "$CONFIGURE_SCRIPT" ]] || die "Configure script missing or not executable: $CONFIGURE_SCRIPT"

BOOTSTRAP_DIR="$ROOT_DIR/bootstrap"
[[ -r "$BOOTSTRAP_DIR/lib.sh"             ]] || die "bootstrap/lib.sh not found"
[[ -r "$BOOTSTRAP_DIR/B01-core.sh"        ]] || die "bootstrap/B01-core.sh not found"
[[ -r "$BOOTSTRAP_DIR/cron-runner.sh"     ]] || die "bootstrap/cron-runner.sh not found"
[[ -r "$BOOTSTRAP_DIR/B02-persistence.sh" ]] || die "bootstrap/B02-persistence.sh not found"
[[ -r "$BOOTSTRAP_DIR/B02-schema.sql"     ]] || die "bootstrap/B02-schema.sql not found"
[[ -r "$BOOTSTRAP_DIR/B02-sn-core-schema.sql" ]] || die "bootstrap/B02-sn-core-schema.sql not found"
[[ -r "$BOOTSTRAP_DIR/B02-sn-core-seed.sql"   ]] || die "bootstrap/B02-sn-core-seed.sql not found"
[[ -r "$BOOTSTRAP_DIR/B03-routing.sh"     ]] || die "bootstrap/B03-routing.sh not found"
[[ -r "$BOOTSTRAP_DIR/B04-secrets-slice.sh" ]] || die "bootstrap/B04-secrets-slice.sh not found"
[[ -r "$BOOTSTRAP_DIR/B05-identity.sh"    ]] || die "bootstrap/B05-identity.sh not found"
[[ -r "$BOOTSTRAP_DIR/B05-schema.sql"     ]] || die "bootstrap/B05-schema.sql not found"
[[ -d "$BOOTSTRAP_DIR/authum"             ]] || die "bootstrap/authum/ (vendored Authum snapshot) not found"
[[ -r "$BOOTSTRAP_DIR/B06-web-interface.sh" ]] || die "bootstrap/B06-web-interface.sh not found"
[[ -d "$BOOTSTRAP_DIR/smart-node-ui"      ]] || die "bootstrap/smart-node-ui/ (vendored panel snapshot) not found"

# Phase 2 companions + Phase 3 validate live alongside the bootstrap dir.
# They are only needed for --all; pre-flight them up front so an --all run
# fails fast (before Layer P provisions anything) if the tree is incomplete.
COMPANIONS_DIR="$ROOT_DIR/companions"
VALIDATE_SCRIPT="$ROOT_DIR/validate.sh"
COMPANION_ORDER=(admin-runner memory core-api gateway documents agent-runtimes tracking
                 dispatcher workflows communication voice studio hitl)
if [[ "$MODE" == "all" || "$MODE" == "companions-only" ]]; then
    [[ -d "$COMPANIONS_DIR" ]] || die "companions/ not found ($COMPANIONS_DIR) -- required for --all / --companions-only"
    [[ -r "$VALIDATE_SCRIPT" ]] || die "validate.sh not found ($VALIDATE_SCRIPT) -- required for --all / --companions-only (Phase 3)"
    if [[ "$MODE" == "all" ]]; then
        # --companions-only skips Phase 1 so these bootstrap files are not needed.
        [[ -r "$BOOTSTRAP_DIR/claude-auth-capture.sh" ]] \
            || die "bootstrap/claude-auth-capture.sh not found -- required for --all (W1 install-time auth capture)"
        [[ -r "$BOOTSTRAP_DIR/onboarding-seed.sh" ]] \
            || die "bootstrap/onboarding-seed.sh not found -- required for --all (W3 onboarding seed)"
        # The configurator specialist is the agent definition the onboarding seed
        # uses when creating the node's first agent. If it is absent from the
        # release package, the POST /api/agents call will fail (bad specialist slug)
        # and the node will come up with no agents -- which means the acceptance
        # suite cannot run. Refuse here, before any provisioning starts, so the
        # operator knows exactly what is missing and why.
        [[ -r "$COMPANIONS_DIR/srv-skeleton/agents/_specialists/configurator.md" ]] \
            || die "companions/srv-skeleton/agents/_specialists/configurator.md not found -- the configurator specialist is required to provision the node's first agent"
    fi
    for _c in "${COMPANION_ORDER[@]}"; do
        [[ -r "$COMPANIONS_DIR/$_c.sh" ]] \
            || die "companion script missing: companions/$_c.sh -- required for --all / --companions-only"
    done
    # Exactly-one-of the Telegram pair is almost certainly an operator slip.
    if [[ -n "$TG_TOKEN" && -z "$TG_CHANNEL_ID" ]] \
       || [[ -z "$TG_TOKEN" && -n "$TG_CHANNEL_ID" ]]; then
        die "--tg-token and --tg-channel-id must be supplied together"
    fi
    # Mail credentials without a host, or a host with no credentials, cannot
    # open a mailbox -- catch it here rather than three layers down.
    if [[ -z "$MAIL_HOST" ]] \
       && [[ -n "$MAIL_ACCOUNT$MAIL_PASSWORD$MAIL_ACCOUNTS_FILE" ]]; then
        die "--mail-account / --mail-password / --mail-accounts-file need --mail-host"
    fi
    if [[ -n "$MAIL_HOST" && -z "$MAIL_ACCOUNTS_FILE" ]] \
       && { [[ -z "$MAIL_ACCOUNT" ]] || [[ -z "$MAIL_PASSWORD" ]]; }; then
        die "--mail-host needs credentials: --mail-accounts-file, or --mail-account together with --mail-password"
    fi
else
    # The companion-tuning flags only mean something for --all / --companions-only.
    [[ -n "$ANTHROPIC_KEY" ]] && die "--anthropic-key is only valid with --all or --companions-only"
    [[ -n "$OPENROUTER_KEY" ]] && die "--openrouter-key is only valid with --all or --companions-only"
    [[ -n "$GEMINI_KEY" ]] && die "--gemini-key is only valid with --all or --companions-only"
    [[ -n "$TG_TOKEN" || -n "$TG_CHANNEL_ID" ]] \
        && die "--tg-token / --tg-channel-id are only valid with --all or --companions-only"
    [[ -n "$MAIL_HOST$MAIL_ACCOUNT$MAIL_PASSWORD$MAIL_ACCOUNTS_FILE" ]] \
        && die "--mail-* options are only valid with --all or --companions-only"
    [[ -z "$HITL_ORIGIN" ]] \
        || die "--hitl-origin is only valid with --all or --companions-only"
    [[ "$HITL_ADOPT_LEGACY" -eq 0 ]] \
        || die "--hitl-adopt-legacy is only valid with --all or --companions-only"
fi

if [[ -n "$HITL_ORIGIN" && ! "$HITL_ORIGIN" =~ ^https://[a-zA-Z0-9][a-zA-Z0-9.-]*(:[0-9]+)?$ ]]; then
    die "--hitl-origin must be an HTTPS origin without path"
fi
if [[ "$HITL_ADOPT_LEGACY" -eq 1 && -z "$HITL_ORIGIN" ]]; then
    die "--hitl-adopt-legacy requires --hitl-origin (the existing passkey origin)"
fi

# Working dir for per-layer logs. Preserved on failure so the operator
# can read what went wrong; cleaned only after a fully successful run.
WORK_DIR="/tmp/smart-node-install-$NAME.$$"
mkdir -p "$WORK_DIR"
cleanup() {
    local rc=$?
    # The node-side staging directory goes however this run ends. It is set
    # later (it needs SSH), so this is a no-op until then. Anything left there
    # is the installer's, not the node's, and leaving it behind is what makes
    # the NEXT run fail on a path it cannot overwrite.
    if [[ -n "${RSTAGE:-}" && -n "${P_USER:-}" && -n "${P_IP:-}" ]]; then
        ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n rm -rf -- '$RSTAGE'" >/dev/null 2>&1 || true
    fi
    if (( rc == 0 )); then
        rm -rf "$WORK_DIR"
    else
        echo "[install-on] logs preserved at $WORK_DIR (exit $rc)" >&2
        [[ -n "${RSTAGE:-}" ]] \
            && echo "[install-on] node staging dir $RSTAGE removed" >&2
    fi
}
trap cleanup EXIT

# ========================================================================
# Shipping hygiene -- before a single byte leaves this machine.
# ========================================================================
# The companion trees go to the node a directory at a time, so whatever sits in
# them travels. .gitignore does not protect a node; it only hides the file from
# review. That is how a Telegram token, a brin API key and two mailbox passwords
# rode to every node inside companions/communication/.env.bak.*.
HYGIENE="$(dirname "${BASH_SOURCE[0]}")/../tools/shipping-hygiene.sh"
if [[ -x "$HYGIENE" ]]; then
    if ! "$HYGIENE"; then
        die "refusing to install: the shipped trees contain files that must not reach a node (see above)" 4
    fi
else
    log "WARN: tools/shipping-hygiene.sh missing -- shipping unchecked"
fi

# ========================================================================
# Layer P
# ========================================================================
# companions-only: skip Layer P entirely. The target is an existing container
# that already has bootstrap complete. SSH coordinates come from --ip / --ssh-user.
if [[ "$MODE" == "companions-only" ]]; then
    [[ -n "$SSH_USER" ]] || die "--companions-only requires --ssh-user (the admin user on the pre-bootstrapped container, e.g. admin)"
    P_IP="$IP"
    P_USER="$SSH_USER"
    P_PROVIDER_ID="(companions-only -- no Layer P)"
    log "companions-only: using existing container at $P_USER@$P_IP (Layer P skipped)"
else
    log "=== Layer P ($PROVIDER) ==="

    P_LOG="$WORK_DIR/provision.log"
    P_JSON="$WORK_DIR/provision.json"

    # Provision script writes its JSON contract to stdout when --json; mixed
    # apt output goes to stderr. We capture both, then extract the JSON object
    # from stdout (last contiguous { ... } block). --disk, when given, is
    # passed through to size the node's root disk.
    PROVISION_ARGS=(--name "$NAME" --ip "$IP" --ssh-key "$SSH_KEY_PUB" --json)
    [[ -n "$DISK" ]] && PROVISION_ARGS+=(--disk "$DISK")
    [[ -n "$SSH_USER" ]] && PROVISION_ARGS+=(--ssh-user "$SSH_USER")
    # Provider-shaping flags. The lxd-local provisioner consumes them; the
    # 'existing' provisioner accepts-and-ignores them (it creates nothing).
    [[ -n "$SUBNET" ]] && PROVISION_ARGS+=(--subnet "$SUBNET")
    [[ -n "$NETWORK" ]] && PROVISION_ARGS+=(--network "$NETWORK")
    [[ -n "$STORAGE_POOL" ]] && PROVISION_ARGS+=(--storage-pool "$STORAGE_POOL")
    [[ "$FORCE" -eq 1 ]] && PROVISION_ARGS+=(--force)
    if ! "$PROVISION_SCRIPT" \
            "${PROVISION_ARGS[@]}" \
            2> "$P_LOG.err" \
            | tee "$P_LOG" \
            | awk '/^{/,/^}/' > "$P_JSON"; then
        cat "$P_LOG.err" >&2
        die "Layer P failed (see $P_LOG.err)" 1
    fi

    if ! jq -e . "$P_JSON" >/dev/null 2>&1; then
        cat "$P_LOG.err" >&2
        die "Layer P produced unparseable JSON (see $P_JSON)" 1
    fi

    P_IP=$(jq -r '.ip'           "$P_JSON")
    P_USER=$(jq -r '.ssh_user'    "$P_JSON")
    P_PROVIDER_ID=$(jq -r '.provider_id' "$P_JSON")
    log "Layer P OK -- provider_id=$P_PROVIDER_ID ip=$P_IP user=$P_USER"
fi

# ========================================================================
# SSH options -- defined here (before Layer A) so the early B01.state probe
# can run before the hostname rename decision in Layer A. (SSH options must exist before the early probe so identity preservation
# can be automatic rather than dependent on --force.)
# ========================================================================
SSH_OPTS=(
    -i "$SSH_KEY_PRIV"
    -p "$SSH_PORT"
    -o "BatchMode=yes"
    -o "StrictHostKeyChecking=accept-new"
    -o "ConnectTimeout=10"
    -o "UserKnownHostsFile=$WORK_DIR/known_hosts"
)
# scp uses -P (uppercase) for port; -p means "preserve timestamps".
SCP_OPTS=(
    -i "$SSH_KEY_PRIV"
    -P "$SSH_PORT"
    -o "BatchMode=yes"
    -o "StrictHostKeyChecking=accept-new"
    -o "ConnectTimeout=10"
    -o "UserKnownHostsFile=$WORK_DIR/known_hosts"
)

# ========================================================================
# Early update-vs-install detection
#
# Was this machine EMPTY before Layer C touched it? This must be asked BEFORE
# Layer A runs, because Layer A may rename the node and we need to know whether
# to skip that rename BEFORE it happens.  The --force flag was the previous
# indicator; _WAS_FRESH is the automatic one. Both are correct: --force lets
# the provisioner proceed; _WAS_FRESH detects what the node actually carries.
#
# THIS MUST COME AFTER SSH_OPTS EXISTS. SSH_OPTS is now defined
# earlier precisely so this probe can precede Layer A.
#
# AND AN UNREACHABLE NODE IS AN ERROR, NOT AN EMPTY ONE.
# ========================================================================
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "true" >/dev/null 2>&1; then
    die "cannot reach $P_USER@$P_IP before Layer A -- refusing to guess
           whether the target is empty (the answer decides identity preservation
           and release-evidence eligibility; guessing wrong is worse than stopping,
           as shown by the early identity probe)." 2
fi
if [[ "$MODE" == all || "$MODE" == companions-only ]]; then
    _legacy_hitl=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
        "sudo -n test -d /opt/smart-node/hitl/app && ! sudo -n test -f /etc/smart-node/hitl-adoption.json && echo yes" \
        2>/dev/null || true)
    if [[ "$_legacy_hitl" == yes && "$HITL_ADOPT_LEGACY" -eq 0 ]]; then
        die "legacy Docker HITL detected; this release requires explicit --hitl-adopt-legacy and --hitl-origin before any node update"
    fi
    if [[ "$_legacy_hitl" != yes && "$HITL_ADOPT_LEGACY" -eq 1 ]]; then
        die "--hitl-adopt-legacy requested but no legacy Docker HITL is present"
    fi
fi
if [[ "$MODE" == "companions-only" ]]; then
    # companions-only requires bootstrap to be complete. If B01.state is absent
    # the container was not bootstrapped and Phase 2 cannot run.
    if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "test -e /var/lib/smart-node/stages/B01.state" >/dev/null 2>&1; then
        die "--companions-only requires bootstrap complete (B01.state absent on target).
           Run install-on.sh --bootstrap-only first, or provision from a golden image with
           provision/build-stand-golden.sh." 3
    fi
    _WAS_FRESH="false"
    log "companions-only: bootstrap confirmed (B01.state present) at $P_USER@$P_IP"
elif ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "test -e /var/lib/smart-node/stages/B01.state" >/dev/null 2>&1; then
    _WAS_FRESH="false"
    log "target already carries B01.state -- this run is an UPDATE"
else
    _WAS_FRESH="true"
    log "target has no Layer C state (no B01.state) -- this run is a CLEAN INSTALL"
fi

# On an update without --set-identity, preserve the existing hostname (no
# rename). On a first install, behave as before. If the operator passed an
# explicit --node-fqdn the rename is honored regardless of --set-identity.
if [[ "${_WAS_FRESH}" == "false" && -z "$NODE_FQDN" && "$SET_IDENTITY" -eq 0 ]]; then
    NODE_FQDN_EFFECTIVE="(existing hostname preserved -- pass --set-identity with --node-fqdn to rename)"
fi

# ========================================================================
# Phase 1: Layer A through B06
# companions-only skips this entire block -- bootstrap is already done.
# ========================================================================
if [[ "$MODE" != "companions-only" ]]; then

# ========================================================================
# Layer A
# ========================================================================
log "=== Layer A (configure-host) ==="

A_LOG="$WORK_DIR/configure.log"

# --name is threaded into Layer A as --node-name: it enables A6
# (vpn-server) -- the node FQDN <name>.sn.vpn, WireGuard, wildcard DNS and
# phone QR. --vpn-endpoint is passed through only when the operator gave
# one (a NAT'd node); otherwise A6 defaults it to <node-ip>:51820.
# Layer A closes the target firewall to --allow-from only. If the operator did
# not pass one, derive the controller own source address toward the node: any
# other default risks locking the installer out of the machine it just built
# (a fixed VPN CIDR does exactly that on a local-LXD or same-LAN install).
if [[ -z "$ALLOW_FROM" ]]; then
    CTL_SRC=$(ip route get "$P_IP" 2>/dev/null |
        awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i+1); exit } }')
    if [[ -z "$CTL_SRC" ]]; then
        die "cannot derive --allow-from: no source address on the route to $P_IP -- pass --allow-from explicitly" 2
    fi
    ALLOW_FROM="$CTL_SRC/32"
    log "Layer A: --allow-from not given -- derived controller source $ALLOW_FROM"
    log "Layer A: pass --allow-from CIDR to widen it (e.g. your LAN or VPN subnet)"
fi

CONFIGURE_ARGS=(
    --ip "$P_IP" --ssh-user "$P_USER" --ssh-key "$SSH_KEY_PRIV"
    --allow-from "$ALLOW_FROM" --ssh-port "$SSH_PORT"
)
# On an update (B01.state present) without --set-identity and without an
# explicit --node-fqdn, skip the hostname rename entirely: silence means
# "leave the existing hostname". On a first install, behave as before
# (--node-name drives A6 and sets the node FQDN to NAME.sn.vpn by default).
# When --set-identity is given the caller is explicitly overriding identity,
# so the rename proceeds even without --node-fqdn.
if [[ "${_WAS_FRESH}" == "false" && -z "$NODE_FQDN" && "$SET_IDENTITY" -eq 0 ]]; then
    log "update run: skipping hostname rename (existing FQDN preserved; pass --set-identity with --node-fqdn to rename)"
else
    CONFIGURE_ARGS+=(--node-name "$NAME")
fi
[[ -n "$NODE_FQDN" ]] && CONFIGURE_ARGS+=(--node-fqdn "$NODE_FQDN")
[[ "$NO_VPN" -eq 1 ]] && CONFIGURE_ARGS+=(--no-vpn)
[[ -n "$VPN_ENDPOINT" ]] && CONFIGURE_ARGS+=(--vpn-endpoint "$VPN_ENDPOINT")
[[ -n "$TIMEZONE" ]] && CONFIGURE_ARGS+=(--timezone "$TIMEZONE")

if ! "$CONFIGURE_SCRIPT" "${CONFIGURE_ARGS[@]}" 2>&1 | tee "$A_LOG"; then
    die "Layer A failed (see $A_LOG)" 2
fi
log "Layer A OK"

# Brief settle for UFW (and any sshd reload) before we try a fresh SSH.
sleep 3

# ========================================================================
# Layer C bootstrap B01
# ========================================================================
log "=== Layer C bootstrap B01 (core) ==="

# SSH_OPTS and _WAS_FRESH are defined earlier (before Layer A) so the
# identity-preservation decision can be made before the hostname is set.
# See the "Early update-vs-install detection" block above.

# Probe a fresh post-UFW SSH (caller's source must be in --allow-from).
log "Probing post-A SSH"
for attempt in 1 2 3 4 5; do
    if ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "true" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    [[ $attempt -eq 5 ]] && die "post-A SSH unreachable (allow-from likely excludes controller IP)" 3
done

# ---------------------------------------------------------------------------
# One staging directory per run, made by the node, owned by the SSH user.
#
# Everything this installer copies to the node lands here and nowhere else.
# It used to write 22 FIXED paths straight into /tmp -- $RSTAGE/lib.sh,
# $RSTAGE/B01-core.sh, $RSTAGE/validate.sh and so on -- and /tmp is world-writable.
# Anything on the node that got there first, an agent with a shell, a stray
# script, an operator, or simply a previous run of this installer that left
# root-owned files behind, made the next `scp` fail with "Permission denied"
# and took the whole update down with it. That is exactly how the reference-node update
# died on 2026-08-15: /tmp/sn-companion-core-api was root:root from an earlier
# run, and the failure arrived at the last step, after everything real had
# already succeeded.
#
# `mktemp -d` on the NODE, not a name built from the controller's PID: the node
# is the only place that can guarantee the name is free, and two controllers
# updating the same machine must not collide.
#
# Note the sticky bit on /tmp means this was never a way to get code executed as
# root -- a non-owner cannot replace another user's file there. The exposure was
# denial of service, and this closes it.
# ---------------------------------------------------------------------------
RSTAGE=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
    'mktemp -d /tmp/sn-install.XXXXXXXX' 2>/dev/null) \
    || die "could not create a staging directory on the node" 3
[[ -n "$RSTAGE" ]] || die "the node returned an empty staging directory path" 3
log "staging on the node: $RSTAGE"

# Removing it is the existing `cleanup` EXIT trap's job (it now knows about
# RSTAGE). Do NOT add a second `trap ... EXIT` here: bash keeps only the last
# one, so a second trap would silently replace the first and the "logs
# preserved at ..." line would stop being printed on failure.

log "Pushing bootstrap/lib.sh + bootstrap/B01-core.sh + bootstrap/cron-runner.sh + the permission surface"
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/lib.sh"         "$P_USER@$P_IP:$RSTAGE/lib.sh" >/dev/null
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/B01-core.sh"    "$P_USER@$P_IP:$RSTAGE/B01-core.sh" >/dev/null
# cron-runner.sh is a B01-core sidecar: B01-core.sh's install_cron_infra
# reads it from its own directory (/tmp) and installs it to
# /usr/local/lib/smart-node/cron-runner.sh.
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/cron-runner.sh" "$P_USER@$P_IP:$RSTAGE/cron-runner.sh" >/dev/null
# permissions.sh + permissions.list are B01-core sidecars too: install_lib_sh
# copies them to /usr/local/lib/smart-node so every later install path and the
# scheduled check find them at a fixed location. Without them here, a fresh
# node installs with NO permission surface at all -- B01 skips its check cron
# and its own --verify fails.
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/permissions.sh"   "$P_USER@$P_IP:$RSTAGE/permissions.sh" >/dev/null
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/permissions.list" "$P_USER@$P_IP:$RSTAGE/permissions.list" >/dev/null
# local-layer.sh, the node-local settings layer, travels the same way.
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/local-layer.sh"   "$P_USER@$P_IP:$RSTAGE/local-layer.sh" >/dev/null
# agent-max.sudoers grants the sysadmin role only the sn-op wrapper. B01-core.sh
# reads it from the staged sidecar, as it does cron-runner.sh.
scp "${SCP_OPTS[@]}" "$ROOT_DIR/provision/sudoers/agent-max.sudoers" "$P_USER@$P_IP:$RSTAGE/agent-max.sudoers" >/dev/null
# The sudoers grant names this validated wrapper, so B01 installs it first.
scp "${SCP_OPTS[@]}" "$ROOT_DIR/provision/sbin/sn-op" "$P_USER@$P_IP:$RSTAGE/sn-op" >/dev/null

# Install lib.sh into its canonical path before running B01-core.
ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "
    sudo -n install -d -m 0755 /usr/local/lib/smart-node &&
    sudo -n install -m 0644 $RSTAGE/lib.sh /usr/local/lib/smart-node/lib.sh
"

log "Running B01-core"
C_LOG="$WORK_DIR/install-B01.log"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B01-core.sh" 2>&1 | tee "$C_LOG"; then
    die "Layer C bootstrap B01 failed (see $C_LOG)" 3
fi

log "Final --verify"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B01-core.sh --verify" 2>&1 | tee -a "$C_LOG"; then
    die "Layer C bootstrap B01 --verify failed (see $C_LOG)" 3
fi

# Cleanup pushed staging files (best-effort).
ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f $RSTAGE/lib.sh $RSTAGE/B01-core.sh $RSTAGE/cron-runner.sh $RSTAGE/permissions.sh $RSTAGE/permissions.list $RSTAGE/local-layer.sh $RSTAGE/agent-max.sudoers $RSTAGE/sn-op" >/dev/null 2>&1 || true

# ========================================================================
# Layer C bootstrap B02 (persistence)
# ========================================================================
# Same push -> run -> --verify as B01. B02 sources the lib.sh that B01
# already installed; it needs its B02-schema.sql sidecar alongside the
# stage script. B02 itself gates on B01.state (require_stage B01).
log "=== Layer C bootstrap B02 (persistence) ==="

log "Pushing bootstrap/B02-persistence.sh + bootstrap/B02-schema.sql"
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/B02-persistence.sh" "$P_USER@$P_IP:$RSTAGE/B02-persistence.sh" >/dev/null
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/B02-schema.sql"     "$P_USER@$P_IP:$RSTAGE/B02-schema.sql" >/dev/null
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/B02-sn-core-schema.sql" "$P_USER@$P_IP:$RSTAGE/B02-sn-core-schema.sql" >/dev/null
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/B02-sn-core-seed.sql"   "$P_USER@$P_IP:$RSTAGE/B02-sn-core-seed.sql" >/dev/null

log "Running B02-persistence"
C2_LOG="$WORK_DIR/install-B02.log"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B02-persistence.sh" 2>&1 | tee "$C2_LOG"; then
    die "Layer C bootstrap B02 failed (see $C2_LOG)" 3
fi

log "Final --verify"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B02-persistence.sh --verify" 2>&1 | tee -a "$C2_LOG"; then
    die "Layer C bootstrap B02 --verify failed (see $C2_LOG)" 3
fi

# Cleanup pushed staging files (best-effort).
ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f $RSTAGE/B02-persistence.sh $RSTAGE/B02-schema.sql $RSTAGE/B02-sn-core-schema.sql $RSTAGE/B02-sn-core-seed.sql" >/dev/null 2>&1 || true

# ========================================================================
# Layer C bootstrap B03 (routing)
# ========================================================================
# Same push -> run -> --verify as B01/B02. B03 installs Docker + a Traefik
# proxy; it gates on B01.state (require_stage B01). A node domain, if
# given, is threaded through as SMART_NODE_DOMAIN -- sudo resets the
# environment, so it is set via `env` (a plain command, no sudoers SETENV
# needed). Without a domain B03 falls back to a self-signed cert.
log "=== Layer C bootstrap B03 (routing) ==="

log "Pushing bootstrap/B03-routing.sh"
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/B03-routing.sh" "$P_USER@$P_IP:$RSTAGE/B03-routing.sh" >/dev/null

B03_ENV=""
if [[ -n "$NODE_DOMAIN" ]]; then
    log "B03: node domain '$NODE_DOMAIN' -- Traefik will request a Let's Encrypt cert"
    B03_ENV="SMART_NODE_DOMAIN=$NODE_DOMAIN SMART_NODE_ACME_EMAIL=$ACME_EMAIL"
else
    log "B03: no node domain -- Traefik uses a self-signed bootstrap cert"
fi
# The public name is a SEPARATE fact from the node domain: the node is
# administered by one and reached by the other. Passed here only when the
# operator restates it -- B03 carries a previously recorded value forward, so
# leaving it off an update no longer resets the node's public face.
if [[ -n "$PUBLIC_DOMAIN" ]]; then
    if [[ "$PUBLIC_DOMAIN" == "-" ]]; then
        log "B03: public domain being CLEARED -- sign-in identity returns to the node domain"
    else
        log "B03: public domain '$PUBLIC_DOMAIN' -- sign-in identity is built from it"
    fi
    B03_ENV="$B03_ENV SMART_NODE_PUBLIC_DOMAIN=$PUBLIC_DOMAIN"
fi
if [[ -n "$B03_ENV" ]]; then
    B03_RUN="sudo -n env $B03_ENV bash $RSTAGE/B03-routing.sh"
else
    B03_RUN="sudo -n bash $RSTAGE/B03-routing.sh"
fi

log "Running B03-routing"
C3_LOG="$WORK_DIR/install-B03.log"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "$B03_RUN" 2>&1 | tee "$C3_LOG"; then
    die "Layer C bootstrap B03 failed (see $C3_LOG)" 3
fi

log "Final --verify"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B03-routing.sh --verify" 2>&1 | tee -a "$C3_LOG"; then
    die "Layer C bootstrap B03 --verify failed (see $C3_LOG)" 3
fi

# Cleanup pushed staging files (best-effort).
ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f $RSTAGE/B03-routing.sh" >/dev/null 2>&1 || true

# ========================================================================
# Layer C bootstrap B04 (secrets)
# ========================================================================
# Installs sops + age binaries + per-node age key + empty
# sops-encrypted integrations.sops.yaml. No containers, no Redis, no
# Infisical -- the legacy Infisical-based B04 was orphaned dead weight
# (~5 min install + 700 MB + 2 idle containers, never read by any
# companion at runtime; the legacy Infisical stack is no longer installed).
log "=== Layer C bootstrap B04 (secrets) ==="

log "Pushing bootstrap/B04-secrets-slice.sh"
scp "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/B04-secrets-slice.sh" "$P_USER@$P_IP:$RSTAGE/B04-secrets.sh" >/dev/null

log "Running B04-secrets (slice)"
C4_LOG="$WORK_DIR/install-B04.log"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B04-secrets.sh" 2>&1 | tee "$C4_LOG"; then
    die "Layer C bootstrap B04 failed (see $C4_LOG)" 3
fi

log "Final --verify"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B04-secrets.sh --verify" 2>&1 | tee -a "$C4_LOG"; then
    die "Layer C bootstrap B04 --verify failed (see $C4_LOG)" 3
fi

# Cleanup pushed staging files (best-effort).
ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f $RSTAGE/B04-secrets.sh" >/dev/null 2>&1 || true

# ========================================================================
# Layer C bootstrap B05 (identity)
# ========================================================================
# Same push -> run -> --verify as B01..B04. B05 installs the Authum SSO
# service + the identity/roles schema; it gates on B02.state + B03.state +
# B04.state (require_stage) and reads the persistence + routing contracts
# from the target -- so no env threading is needed. Unlike the earlier
# stages B05 needs two sidecars: the B05-schema.sql file and the vendored
# bootstrap/authum/ snapshot it builds the Authum image from. Both must sit
# alongside B05-identity.sh, so all three go to /tmp.
log "=== Layer C bootstrap B05 (identity) ==="

log "Pushing bootstrap/B05-identity.sh + bootstrap/B05-schema.sql + bootstrap/authum/"
scp "${SCP_OPTS[@]}"    "$BOOTSTRAP_DIR/B05-identity.sh" "$P_USER@$P_IP:$RSTAGE/B05-identity.sh" >/dev/null
scp "${SCP_OPTS[@]}"    "$BOOTSTRAP_DIR/B05-schema.sql"  "$P_USER@$P_IP:$RSTAGE/B05-schema.sql" >/dev/null
scp -r "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/authum"          "$P_USER@$P_IP:$RSTAGE/authum" >/dev/null

log "Running B05-identity"
C5_LOG="$WORK_DIR/install-B05.log"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B05-identity.sh" 2>&1 | tee "$C5_LOG"; then
    die "Layer C bootstrap B05 failed (see $C5_LOG)" 3
fi

log "Final --verify"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B05-identity.sh --verify" 2>&1 | tee -a "$C5_LOG"; then
    die "Layer C bootstrap B05 --verify failed (see $C5_LOG)" 3
fi

# Cleanup pushed staging files (best-effort).
ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -rf $RSTAGE/B05-identity.sh $RSTAGE/B05-schema.sql $RSTAGE/authum" >/dev/null 2>&1 || true

# ========================================================================
# Layer C bootstrap B06 (web-interface)
# ========================================================================
# The final Phase-1 stage. Same push -> run -> --verify as B01..B05. B06
# installs the smart-node-ui control panel; it gates on B03.state +
# B05.state and reads the routing + persistence + identity contracts from
# the target. Like B05 it needs a vendored sidecar -- bootstrap/smart-node-ui/
# -- alongside the stage script, so both go to /tmp.
log "=== Layer C bootstrap B06 (web-interface) ==="

log "Pushing bootstrap/B06-web-interface.sh + bootstrap/smart-node-ui/"
scp "${SCP_OPTS[@]}"    "$BOOTSTRAP_DIR/B06-web-interface.sh" "$P_USER@$P_IP:$RSTAGE/B06-web-interface.sh" >/dev/null
scp -r "${SCP_OPTS[@]}" "$BOOTSTRAP_DIR/smart-node-ui"        "$P_USER@$P_IP:$RSTAGE/smart-node-ui" >/dev/null

log "Running B06-web-interface"
C6_LOG="$WORK_DIR/install-B06.log"
# --owner-password: thread it to B06 via `env` so B06 seeds the super-admin
# password into Authum (born logged-in). Escaped for the remote shell; passed
# as an env var (never written to a file, never in --verify). Absent it, B06
# emits the one-time /setup token instead.
B06_RUN="sudo -n bash $RSTAGE/B06-web-interface.sh"
if [[ -n "$OWNER_PASSWORD" ]]; then
    printf -v _opq '%q' "$OWNER_PASSWORD"
    B06_RUN="sudo -n env SMART_NODE_OWNER_PASSWORD=$_opq bash $RSTAGE/B06-web-interface.sh"
fi
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "$B06_RUN" 2>&1 | tee "$C6_LOG"; then
    die "Layer C bootstrap B06 failed (see $C6_LOG)" 3
fi

log "Final --verify"
if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/B06-web-interface.sh --verify" 2>&1 | tee -a "$C6_LOG"; then
    die "Layer C bootstrap B06 --verify failed (see $C6_LOG)" 3
fi

# Cleanup pushed staging files (best-effort).
ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -rf $RSTAGE/B06-web-interface.sh $RSTAGE/smart-node-ui" >/dev/null 2>&1 || true

fi  # end if [[ "$MODE" != "companions-only" ]] -- Phase 1

# ========================================================================
# Phase 2 companions + Phase 3 validate  (--all / --companions-only)
# ========================================================================
# Phase 1 bootstrap (B01..B06) is complete here -- the node has a working
# control panel (or was already bootstrapped for --companions-only).
# MODE decides how far the run goes:
#   default / bootstrap-only  -> stop (operator continues in web)
#   all / companions-only     -> Phase 2 companions, then validate

# COMPANIONS_DONE accumulates the companions actually installed.
# COMPANIONS_SKIPPED accumulates companions that were previously installed but
# could not be updated on this run (e.g. communication with no channel given).
# COMPANIONS_ABSENT is different: optional companions deliberately not installed
# on this node. They are valid omissions, but validate must name them because
# their stage and artifact checks are outside its denominator.
# A skipped-but-installed companion is NOT a full success: the output section
# and exit code distinguish it so the operator knows the update was partial
# .
COMPANIONS_DONE=()
COMPANIONS_SKIPPED=()
COMPANIONS_ABSENT=()

# companions-only: RSTAGE was not created by Phase 1 (which was skipped).
# Create it now so run_companion has a staging directory on the node.
if [[ "$MODE" == "companions-only" ]]; then
    RSTAGE=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
        'mktemp -d /tmp/sn-install.XXXXXXXX' 2>/dev/null) \
        || die "could not create a staging directory on the node" 3
    [[ -n "$RSTAGE" ]] || die "the node returned an empty staging directory path" 3
    log "companions-only: staging on the node: $RSTAGE"
fi

# companion_source_hash SCRIPT SIDECARS ENVSTR MODE -- one hash of everything a
# companion install is made from: its script, every sidecar tree, the environment
# string handed to it and the forced mode. Paths are hashed relative to their own
# root, so a different checkout of the same source gives the same answer. The
# environment string may carry keys; it is only ever fed to sha256sum.
companion_source_hash() {
    local script="$1" sidecars="$2" envstr="$3" forced="$4" s src
    {
        printf 'env:%s\nmode:%s\n' "$envstr" "$forced"
        sha256sum "$script" | cut -d' ' -f1
        for s in $sidecars; do
            if [[ "$s" == *:* ]]; then src="$ROOT_DIR/${s%%:*}"; else src="$COMPANIONS_DIR/$s"; fi
            [[ -e "$src" ]] || return 1
            printf 'sidecar:%s\n' "$s"
            if [[ -d "$src" ]]; then
                (cd "$src" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum)
            else
                sha256sum "$src" | cut -d' ' -f1
            fi
        done
    } | sha256sum | cut -d' ' -f1
}

# run_companion NAME "sidecar ..." "ENV ..." -- install one Phase 2
# companion. Pushes the companion script plus every sidecar (a vendored
# snapshot dir, an image tarball dir, or a sidecar file) into a private
# /tmp staging dir on the node -- they must land beside the script, which
# resolves its own dir to find them -- then runs it and --verify's it,
# mirroring the B01..B06 push -> run -> verify flow. ENV, when non-empty,
# is threaded through `sudo env` (the B03 pattern: no sudoers SETENV).
# Does the node's proxy have a route to the Studio? Any answer but "no such route".
studio_answers() {
    local code
    code=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" 'd=$(sudo -n sed -n "s/^SMART_NODE_DOMAIN=//p" /etc/smart-node/routing.env | tail -1)
        [ -n "$d" ] || { echo none; exit 0; }
        case "$d" in *.localhost) s=http p=80 ;; *) s=https p=443 ;; esac
        curl -sk -m 10 -o /dev/null -w "%{http_code}" --resolve "studio.$d:$p:127.0.0.1" "$s://studio.$d/"') || return 1
    [[ "$code" =~ ^[23] ]]
}

# Whether this run installs a companion. One place, read from the manifests
# (tools/sn-components): nothing here knows what any module needs.
component_wanted() {
    [[ -z "$WANTED_COMPONENTS" || "$WANTED_COMPONENTS" == *" $1 "* ]]
}

run_companion() {
    # $4, when given, is the companion mode to run instead of full or
    # contracts-only (e.g. agent-cli). Such a mode checks its own result, so
    # the companion's --verify, which describes a fully installed companion,
    # is not run after it.
    local name="$1" sidecars="${2:-}" envstr="${3:-}" forced_mode="${4:-}"
    local script="$COMPANIONS_DIR/$name.sh"
    if ! component_wanted "$name"; then
        log "$name not chosen -- skipped"
        COMPANIONS_ABSENT+=("$name")
        return 0
    fi

    # SN_CORE_API_URL for every companion. the installer removed the baked default and
    # nothing replaced it, so on a fresh install the companions had no core-api
    # address at all: "integration 'mesh' NOT registered -- core-api unreachable ()"
    # with empty parentheses, and the gdrive mcp_servers row was skipped silently.
    # Derive it with the SAME expression core-api uses to choose its own bind
    # (companions/core-api.sh CORE_API_HOST), so both sides agree by construction
    # rather than by two people guessing the same network name.
    if [[ -z "${SN_CORE_API_URL:-}" ]]; then
        # the routing contract, and the same answer companions/tracking.sh already gives in
        # resolve_core_api_url(): core-api binds 0.0.0.0 and every consumer of
        # this file is a NATIVE process on the node (core-api, dispatcher,
        # gateway, runner, retention, voice), so the address is loopback.
        #
        # It used to be the docker network GATEWAY, read off the host at install
        # time. That is the address of a bridge, and it made the node contract
        # disagree with the integrations registry, which says 127.0.0.1. On
        # 2026-09-19 that difference alone stopped every HTTP-model agent using
        # the planner: brin compared the two and concluded that a call to
        # core-api was not a call to core-api. The owner has said it
        # more than once: we do not bind to bridge addresses.
        SN_CORE_API_URL="http://127.0.0.1:${SMART_NODE_CORE_API_PORT:-8030}"
        if [[ -n "$SN_CORE_API_URL" ]]; then
            log "core-api address for companions: $SN_CORE_API_URL"
            export SN_CORE_API_URL
            # Persist it as a node contract file, the way B03 writes routing.env.
            # Passing it to the install is not enough: the SERVICES read it at
            # runtime and now refuse without it, so brin came up dead with
            # "SN_CORE_API_URL is not set. The node installer should write this to
            # the service environment". Units pick it up with EnvironmentFile=-.
            ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
                "sudo -n install -d -m 755 /etc/smart-node && printf '# Smart Node core-api contract -- written by install-on.sh.\n# Do not edit by hand. Read by companion units via EnvironmentFile=-.\n# Both names, one value. The tree is split on them: eleven files read\n# SN_CORE_API_URL (brin/main.py, voice, tracking/mcp_planner.py, the\n# acceptance tools) and twenty-odd read the bare CORE_API_URL (runner.py,\n# brin/core/*, gateway/*, node-ops, studio.sh). Writing one name leaves the\n# other half blind, which is how the runner came up with no address at all.\nSN_CORE_API_URL=%s\nCORE_API_URL=%s\n' '$SN_CORE_API_URL' '$SN_CORE_API_URL' | sudo -n tee /etc/smart-node/core-api.env >/dev/null && sudo -n chmod 644 /etc/smart-node/core-api.env" \
                >/dev/null 2>&1 \
                || log "WARN could not write /etc/smart-node/core-api.env"
        else
            log "WARN could not derive SN_CORE_API_URL; companions will refuse core-api calls"
        fi
    fi
    [[ -r "$script" ]] || die "companion script missing: $script" 5

    # differential companion update.
    # Compare the local STAGE_VERSION (the installer's own version number) to the
    # stage_version written into companions.d/NAME.json on the live node by the
    # previous run.  If they match and --force-companions is not set, run the
    # companion in --contracts-only mode (env files, unit files, MCP shims,
    # integration records) and skip the heavy install steps (venv, pip, /opt copies).
    local local_ver node_ver companion_mode src_hash node_hash srcfile
    srcfile="/var/lib/smart-node/install-src/$name.sha256"
    local_ver=$(grep -m1 '^STAGE_VERSION=' "$script" | cut -d'"' -f2 2>/dev/null || true)
    node_ver=""; node_hash=""
    src_hash=$(companion_source_hash "$script" "$sidecars" "$envstr" "$forced_mode") || src_hash=""
    if [[ -n "$local_ver" ]]; then
        # companions.d is root:smart-node 2750, so this read needs sudo. Without it
        # the read failed silently, node_ver stayed empty and every update ran every
        # companion in full, whatever had changed.
        node_ver=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n jq -r '.stage_version // empty' /etc/smart-node/companions.d/$name.json 2>/dev/null" \
            2>/dev/null || true)
        # The version number alone is a promise somebody has to keep: a companion
        # whose code changed under the same STAGE_VERSION would be skipped and the
        # node left on old code. So the skip also needs the same source hash, which
        # the installer wrote after the last install that succeeded.
        [[ -n "$src_hash" ]] && node_hash=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n cat '$srcfile' 2>/dev/null" 2>/dev/null || true)
    fi
    if [[ -n "$local_ver" && -n "$node_ver" \
          && "$local_ver" == "$node_ver" && "$FORCE_COMPANIONS" -eq 0 \
          && -n "$src_hash" && "$src_hash" == "$node_hash" ]]; then
        companion_mode="contracts-only"
    else
        companion_mode="full"
    fi

    log "--- Phase 2 companion: $name ---"
    if [[ "$companion_mode" == "contracts-only" ]]; then
        log "$name v$local_ver and its source unchanged, contracts only"
    elif [[ -n "$node_ver" && -n "$local_ver" ]]; then
        log "$name v$local_ver (was $node_ver), full install"
    else
        log "$name v${local_ver:-?} no prior record, full install"
    fi

    if [[ "$companion_mode" == "full" ]]; then
        ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f '$srcfile'" >/dev/null 2>&1 || true
    fi

    local rdir="$RSTAGE/companion-$name"
    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "rm -rf '$rdir' && mkdir -p '$rdir'" >/dev/null
    scp "${SCP_OPTS[@]}" "$script" "$P_USER@$P_IP:$rdir/$name.sh" >/dev/null

    # A sidecar is normally a directory beside the companion script. It may also
    # be written `path/from/repo/root:destname` when the thing being shipped is
    # not a companion internal -- documentation, for one, which belongs under
    # docs/ where a human reading the repo will find it, not buried in
    # companions/. The destination name is given explicitly so the companion
    # still finds it where it expects.
    local s src dest
    for s in $sidecars; do
        if [[ "$s" == *:* ]]; then
            src="$ROOT_DIR/${s%%:*}"; dest="${s##*:}"
        else
            src="$COMPANIONS_DIR/$s"; dest="$s"
        fi
        [[ -e "$src" ]] || die "companion $name sidecar missing: ${src#$ROOT_DIR/}" 5
        log "pushing sidecar ${src#$ROOT_DIR/}"
        # A destination may name a path, so that a companion reads a shared file
        # at the same relative place in the staged directory as in the source tree.
        if [[ "$dest" == */* ]]; then
            ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "mkdir -p '$rdir/$(dirname "$dest")'" >/dev/null
        fi
        scp -r "${SCP_OPTS[@]}" "$src" "$P_USER@$P_IP:$rdir/$dest" >/dev/null
    done

    local clog="$WORK_DIR/install-companion-$name.log"
    # SN_BUILD_COMMIT is always passed so companion scripts can write a
    # .sn-commit marker into their install directory for sn-version to read.
    local _core_env=""; [[ -n "${SN_CORE_API_URL:-}" ]] && _core_env="SN_CORE_API_URL=$SN_CORE_API_URL"
    local run_suffix=""
    [[ "$companion_mode" == "contracts-only" ]] && run_suffix=" --contracts-only"
    [[ -n "$forced_mode" ]] && run_suffix=" --$forced_mode"
    local runcmd="sudo -n env SN_BUILD_COMMIT=${SN_BUILD_COMMIT:-unknown} $_core_env bash '$rdir/$name.sh'${run_suffix}"
    [[ -n "$envstr" ]] && runcmd="sudo -n env SN_BUILD_COMMIT=${SN_BUILD_COMMIT:-unknown} $_core_env $envstr bash '$rdir/$name.sh'${run_suffix}"

    log "running $name${run_suffix:+ (${run_suffix# --})}"
    if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "$runcmd" 2>&1 | tee "$clog"; then
        die "Phase 2 companion $name failed (see $clog)" 5
    fi
    if [[ -z "$forced_mode" ]]; then
        log "verifying $name"
        if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
                "sudo -n env $_core_env bash '$rdir/$name.sh' --verify" 2>&1 | tee -a "$clog"; then
            die "Phase 2 companion $name --verify failed (see $clog)" 5
        fi
    fi

    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -rf '$rdir'" >/dev/null 2>&1 || true

    # No permission pass here. Every run_companion call sits in the Phase 2 block of a
    # full install, and Phase 2.9 below applies the whole declared surface once,
    # fatally, before validate. The pass that used to run after each companion cost
    # about a minute apiece on a node with large agent homes (13 companions, three
    # passes a day) and only repeated what 2.9 enforces.

    if [[ -z "$forced_mode" && -n "$src_hash" ]]; then
        ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n install -d -m 0755 -o root -g root /var/lib/smart-node/install-src && printf '%s\n' '$src_hash' | sudo -n install -m 0644 -o root -g root /dev/stdin '$srcfile'" \
            >/dev/null 2>&1 || log "WARN: could not record the source hash for $name; the next update runs it in full"
    fi

    # A forced mode installs part of a companion, never the companion: it
    # writes no stage, so recording it as done would make validate demand a
    # stage that cannot exist. That failed the first 1.12.46 clean install at
    # validate, 207/208 "selected companion stage communication missing".
    if [[ -n "$forced_mode" ]]; then
        log "companion $name ($forced_mode) OK -- not recorded as installed"
        return 0
    fi
    COMPANIONS_DONE+=("$name")
    log "companion $name OK"
}

# capture_claude_auth -- primary auth path. Once agent-runtimes has
# installed the Claude Code CLI on the node, run `claude setup-token` there so
# the node is BORN AUTHED: the sysadmin agent and the CLI roster work from
# first boot, no cloud-first constraint. The OAuth paste needs a real terminal,
# so this is attempted ONLY when the operator drives install-on.sh from an
# interactive TTY -- we push the helper and run it over `ssh -t`, bridging the
# node's prompt to the operator's own terminal. A headless run (CI, or --all
# in the background) has no controlling TTY, so we skip and the node stays
# deferred to the Studio "AI Gateway -> Connect" modal (admin-runner login).
# Non-fatal by contract: a skipped OR failed capture never aborts the install
# (the helper always exits 0 and reports CAPTURED|ALREADY|DEFERRED|FAILED).
capture_claude_auth() {
    local helper="$BOOTSTRAP_DIR/claude-auth-capture.sh"
    if [[ ! -t 0 || ! -t 1 ]]; then
        log "Claude auth capture DEFERRED -- install-on.sh has no interactive TTY;"
        log "  complete later via Studio -> AI Gateway -> Connect (admin-runner login)."
        return 0
    fi
    log "--- Claude auth capture (interactive) ---"
    local rpath="$RSTAGE/claude-auth-capture.sh"
    if ! scp "${SCP_OPTS[@]}" "$helper" "$P_USER@$P_IP:$rpath" >/dev/null; then
        log "WARN: could not push the auth-capture helper -- skipping (node stays deferred)"
        return 0
    fi
    # -t allocates a remote PTY bridged to the operator's terminal so
    # `claude setup-token` can print its URL and read the pasted code.
    ssh -t "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash '$rpath'" \
        || log "WARN: auth-capture exited non-zero over ssh -- node stays deferred"
    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f '$rpath'" >/dev/null 2>&1 || true
}

# seed_onboarding -- With the node's services up (core-api provides
# the agent + cron registries; agent-runtimes the CLI; tracking the Planner),
# push bootstrap/onboarding-seed.sh and run it: provision the sysadmin agent,
# register its wake cron, and seed the "Configure this business" Planner task.
# The node then configures itself on the next sysadmin wake.
# The seed reaches core-api over its unix socket, as root, and exits 1 when the
# socket is missing; that is treated as an error here, not a warning, because a
# zero-agent node silently blocks all work.
seed_onboarding() {
    local helper="$BOOTSTRAP_DIR/onboarding-seed.sh"
    log "--- Phase 2: onboarding seed ---"
    local rpath="$RSTAGE/onboarding-seed.sh"
    if ! scp "${SCP_OPTS[@]}" "$helper" "$P_USER@$P_IP:$rpath" >/dev/null; then
        log "WARN: could not push onboarding-seed.sh -- skipping (seed onboarding later from the panel)"
        return 0
    fi
    # srv-skeleton deploys all specialists (including configurator) to
    # /srv/agents/_specialists/ on the node. Point the seed at configurator.md
    # because that is the specialist the foreman agent is created with; checking
    # sysadmin.md (the old default) was a guard on the wrong file.
    local _seed_force=""
    if (( FORCE_ONBOARDING_RESEED )); then
        _seed_force="SN_ONBOARD_FORCE_RESEED=1"
    fi
    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
        "sudo -n env SN_SPECIALIST_MD=/srv/agents/_specialists/configurator.md $_seed_force bash '$rpath'" 2>&1 \
        | tee "$WORK_DIR/onboarding-seed.log" \
        || log "ERROR: onboarding-seed exited non-zero -- check $WORK_DIR/onboarding-seed.log (seed agents later from the panel)"
    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f '$rpath'" >/dev/null 2>&1 || true

    # Report the agent count. A count of zero after a seed is a hard failure:
    # the node cannot run the acceptance suite without at least one agent, and
    # a clean install that leaves no agents is a broken release.
    local agent_count
    # Active agents only. Without the WHERE clause the line counts archived
    # rows too and overstates the roster: the node this was noticed on has 19
    # active agents and three archived, and the install reported 22. The zero
    # test below wants the same number, since a node whose only agents are
    # archived is as unable to run the acceptance suite as one with none.
    agent_count=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
        "sudo -n -u postgres psql -d smartnode -qAt -c 'SELECT COUNT(*) FROM sn_config.agents WHERE archived_at IS NULL'" 2>/dev/null || echo "unknown")
    if [[ "$agent_count" == "0" ]]; then
        die "agent count = 0 after onboarding seed -- no agents provisioned on this node. Check $WORK_DIR/onboarding-seed.log. Re-run the seed from the panel, or re-install." 8
    elif [[ "$agent_count" == "unknown" ]]; then
        log "WARN: could not read agent count from sn_config.agents"
    else
        log "onboarding seed: $agent_count agent(s) provisioned"
    fi
}

if [[ "$MODE" != "all" && "$MODE" != "companions-only" ]]; then
    case "$MODE" in
        bootstrap-only) log "MODE=bootstrap-only -- Phase 1 complete; stopping after B06" ;;
        *)              log "MODE=default -- Phase 1 complete; stopping after B06" ;;
    esac
else
    log "=== Phase 2 (companions) ==="

    # What to install: named modules, or the answer to "how will your AI work?" (given
    # with --ai or asked at a terminal). With none of these (a script, a wizard) every
    # module, as before this existed.
    if [[ -z "$COMPONENTS" ]]; then
        if [[ -z "$AI_WAY" && -t 0 && -t 1 ]]; then
            AI_WAY=$(SN_SRC="$ROOT_DIR" python3 "$ROOT_DIR/tools/sn-components" ways) \
                || die "could not ask how the AI will work" 4
        fi
        if [[ -n "$AI_WAY" ]]; then
            COMPONENTS=$(SN_SRC="$ROOT_DIR" python3 "$ROOT_DIR/tools/sn-components" for-way "$AI_WAY") \
                || die "--ai: $AI_WAY" 4
        fi
    fi
    if [[ -n "$COMPONENTS" ]]; then
        _plan=$(SN_SRC="$ROOT_DIR" python3 "$ROOT_DIR/tools/sn-components" plan "$COMPONENTS") \
            || die "--components: $COMPONENTS" 4
        echo "$_plan"
        WANTED_COMPONENTS=" $(echo "$_plan" | sed -n 's/^order: *//p') "
    fi

    # gateway: optional Anthropic/OpenRouter keys -> .env; absent, degraded.
    # Threaded space-separated through `sudo env` (run_companion).
    GATEWAY_ENV=""
    [[ -n "$ANTHROPIC_KEY" ]] && GATEWAY_ENV="SMART_NODE_ANTHROPIC_API_KEY=$ANTHROPIC_KEY"
    [[ -n "$OPENROUTER_KEY" ]] && GATEWAY_ENV="${GATEWAY_ENV:+$GATEWAY_ENV }SMART_NODE_OPENROUTER_API_KEY=$OPENROUTER_KEY"

    # registry: ghcr.io is private (matching the repository). Thread GITHUB_TOKEN
    # to the three companions that pull container images from it. Missing token
    # is not fatal -- they fall back to the tarball path.
    REGISTRY_ENV=""
    [[ -n "${GITHUB_TOKEN:-}" ]] && REGISTRY_ENV="GITHUB_TOKEN=${GITHUB_TOKEN}"

    # Dependency-ordered. Each companion reads the persistence/routing/
    # secrets contracts the lower stages wrote on the node, so only the
    # operator-supplied secrets need env threading.
    # core-api leads: the sn_core/sn_config schema is baked by B02, so this
    # installs only the FastAPI service that serves the panel write-side.
    # It binds the Docker bridge gateway at port 8030 (gateway uses :8000 -- no clash) and writes
    # its own pg_ident map line before starting.
    # admin-runner leads: the privileged operations service. It has no
    # companion dependencies (postgres + the sn_config/sn_core schema from B02
    # are all it needs) and MUST precede core-api -- core-api's cron-reconcile
    # timer (installed + enabled by the core-api companion) delegates the
    # /etc/cron.d write to admin-runner via admin_runner.resolve_endpoint(),
    # which reads the integration row admin-runner seeds. Absent it, the first
    # reconcile tick 502s and scheduled agent wakes never materialise.
    run_companion admin-runner   "admin-runner"
    # node-ops / gateway-ops are SIBLING directories of the vendored snapshots,
    # not part of them, so they have to be named here or they never reach the
    # node. Both companions require their file with `die`, so a missing sidecar
    # is not a degraded install -- it is an install that stops at this line.
    # Shipped in 1.9.21 without these two entries; node-ops and gateway-ops
    # existed, but the wiring inside each companion was checked and the
    # DELIVERY of the files was not.
    # memory BEFORE core-api. core-api is a host process, so it reaches
    # Mesh by the container's IP on the smart-node network and writes that
    # address into its own unit at install time. Installed first, it finds no
    # container, ships without the address, and every knowledge write on the
    # fresh node lands in the database and nowhere else -- the exact defect
    # the release notes record, reintroduced on day one of every new node. Neither
    # companion declares the other as a dependency (memory needs B02+B03,
    # core-api needs B02), and core-api still precedes gateway, which is the
    # ordering the header explains.
    run_companion memory         "mesh _shared"                   "$REGISTRY_ENV"
    CORE_API_ENV=""
    [[ -n "$SECRETS_OPERATOR_USERS" ]] \
        && CORE_API_ENV="SMART_NODE_SECRETS_OPERATOR_USERS=$SECRETS_OPERATOR_USERS"
    run_companion core-api       "core-api srv-skeleton node-ops integrations/gdrive-mcp:gdrive-mcp companions/tools:tools _shared" "$CORE_API_ENV"
    # Flint is on every node (core in its manifest): the API-key way runs on it. Before
    # the gateway, which turns Flint on only when it finds it there. It used to be
    # skipped on an install with no module list, so a fresh node had no Flint.
    if component_wanted flint; then
        run_companion flint ""
    fi
    run_companion gateway        "gateway gateway-ops companions/core-api/lib/supervisor.py:core-api/lib/supervisor.py" "$GATEWAY_ENV"
    run_companion documents      "doci docs/node:doci-seed _shared" "$REGISTRY_ENV"
    run_companion agent-runtimes ""
    # W1: with the Claude CLI now on the node, born-auth it in-place when the
    # operator is at an interactive terminal (non-fatal; defers if headless).
    if [[ -z "$WANTED_COMPONENTS" || "$WANTED_COMPONENTS" == *" agent-runtimes "* ]]; then
        capture_claude_auth
    fi
    # `tracking` (the directory) carries mcp_planner.py, which tracking.sh
    # installs as the planner MCP server. It was added with the MCP itself and
    # not listed here, so a clean install died at "planner MCP source missing"
    # while every existing node was fine -- they run the companion from a tree
    # where the file is already beside it. Found by the first acceptance install
    # on a throwaway node, 2026-08-30.
    run_companion tracking       "planner tracking _shared"               "$REGISTRY_ENV"
    run_companion dispatcher     "brin _shared"
    # Workflows owns the flow and cron registration; the poller helper and
    # scripts travel as an optional sidecar. It defers cleanly until HITL is
    # installed, and hitl.sh retries after creating the native caller.
    run_companion workflows      "rein workflows-secretary.sh flows _shared companions/hitl:hitl"

    # communication needs at least ONE channel, not one SPECIFIC channel.
    # It used to demand Telegram, so a mail-only node could not install the
    # component at all -- which is what forced hand-assembly on field-node.
    # Telegram and external mail are both optional and both sufficient; the
    # companion itself refuses only when nothing at all can be wired.
    COMM_ENV=""
    [[ -n "$NOTIFY_EMAIL_SENDER" ]] \
        && COMM_ENV="$COMM_ENV SMART_NODE_NOTIFY_EMAIL_SENDER=$NOTIFY_EMAIL_SENDER"
    [[ -n "$TG_TOKEN" && -n "$TG_CHANNEL_ID" ]] \
        && COMM_ENV="$COMM_ENV SMART_NODE_TG_TOKEN=$TG_TOKEN SMART_NODE_TG_CHANNEL_ID=$TG_CHANNEL_ID"
    if [[ -n "$MAIL_HOST" ]]; then
        COMM_ENV="$COMM_ENV SMART_NODE_MAIL_HOST=$MAIL_HOST"
        # Reading and sending are separate service names on most mail estates,
        # and the companion has always supported the split -- the installer just
        # could not express it, so every node had to be corrected by hand after
        # install. Given, they win; omitted, both fall back to MAIL_HOST.
        [[ -n "$MAIL_IMAP_HOST" ]] && COMM_ENV="$COMM_ENV SMART_NODE_MAIL_IMAP_HOST=$MAIL_IMAP_HOST"
        [[ -n "$MAIL_SMTP_HOST" ]] && COMM_ENV="$COMM_ENV SMART_NODE_MAIL_SMTP_HOST=$MAIL_SMTP_HOST"
        [[ -n "$MAIL_ACCOUNT" ]]  && COMM_ENV="$COMM_ENV SMART_NODE_MAIL_ACCOUNT=$MAIL_ACCOUNT"
        [[ -n "$MAIL_PASSWORD" ]] && COMM_ENV="$COMM_ENV SMART_NODE_MAIL_ACCOUNT_PW=$MAIL_PASSWORD"
        if [[ -n "$MAIL_ACCOUNTS_FILE" ]]; then
            # The file lives on the CONTROLLER; push it and hand the companion
            # the node-side path.
            [[ -r "$MAIL_ACCOUNTS_FILE" ]] \
                || die "--mail-accounts-file $MAIL_ACCOUNTS_FILE is not readable" 4
            scp "${SCP_OPTS[@]}" "$MAIL_ACCOUNTS_FILE" \
                "$P_USER@$P_IP:$RSTAGE/sn-mail-accounts.env" >/dev/null \
                || die "could not push --mail-accounts-file to the node" 5
            ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n chmod 0600 $RSTAGE/sn-mail-accounts.env" >/dev/null 2>&1 || true
            COMM_ENV="$COMM_ENV SMART_NODE_MAIL_ACCOUNTS_FILE=$RSTAGE/sn-mail-accounts.env"
        fi
    fi
    if [[ -n "$COMM_ENV" ]]; then
        # comm-tool-usage.md is the contract the agent is given when the tool
        # is granted; _shared carries the registration helper. Missing either,
        # the tool is never registered and the written instruction never
        # reaches a node at all.
        run_companion communication "communication comm-tool-usage.md _shared" "${COMM_ENV# }"
        [[ -n "$MAIL_ACCOUNTS_FILE" ]] && ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n rm -f $RSTAGE/sn-mail-accounts.env" >/dev/null 2>&1 || true
    else
        # No channel credentials given on this run. If the companion was
        # previously installed, run it anyway: it will carry forward its
        # own configuration (adopt_telegram_from_env / adopt_email_from_env
        # in the companion) and optionally read credentials from the node's
        # secret store (defect 3). A previously-installed companion
        # that is silently skipped on an update means mail/telegram fixes
        # in this release never reach the node and nobody is told.
        # sudo, because the answer is otherwise always no. B04 creates
        # /etc/smart-node/companions.d as drwxr-s--- root:smart-node, and the
        # ssh user is an ordinary admin account that need not be in that group:
        # it cannot stat anything inside, so an unprivileged `test -f` reports
        # the companion as never installed on a node where it is installed and
        # running. That is how this probe behaved on the node the defect was
        # reported from: the file was there, dated the day before, and the
        # update skipped the companion exactly as 1.12.37 did.
        _comm_prev=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n test -f /etc/smart-node/companions.d/communication.json && echo yes" \
            2>/dev/null || true)
        if [[ "$_comm_prev" == "yes" ]]; then
            log "communication companion previously installed -- running without new credentials (it will carry forward its own channels and check the secret store)"
            run_companion communication "communication comm-tool-usage.md _shared" ""
            # Remember that it ran. The closing report decides what to say about
            # this companion from the credentials on the command line, which
            # stopped describing the run the moment a companion could be carried
            # forward without them.
            COMM_RAN=1
        else
            log "communication companion not installed -- no channel given (--tg-token/--tg-channel-id or --mail-host); install it from the B06 panel"
            COMPANIONS_ABSENT+=("communication")
            # The agent CLI is not a channel. Every agent is told to use
            # /srv/tools/comm/comm.sh and agents message each other through it,
            # so it is installed on every node, channel or not.
            log "communication: installing the agent CLI (comm.sh) and its tool contract only"
            run_companion communication "communication comm-tool-usage.md _shared" "" agent-cli
        fi
    fi

    # voice: optional Gemini API key -> .env; absent, installs degraded
    # (browser UI loads, live conversation fails).
    # Skipped entirely when the seed manifest sets voice=disconnected.
    if [[ "$SEED_MANIFEST_VOICE_SKIP" -eq 1 ]]; then
        log "voice companion SKIPPED -- seed manifest: voice=disconnected"
        COMPANIONS_ABSENT+=("voice")
    else
        VOICE_ENV=""
        [[ -n "${GEMINI_KEY:-}" ]] && VOICE_ENV="SMART_NODE_GEMINI_API_KEY=$GEMINI_KEY"
        run_companion voice          "voice"                                   "$VOICE_ENV"
    fi

    # Studio: the native end-user panel. Installs LAST -- it proxies core-api,
    # doci, planner and brin, all of which are up by now. No keys/env needed.
    run_companion studio         "studio"

    # HITL is opt-in because the WebAuthn origin must have working DNS/TLS.
    # Once installed, keep it in the update path even when the operator omits
    # the option; the companion reads its recorded origin from hitl.env.
    if [[ -n "$HITL_ORIGIN" ]]; then
        _hitl_env="SMART_NODE_HITL_ORIGIN=$HITL_ORIGIN"
        [[ "$HITL_ADOPT_LEGACY" -eq 0 ]] || _hitl_env="$_hitl_env SMART_NODE_HITL_ADOPT_LEGACY=1"
        run_companion hitl "hitl-upstream hitl _shared" "$_hitl_env"
    else
        _hitl_prev=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n test -f /etc/smart-node/companions.d/hitl.json && echo yes" \
            2>/dev/null || true)
        [[ "$_hitl_prev" != yes ]] || run_companion hitl "hitl-upstream hitl _shared"
    fi

    # W3: node configures itself. All prerequisite companions (core-api,
    # agent-runtimes, tracking) are up; seed the foreman agent + wake cron +
    # "Configure this business" task. Fatal when no agents are provisioned:
    # a zero-agent node cannot run the acceptance suite.
    seed_onboarding

    # Deterministically materialise /etc/cron.d before validate. cron-reconcile
    # runs on a timer (OnBootSec=2min / OnUnitActiveSec=5min); during the install
    # window its first ticks can race service/DB settling (a transient 502 on the
    # core-api -> admin-runner render), leaving the unit's LAST result failed --
    # which validate then flags even though the timer self-heals within 5min.
    # With every companion up and the wake cron just seeded, kick one synchronous
    # run so /etc/cron.d is materialised and the unit's last result is success.
    # `systemctl start` on a Type=oneshot blocks and returns the ExecStart status,
    # so a transient failure is retried; non-fatal (validate remains the gate).
    log "kicking one cron-reconcile run (materialise /etc/cron.d before validate)"
    for _i in 1 2 3 4 5; do
        if ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
                "sudo -n systemctl start smart-node-cron-reconcile.service" >/dev/null 2>&1; then
            log "cron-reconcile materialised /etc/cron.d"
            break
        fi
        log "cron-reconcile not ready yet (attempt $_i/5) -- retrying in 5s"
        sleep 5
    done

    # --------------------------------------------------------------------
    # Phase 2.9 permissions -- the closing step of the install.
    #
    # Companions create directories; they do not decide their permissions.
    # This applies bootstrap/permissions.list over whatever they left and
    # REFUSES if a writable path is not declared there. It runs before
    # validate so a refusal fails the install rather than being reported
    # after the node is called healthy.
    # --------------------------------------------------------------------
    # The node-local settings layer: every smart-node unit loads
    # /etc/smart-node/local/<unit>.env after the release's own files, and the
    # installer never writes there, so what an operator set survives this
    # update (bootstrap/local-layer.sh). Before permissions, which declares
    # the directory owner-only.
    log "=== local settings layer ==="
    if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n /usr/local/lib/smart-node/local-layer.sh" 2>&1 | tee "$WORK_DIR/local-layer.log"; then
        die "local settings layer failed (see $WORK_DIR/local-layer.log)" 7
    fi

    log "=== permissions (apply the declared surface) ==="
    if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n /usr/local/lib/smart-node/permissions.sh" 2>&1 | tee "$WORK_DIR/permissions.log"; then
        die "permissions stage refused (see $WORK_DIR/permissions.log): a writable path is not declared in permissions.list" 7
    fi
    log "permissions applied"

    # The class-verify timer may already have fired by now. It counts 15 min
    # (+ up to 15 random) from core-api's install, and a --all whose image
    # builds run long passes that before validate; /etc/smart-node/classes.json
    # is laid only in phase 3.5 below, so that firing exits 2 "not there" and
    # leaves the unit failed, and validate then fails a healthy node for it
    # (sn-clean42, 1.12.42). Clear it ONLY while the declaration is
    # absent: that failure is this ordering, not a finding. With the file
    # present a failed unit is real and validate must see it.
    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP"         "test -f /etc/smart-node/classes.json || sudo -n systemctl reset-failed smart-node-classes-verify.service"         >/dev/null 2>&1 || true

    # --------------------------------------------------------------------
    # Phase 3 validate
    # --------------------------------------------------------------------
    log "=== Phase 3 (validate) ==="
    V_LOG="$WORK_DIR/validate.log"

    # Persist what this install selected before validation. validate.sh used to
    # infer its whole scope from existing *.state and manifest rows, so omitting
    # a companion silently removed every one of its checks and still produced
    # an unqualified PASS. The scope is non-secret and remains on the node so a
    # later standalone validate run answers the same question.
    _validation_selected=$(IFS=,; echo "${COMPANIONS_DONE[*]}")
    _validation_absent=$(IFS=,; echo "${COMPANIONS_ABSENT[*]}")
    VALIDATION_SCOPE_LOCAL="$WORK_DIR/validation-scope.env"
    {
        echo "# Smart Node validation scope -- written by install-on.sh@$SCRIPT_VERSION."
        echo "# Selected companions must have a done stage; absent companions are outside the artifact denominator."
        echo "SN_VALIDATION_SELECTED_STAGES=$_validation_selected"
        echo "SN_VALIDATION_OMITTED_STAGES=$_validation_absent"
    } > "$VALIDATION_SCOPE_LOCAL"
    scp "${SCP_OPTS[@]}" "$VALIDATION_SCOPE_LOCAL" \
        "$P_USER@$P_IP:$RSTAGE/validation-scope.env" >/dev/null
    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
        "sudo -n install -m 0644 -o root -g root $RSTAGE/validation-scope.env /etc/smart-node/validation-scope.env" \
        >/dev/null

    scp "${SCP_OPTS[@]}" "$VALIDATE_SCRIPT" "$P_USER@$P_IP:$RSTAGE/validate.sh" >/dev/null
    # Beside it, so validate can look at the node's PUBLIC face too. It
    # goes to /tmp rather than being installed, for the same reason validate.sh
    # does: neither is something a node should carry between runs. Without it
    # validate simply says nothing about public names -- which is the behaviour
    # on a node whose installer predates this.
    if [[ -r "$ROOT_DIR/tools/public-names.sh" ]]; then
        scp "${SCP_OPTS[@]}" "$ROOT_DIR/tools/public-names.sh" "$P_USER@$P_IP:$RSTAGE/public-names.sh" >/dev/null
        ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n chmod +x $RSTAGE/public-names.sh" >/dev/null 2>&1 || true
    fi
    if ! ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n bash $RSTAGE/validate.sh" 2>&1 | tee "$V_LOG"; then
        die "Phase 3 validate reported failures (see $V_LOG and the node's validation-report.md)" 6
    fi
    ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f $RSTAGE/validate.sh $RSTAGE/public-names.sh" >/dev/null 2>&1 || true
    log "Phase 3 validate OK"

    # Validate reads what the node recorded; a skipped route records nothing. So ask the
    # proxy itself. A new node with no Studio is a failed install; an update warns,
    # its node may have been reached another way for years.
    if [[ " ${COMPANIONS_DONE[*]} " == *" studio "* ]] && ! studio_answers; then
        _no_ws="the Studio does not answer on this node: pass --node-domain, and point studio.<domain> at it"
        [[ "$_WAS_FRESH" == "true" ]] && die "$_no_ws" 6
        log "WARN: $_no_ws"
    fi

    # Release evidence. Every release gate reads the REPOSITORY, so a
    # defect that only exists against an empty database is invisible to all of
    # them -- which is how seven of them shipped in 1.12.0..1.12.5 while the
    # fleet updated flawlessly. This is the one fact no repository check can
    # produce: a node was built from nothing, on this release, and validate
    # passed. tools/release.sh refuses to PUBLISH a release without it.
    #
    # Written only for a clean install. An update proves the update path and
    # says nothing about the install path, so it must not be able to satisfy
    # the gate.
    if [[ "${_WAS_FRESH:-false}" == "true" && -f "$ROOT_DIR/RELEASE" ]]; then
        _ev_rel=$(head -1 "$ROOT_DIR/RELEASE" | tr -d '[:space:]')
        _ev_commit=$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
        _ev_checks=$(grep -oE 'Validation PASS -- [0-9]+/[0-9]+' "$V_LOG" 2>/dev/null | tail -1 | grep -oE '[0-9]+/[0-9]+' || echo unknown)
        _ev_file="$ROOT_DIR/release-evidence/$_ev_rel.json"

        # Guard: only write evidence when HEAD is the tagged commit for
        # this release. The RELEASE file names the last cut, not the code in the
        # tree -- a development build on commit N+30 would otherwise overwrite the
        # genuine proof and satisfy the publication gate with the wrong code.
        _ev_tag="v$_ev_rel"
        _tag_commit=$(git -C "$ROOT_DIR" rev-parse --short "${_ev_tag}^{}" 2>/dev/null || echo "")

        if [[ -z "$_tag_commit" ]]; then
            # Tag does not exist yet -- this is a pre-release development build.
            log "WARN: skipping release evidence -- tag $_ev_tag does not exist"
            log "      Run install-on.sh from the tagged commit to record clean-install proof for $_ev_rel."
        elif [[ "$_ev_commit" != "$_tag_commit" ]]; then
            # HEAD is not the tagged release commit -- development build between two releases.
            log "WARN: skipping release evidence -- HEAD ($_ev_commit) is not the $_ev_rel release commit ($_tag_commit)"
            log "      Run install-on.sh from commit $_tag_commit (tag $_ev_tag) to record clean-install proof."
        elif [[ -f "$_ev_file" ]]; then
            # Right commit, but evidence already exists. Never overwrite; show both when they differ.
            _existing_commit=$(grep -oE '"commit": "[^"]+' "$_ev_file" | head -1 | grep -oE '[^"]+$' || echo unknown)
            if [[ "$_existing_commit" == "$_ev_commit" ]]; then
                log "release evidence already exists for $_ev_rel (commit $_ev_commit) -- not overwriting"
            else
                log "ERROR: release-evidence/$_ev_rel.json exists but records a different commit"
                log "       Existing: $_existing_commit  This run: $_ev_commit"
                log "       Existing file content: $(cat "$_ev_file")"
                log "       This run would have written: release=$_ev_rel commit=$_ev_commit validate=$_ev_checks"
                log "       File left unchanged. Investigate before publishing."
            fi
        else
            # New evidence file for the correct tagged commit.
            mkdir -p "$ROOT_DIR/release-evidence"
            cat > "$_ev_file" <<EVEOF
{
  "release": "$_ev_rel",
  "commit": "$_ev_commit",
  "clean_install": true,
  "node": "$NAME",
  "provider": "$PROVIDER",
  "validate": "$_ev_checks",
  "at": "$(date -Is)"
}
EVEOF
            log "release evidence written: release-evidence/$_ev_rel.json (clean install, validate $_ev_checks)"
        fi
    elif [[ "${_WAS_FRESH:-false}" != "true" ]]; then
        log "no release evidence written -- this was an update, and only a clean install proves the install path"
    fi

    # --------------------------------------------------------------------
    # Phase 3.5 node version + capability catalogue
    #   Lay sn-version, bake the release capability doc into
    #   /etc/smart-node/capabilities.md (core-api reads it into the CONFIGURATOR's
    #   prompt -- agent_fs.py gates it on that specialist, it is not in every
    #   agent's prompt), ship the class declaration, and stamp the release lock.
    # --------------------------------------------------------------------
    log "=== node version + capabilities ==="
    _NV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [[ -f "$_NV_ROOT/node-versioning/sn-version" ]]; then
        scp "${SCP_OPTS[@]}" "$_NV_ROOT/node-versioning/sn-version" "$P_USER@$P_IP:$RSTAGE/sn-version" >/dev/null
        ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n install -D -m 0755 $RSTAGE/sn-version /opt/smart-node/bin/sn-version && sudo -n rm -f $RSTAGE/sn-version" >/dev/null
        log "installed /opt/smart-node/bin/sn-version"
    else
        log "WARN: node-versioning/sn-version missing -- skipping version tool"
    fi
    if [[ -f "$_NV_ROOT/node-versioning/capabilities.md" ]]; then
        scp "${SCP_OPTS[@]}" "$_NV_ROOT/node-versioning/capabilities.md" "$P_USER@$P_IP:$RSTAGE/capabilities.md" >/dev/null
        # The doc carries __CORE_API_URL__, never a literal address. core-api
        # reads this file into the configurator's prompt, so a stale address here is
        # told to an agent as fact. Substitute the address this install derived, and
        # refuse if the token survives rather than shipping it into a prompt.
        ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sed -i 's|__CORE_API_URL__|${SN_CORE_API_URL}|g' $RSTAGE/capabilities.md" >/dev/null
        if ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "grep -q '__CORE_API_URL__' $RSTAGE/capabilities.md"; then
            die "capabilities.md still contains __CORE_API_URL__ after substitution -- SN_CORE_API_URL was empty"
        fi
        ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n install -D -m 0644 $RSTAGE/capabilities.md /etc/smart-node/capabilities.md && sudo -n rm -f $RSTAGE/capabilities.md" >/dev/null
        log "installed /etc/smart-node/capabilities.md (core-api at ${SN_CORE_API_URL})"
    else
        log "WARN: node-versioning/capabilities.md missing -- node has no capability catalogue"
    fi
    # The release's module manifests: core-api reads them against companions.d to
    # tell each agent which modules this node has and which it has not.
    if compgen -G "$_NV_ROOT/companions/*.manifest.json" >/dev/null; then
        tar -C "$_NV_ROOT/companions" -cf - $(cd "$_NV_ROOT/companions" && ls *.manifest.json)             | ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n install -d -m 0755 /etc/smart-node/manifests && sudo -n tar -C /etc/smart-node/manifests -xf - && sudo -n chmod 0644 /etc/smart-node/manifests/*.manifest.json"             && log "installed /etc/smart-node/manifests (module catalogue)"
    fi
    # The owner's way back in on a node with no password (a local one).
    scp "${SCP_OPTS[@]}" "$_NV_ROOT/tools/smart-node-signin" "$P_USER@$P_IP:$RSTAGE/smart-node-signin" >/dev/null \
        && ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n install -m 0750 $RSTAGE/smart-node-signin /usr/local/sbin/smart-node-signin && sudo -n rm -f $RSTAGE/smart-node-signin" >/dev/null \
        && log "installed /usr/local/sbin/smart-node-signin"
    # Which CANON built this node. This is the question an operator with ten or
    # a hundred nodes actually asks -- "what is installed on that one" -- and the
    # node could not answer it. sn-version's node_version counts how many times
    # THIS node changed, so every fresh node reports 1.0.0 whatever canon
    # produced it, and only the fingerprint differs: opaque, and not comparable
    # between nodes. Two different questions were sharing one number.
    #
    # So the release name comes FROM the repository, is stamped in at install and
    # does not move afterwards, while the node's own change counter stays beside
    # it and answers the other question: what has been touched here since.
    if [[ -f "$ROOT_DIR/RELEASE" ]]; then
        _rel=$(head -1 "$ROOT_DIR/RELEASE" | tr -d '[:space:]')
        _relcommit=$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
        if ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
                "printf 'release=%s\ncommit=%s\ninstalled_at=%s\n' '$_rel' '$_relcommit' \"\$(date -Is)\" \
                 | sudo -n tee /etc/smart-node/release >/dev/null" >/dev/null 2>&1; then
            log "node built from canon release $_rel ($_relcommit)"
        else
            log "WARN: could not stamp the canon release onto the node"
        fi
    else
        log "WARN: no RELEASE file in the installer -- this node will not record which canon built it"
        _rel=""; _relcommit=""
    fi
    # The class declaration: where every class of thing lives on a node of THIS
    # release. It is stamped with the same release just written to
    # /etc/smart-node/release, and the node-side verifier compares the two
    # before it compares anything else -- an unstamped or mismatched file makes
    # every later check a comparison against another release's truth, and a PASS
    # there would be a lie. capabilities.md has no such stamp, and both live
    # nodes have quietly carried a stale copy of it for weeks; this is that
    # lesson applied rather than repeated.
    if [[ -f "$_NV_ROOT/node-versioning/classes.json" ]]; then
        _cj="$(mktemp)"
        if python3 - "$_NV_ROOT/node-versioning/classes.json" "$_cj" "${_rel:-unstamped}" "${_relcommit:-unknown}" <<'PY'
import json, sys
src, dst, rel, commit = sys.argv[1:5]
with open(src, encoding="utf-8") as f:
    d = json.load(f)
d["release"], d["commit"] = rel, commit
with open(dst, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
PY
        then
            scp "${SCP_OPTS[@]}" "$_cj" "$P_USER@$P_IP:$RSTAGE/classes.json" >/dev/null
            ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n install -D -m 0644 $RSTAGE/classes.json /etc/smart-node/classes.json && sudo -n rm -f $RSTAGE/classes.json" >/dev/null
            log "installed /etc/smart-node/classes.json (release ${_rel:-unstamped})"
            # Run the check once, now that the declaration exists. Findings do
            # NOT fail the install: a document that disagrees with the node is a
            # documentation defect, not a broken node, and hiding it would be
            # worse than either. The output goes to the operator's screen and
            # the node's journal via the timer's first firing.
            if _cv_out=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
                    "sudo -n python3 /opt/smart-node/ops/classes-verify.py" 2>&1); then
                log "class declaration: $(printf '%s' "$_cv_out" | head -1)"
            else
                log "class declaration: the node does not match it --"
                printf '%s\n' "$_cv_out" | sed 's/^/           /' | head -12
            fi
        else
            log "WARN: could not stamp classes.json -- node will have no class declaration"
        fi
        rm -f "$_cj"
    else
        log "WARN: node-versioning/classes.json missing -- the node cannot check its own registries"
    fi
    # `record`, not `init`. This runs at the end of a first install and of an
    # update alike, and init overwrites the lock every time: it reset the
    # release number to 1.0.0 and replaced the history with a single "first
    # pinned composition" entry, so no update was ever recorded and
    # `sn-version check` could only ever answer UNCHANGED.
    # The output is logged, not swallowed: this is the node's release number.
    if _nv_out=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "sudo -n /opt/smart-node/bin/sn-version record" 2>&1); then
        while IFS= read -r _nv_line; do
            [[ -n "$_nv_line" ]] && log "sn-version: $_nv_line"
        done <<<"$_nv_out"
    else
        log "WARN: sn-version record failed -- this node's release was NOT updated:"
        while IFS= read -r _nv_line; do
            [[ -n "$_nv_line" ]] && log "WARN: sn-version: $_nv_line"
        done <<<"$_nv_out"
    fi
    # agents composed earlier in this install predate the capability doc; recompose
    # them so every agent prompt carries the freshly-baked catalogue.
    if [[ -f "$_NV_ROOT/node-versioning/recompose-agents.py" ]]; then
        scp "${SCP_OPTS[@]}" "$_NV_ROOT/node-versioning/recompose-agents.py" "$P_USER@$P_IP:$RSTAGE/recompose-agents.py" >/dev/null
        if ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n python3 $RSTAGE/recompose-agents.py"; then
            ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f $RSTAGE/recompose-agents.py" >/dev/null
            log "recomposed agents with capability catalogue"
        else
            die "agent recompose after capability bake did not complete" 6
        fi
    fi

    # Phase 3.6: seed manifest budgets (if any).
    # They go through core-api, the way the owner sets a limit, not into the
    # budgets table by SQL. The row there is only the meter; the limit the
    # gateway enforces is in the limits registry, and core-api writes both in
    # one transaction. A row inserted by SQL reached the table and not the
    # registry, so a fresh node ran without its declared hard limit.
    # Re-runs are idempotent: an existing budget is updated to the manifest.
    if [[ -n "${MANIFEST_BUDGETS_JSON:-}" && "$MANIFEST_BUDGETS_JSON" != "null" ]]; then
        _bcount=$(printf '%s' "$MANIFEST_BUDGETS_JSON" | jq 'length')
        if [[ "$_bcount" -gt 0 ]]; then
            log "=== Phase 3.6 (seed manifest budgets: $_bcount row(s)) ==="
            scp "${SCP_OPTS[@]}" "$SCRIPT_DIR/seed-budgets.py" \
                "$P_USER@$P_IP:$RSTAGE/seed-budgets.py" >/dev/null \
                || die "Phase 3.6: could not send seed-budgets.py to the node; a declared spend limit must not fail silently" 32
            # The rows travel on stdin. Root runs the helper because root on the
            # core-api socket is the identity the install already acts under.
            if _bout=$(printf '%s' "$MANIFEST_BUDGETS_JSON" | ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
                    "sudo -n python3 $RSTAGE/seed-budgets.py" 2>&1); then
                while IFS= read -r _bline; do
                    [[ -n "$_bline" ]] && log "$_bline"
                done <<<"$_bout"
                log "Phase 3.6 budgets applied"
                ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n rm -f $RSTAGE/seed-budgets.py" >/dev/null 2>&1 || true
            else
                printf '%s\n' "$_bout" | sed 's/^/           /' >&2
                die "Phase 3.6: seed-manifest budgets failed to apply; a declared spend limit must not fail silently" 32
            fi
        fi
    fi
fi

# ========================================================================
# Identity state file
#
# Write the resolved identity values to /etc/smart-node/node-identity.state
# on the node after every successful install and update. A subsequent update
# run reads this file if it exists; it falls back to routing.env + hostname
# for nodes that predate this file. Written regardless of --all / mode:
# any run that reaches here completed its bootstrap stages.
# ========================================================================
if [[ -n "$P_IP" && -n "$P_USER" ]]; then
    _ni_fqdn="${NODE_FQDN:-$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "hostname -f 2>/dev/null" 2>/dev/null || true)}"
    _ni_domain=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
        "sudo -n sed -n 's/^SMART_NODE_DOMAIN=//p' /etc/smart-node/routing.env 2>/dev/null | tail -1" \
        2>/dev/null || true)
    _ni_pub=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
        "sudo -n sed -n 's/^SMART_NODE_PUBLIC_DOMAIN=//p' /etc/smart-node/routing.env 2>/dev/null | tail -1" \
        2>/dev/null || true)
    if ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
            "printf '# Smart Node node identity -- written by install-on.sh after a successful install/update.\n# Read by the next update run to preserve identity when no identity flags are given.\n# Do not edit by hand. To change identity on an update, pass --set-identity.\nNODE_FQDN=%s\nNODE_DOMAIN=%s\nPUBLIC_DOMAIN=%s\n' \
             '${_ni_fqdn}' '${_ni_domain}' '${_ni_pub}' \
             | sudo -n tee /etc/smart-node/node-identity.state >/dev/null" >/dev/null 2>&1; then
        log "identity state written to /etc/smart-node/node-identity.state"
    else
        log "WARN: could not write identity state file (next update will fall back to routing.env + hostname)"
    fi
fi

# ========================================================================
# Output
# ========================================================================
TIMESTAMP=$(date -Iseconds)

# Completed-stage list. Phase 1 always; the Phase 2 companions actually
# installed + validate are appended when --all ran (validate is reached
# only after every companion passed, and dies on failure -- so if we are
# here it passed).
STAGES=(P A B01 B02 B03 B04 B05 B06)
if [[ "$MODE" == "all" ]]; then
    STAGES+=("${COMPANIONS_DONE[@]}" validate)
fi
# What the NODE ended up with, not what was typed on this command line.
# The public name is carried forward by B03, so an update that did not restate
# it still has one -- and a report saying "(none)" there would be the same
# comfortable lie the whole task is about. Read it back from the contract.
PUBLIC_DOMAIN_EFFECTIVE=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" \
    "sudo -n sed -n 's/^SMART_NODE_PUBLIC_DOMAIN=//p' /etc/smart-node/routing.env 2>/dev/null | tail -1" \
    2>/dev/null || true)

STAGES_JSON=$(printf '"%s", ' "${STAGES[@]}"); STAGES_JSON="${STAGES_JSON%, }"
STAGES_TXT=$(printf '%s -> ' "${STAGES[@]}"); STAGES_TXT="${STAGES_TXT% -> }"

# Whether any companion was skipped that was already installed on the node.
# Such a run is PARTIAL: the operator's update did not reach every component
# and the report must say so. The exit code is 9 so automated tooling can
# distinguish full success (0), partial (9), companion failure (5), and
# validation failure (6) -- all distinct values .
_HAS_SKIPPED=0
(( ${#COMPANIONS_SKIPPED[@]} > 0 )) && _HAS_SKIPPED=1

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
    COMPANIONS_JSON=$(printf '"%s", ' "${COMPANIONS_DONE[@]}")
    COMPANIONS_JSON="[${COMPANIONS_JSON%, }]"
    SKIPPED_JSON=$(printf '"%s", ' "${COMPANIONS_SKIPPED[@]}")
    SKIPPED_JSON="[${SKIPPED_JSON%, }]"
    if (( ${#COMPANIONS_ABSENT[@]} > 0 )); then
        ABSENT_JSON=$(printf '"%s", ' "${COMPANIONS_ABSENT[@]}")
        ABSENT_JSON="[${ABSENT_JSON%, }]"
    else
        ABSENT_JSON="[]"
    fi
    [[ "$MODE" == "all" ]] && VALIDATED=true || VALIDATED=false
    [[ -n "$SEED_MANIFEST" ]] && SEED_MANIFEST_JSON=true || SEED_MANIFEST_JSON=false
    _status="ok"
    (( _HAS_SKIPPED )) && _status="partial"
    cat <<JSONEOF
{
  "status": "$_status",
  "provider": "$PROVIDER",
  "provider_id": "$P_PROVIDER_ID",
  "ip": "$P_IP",
  "ssh_user": "$P_USER",
  "ssh_key": "$SSH_KEY_PRIV",
  "allow_from": "$ALLOW_FROM",
  "node_fqdn": "$NODE_FQDN_EFFECTIVE",
  "vpn_endpoint": "${VPN_ENDPOINT:-$P_IP:51820}",
  "node_domain": "$NODE_DOMAIN",
  "public_domain": "$PUBLIC_DOMAIN_EFFECTIVE",
  "mode": "$MODE",
  "stages_done": [$STAGES_JSON],
  "companions": $COMPANIONS_JSON,
  "companions_skipped": $SKIPPED_JSON,
  "companions_absent": $ABSENT_JSON,
  "validated": $VALIDATED,
  "seed_manifest": $SEED_MANIFEST_JSON,
  "completed_at": "$TIMESTAMP",
  "script_version": "$SCRIPT_VERSION"
}
JSONEOF
else
    if [[ "$MODE" == "all" ]]; then
        if (( _HAS_SKIPPED )); then
            PHASE_LINE="Phases 1-3 PARTIAL -- bootstrap + ${#COMPANIONS_DONE[@]} companions updated, ${#COMPANIONS_SKIPPED[@]} SKIPPED + validate."
        elif (( ${#COMPANIONS_ABSENT[@]} > 0 )); then
            PHASE_LINE="Phases 1-3 complete for the selected scope -- bootstrap + ${#COMPANIONS_DONE[@]} companions + validate; ${#COMPANIONS_ABSENT[@]} optional companion(s) not installed."
        else
            PHASE_LINE="Phases 1-3 complete -- bootstrap + ${#COMPANIONS_DONE[@]} companions + validate."
        fi
        # The address people open: the node's own domain, else its FQDN (A6 serves
        # *.<fqdn> over the VPN). A preserved hostname on an update is not one.
        _dom="${NODE_DOMAIN:-$NODE_FQDN_EFFECTIVE}"; [[ "$_dom" == *" "* ]] && _dom=""
        NEXT_LINE="Next: open Studio${_dom:+ (https://studio.$_dom/)} and sign in. Read the validation report:
  ssh $P_USER@$P_IP 'sudo cat /etc/smart-node/validation-report.md'"
        # A node with a setup token: first run is the welcome page, which creates the
        # owner and shows the password once. The command, not the link: the
        # token stays off this log.
        if [[ "$_WAS_FRESH" == "true" && -z "$OWNER_PASSWORD" && -n "$_dom" ]]; then
            _scheme=https; [[ "$_dom" == *.localhost ]] && _scheme=http
            # the first-run page opens on the way chosen here (an API key is entered there)
            _way=""; [[ "$AI_WAY" == key ]] && _way="\\&way=api"; [[ "$AI_WAY" == agent-runtimes ]] && _way="\\&way=claude"
            NEXT_LINE="Next: open Studio. This prints its first-run link ($_scheme://studio.$_dom/welcome/?token=...):
  ssh $P_USER@$P_IP 'echo $_scheme://studio.$_dom/welcome/?token=\$(sudo cat /var/lib/smart-node/panel/setup-token)$_way'"
            # the web installer opens the link itself; a file only it reads, not this log
            if [[ -n "$LINK_FILE" ]]; then
                _tok=$(ssh "${SSH_OPTS[@]}" "$P_USER@$P_IP" "sudo -n cat /var/lib/smart-node/panel/setup-token" 2>/dev/null || true)
                if [[ -n "$_tok" ]]; then
                    ( umask 077; printf '%s\n' "$_scheme://studio.$_dom/welcome/?token=$_tok${_way//\\/}" > "$LINK_FILE" )
                fi
                unset _tok
            fi
        fi
        # Report on the CHANNEL, not on Telegram. This block used to announce
        # "skipped, no Telegram credentials" whenever --tg-token was absent --
        # which now is printed on a perfectly good mail-only node that
        # just installed the companion, telling the operator the opposite of
        # what happened.
        if [[ -n "${COMPANIONS_SKIPPED[*]:-}" ]]; then
            NEXT_LINE="$NEXT_LINE
PARTIAL: the following companions were previously installed but NOT updated
on this run (their release fixes have not reached the node):
  SKIPPED: ${COMPANIONS_SKIPPED[*]}
Re-run with the missing channel credentials, or install from the B06 panel."
        elif [[ -z "$TG_TOKEN$MAIL_HOST" && -z "${COMM_RAN:-}" ]]; then
            NEXT_LINE="$NEXT_LINE
The communication companion was skipped -- no channel was given. Install it
from the B06 panel, or re-run with --mail-host (external mailserver) or
--tg-token + --tg-channel-id."
        elif [[ -n "$MAIL_HOST" ]]; then
            NEXT_LINE="$NEXT_LINE
Email channel wired against $MAIL_HOST. The node polls only mailboxes bound to
an agent: open the panel and bind the address, or the poller finds nothing."
        fi
    else
        PHASE_LINE="Phase 1 bootstrap complete -- the node has a control panel."
        NEXT_LINE="Next: re-run this installer with --all. The owner is created on Studio's
first-run page, whose link it prints."
    fi
    if [[ "$NO_VPN" -eq 1 ]]; then
        NODE_LINE="  Node FQDN:   $NODE_FQDN_EFFECTIVE  (A6 skipped -- --no-vpn; point your own DNS at $P_IP)"
    else
        NODE_LINE="  Node FQDN:   $NODE_FQDN_EFFECTIVE  (A6 vpn-server -- WireGuard + wildcard DNS)
  Phone QR:    /etc/wireguard/peers/phone1.png on the node (endpoint ${VPN_ENDPOINT:-$P_IP:51820})"
    fi
    _ok_label="[OK]"
    (( _HAS_SKIPPED )) && _ok_label="[PARTIAL]"
    cat <<TXTEOF

==========================================================================
$_ok_label Smart Node install complete
==========================================================================
  Provider:    $PROVIDER ($P_PROVIDER_ID)
  IP:          $P_IP
  SSH:         ssh -i $SSH_KEY_PRIV $P_USER@$P_IP
  Allow from:  $ALLOW_FROM
$NODE_LINE
  Node domain: ${NODE_DOMAIN:-(none -- self-signed cert)}
  Public domain: ${PUBLIC_DOMAIN_EFFECTIVE:-(none -- same as the node domain)}
  Mode:        $MODE
  Stages:      $STAGES_TXT (all done)
  Skipped:     ${COMPANIONS_SKIPPED[*]:-(none)}
  Not installed: ${COMPANIONS_ABSENT[*]:-(none)}

$PHASE_LINE

Inspect on target:
  ssh $P_USER@$P_IP 'sudo cat /etc/smart-node/install-report.md'
  ssh $P_USER@$P_IP 'sudo ls -la /var/lib/smart-node/stages/'

$NEXT_LINE
TXTEOF
fi

# Exit 9 when any previously-installed companion was skipped. This is
# distinct from 0 (full success), 5 (companion failed) and 6 (validate
# failed) so automated tooling can identify a partial update .
(( _HAS_SKIPPED )) && exit 9
exit 0
