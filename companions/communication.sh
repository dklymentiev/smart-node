#!/bin/bash
#
# communication.sh -- Smart Node installer, Layer C Phase 2 companion:
# communication.
#
# Runs INSIDE the target node as root. Idempotent. Depends on B02 (the
# messages registry) and the dispatcher companion (brin -- the brain the
# dispatch layer wakes).
#
# Installs the communication companion: the node's channel transport.
# Channel adapters move messages between a channel and the messages
# registry -- transport only, no reasoning. The dispatch layer
# dispatch.py is the realtime trigger that reads the
# registry, wakes brin, and sends the reply back.
#
# Component: the communication source tree, shipped as the sibling
# directory `communication/` (it is the source, not a vendored snapshot
# -- it has no consumer outside a Smart Node).
#
# Native systemd services, NOT containers: the telegram/email adapters
# and the dispatch layer are Python apps and peer-authenticate to the
# host-native B02 PostgreSQL on a local socket -- the native-service
# pattern the gateway / dispatcher companions already establish. The one
# container, so the companion manages it through a oneshot systemd unit
#
# Creates:
#   - service account smart-node-communication (+ owned dirs) via lib.sh
#     create_service_user, and a matching peer-auth PostgreSQL LOGIN role
#     with grants on the messages registry only
#   - /opt/smart-node/communication -- the install tree (component source
#     + a Python venv with the pinned requirements + a state/ dir for the
#     telegram poll offset)
#   - /opt/smart-node/communication/.env (0600, service-user owned) --
#     the runtime config: the core-DB DSN, the telegram bot credentials,
#     and the brin URL + API key (copied from the dispatcher companion)
#   - smart-node-communication-telegram.service -- the telegram adapter
#     long-poll daemon
#   - smart-node-communication-dispatch.service -- the dispatch-layer
#     daemon (registry -> brin -> reply)
#   - smart-node-communication-email.service + .timer -- the email
#     adapter poll, wired only when the mail companion is installed
#     oneshot unit driving docker compose; wired only when a Gemini key
#     is available
#     source + .env), separate from the communication venv tree
#   - companions.d/communication.json -- the manifest the B06 panel reads
#   - "Companion communication" section in the install report
#   - /var/lib/smart-node/stages/communication.state
#
# Database wiring: the adapters and the dispatch layer connect to the
# B02 core DB over the local socket, peer-authenticated through the
# shared `smartnode` ident map. The companion creates the PostgreSQL
# role, grants it the messages registry (SELECT/INSERT on messages,
# SELECT/INSERT/UPDATE on message_state -- nothing else), ensures the
# ident line, and reloads the cluster.
#
# brin wiring: the dispatch layer reaches brin on the loopback. The brin
# URL and API key come from the dispatcher companion's own .env -- the
# dispatcher generates the key generate-once, this installer copies it.
#
# Scope (v0.3.0): the telegram adapter + the dispatch layer; the email
# adapter wired on a systemd timer when the mail companion is installed
# wired as a container when a Gemini key is
# available. The web (WebSocket) adapter is a later subtask of
#
# Usage:
#   communication.sh             full run: probe -> intent -> probe -> mark
#   communication.sh --verify    idempotent post-check; exit 0 / N failures
#   communication.sh --status    read .../stages/communication.state
#   communication.sh --uninstall reverse the install
#   communication.sh --help      this message
#
# Environment:
#   Every channel is optional; the install refuses only when NONE can be
#   wired, since a transport with no channel carries nothing.
#
#   A RE-RUN KEEPS WHAT IT IS NOT TOLD ABOUT. .env is rewritten
#   whole every run, so a value this run does not supply used to be deleted
#   rather than left alone -- which made the supported way to add a second
#   channel destroy the first, and took CONTACT_GATE back to `off` on the way
#   past. Now: an input given on this run wins; otherwise what the node
#   already has is kept; otherwise the documented default. An input that is
#   set but empty counts as given, so clearing a value on purpose still
#   works. A channel already in .env is carried forward whole -- including
#   its mailbox accounts file, which is left exactly as installed.
#   Nothing here changes a first install: with no .env there is nothing to
#   carry, and only what this run supplies is wired.
#   SMART_NODE_TG_TOKEN       telegram bot token -- wires the telegram
#   SMART_NODE_TG_CHANNEL_ID  channel when BOTH are set (per-node bot)
#   SMART_NODE_DISPATCH_INTERVAL     dispatch scan seconds   (default 3)
#   SMART_NODE_EMAIL_POLL_INTERVAL   email poll seconds      (default 60)
#
#   A node hosts no mail. It reads and sends through one external service --
#   the host holding the valid PTR, certificate and DKIM -- and there is no
#   local topology any more: the mail companion was removed after
#   the copy it left on reference-node shadowed the real mailboxes for seven weeks.
#   SMART_NODE_MAIL_HOST        the mail service (name, not address)
#   SMART_NODE_MAIL_IMAP_HOST   override for reading   (default: MAIL_HOST)
#   SMART_NODE_MAIL_SMTP_HOST   override for sending   (default: MAIL_HOST)
#                               Pass the protocol names -- imap.<domain> and
#                               smtp.<domain> -- so the service can move
#                               without touching a node.
#   SMART_NODE_MAIL_IMAP_PORT   default 993
#   SMART_NODE_MAIL_SMTP_PORT   default 587
#   SMART_NODE_MAIL_TLS_VERIFY  default 1 -- the mail service presents a real
#                               certificate for the names above, and the node
#                               is the only place that would notice if it
#                               stopped doing so
#   SMART_NODE_MAIL_ALLOWED_DOMAINS    comma-separated domains this node may
#   SMART_NODE_MAIL_ALLOWED_ADDRESSES  email, and single addresses on top of
#                               them. The sender refuses every recipient
#                               outside these, so a mistaken or invented
#                               address cannot be written to. Neither set
#                               means nothing external is reachable.
#   SMART_NODE_MAIL_ACCOUNTS_FILE  file of PW_<addr> lines to install as this
#                               node's mailbox passwords (multi-mailbox: the
#                               poller reads sn_config.agent_channels and looks
#                               each address up here)
#   SMART_NODE_BUSINESS_NAME    the business this node writes as -- the From
#                               display name and the signature on outbound
#                               messages. Absent on a re-run, the value already
#                               in .env is kept rather than dropped.
#   SMART_NODE_MAIL_ACCOUNT     a single mailbox address, and
#   SMART_NODE_MAIL_ACCOUNT_PW  its password -- shorthand when the node starts
#                               with one box; the accounts file is generated
#   SMART_NODE_NOTIFY_EMAIL_SENDER  mailbox used for owner/system alerts.
#                               Must have an installed password. Defaults to the
#                               single mailbox, or an enabled agent email binding
#                               with credentials; never to an invented address.
#   With SMART_NODE_MAIL_HOST set, one of ACCOUNTS_FILE or ACCOUNT+ACCOUNT_PW
#   is required: a host with no credentials cannot open a mailbox.
#   SMART_NODE_GEMINI_KEY     Gemini Native Audio API key -- wires the
# (/etc/smart-node/node.env), not an env var.
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

STAGE_ID="communication"
STAGE_NAME="communication"
STAGE_VERSION="0.3.12"

# Where this script lives -- the communication source tree sits beside
# it. install-on.sh / the operator ship the two together onto the node.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/communication"

# Install tree, namespaced like every Smart Node artifact.
SERVICE_SHORT="communication"
SERVICE_USER="smart-node-communication"
INSTALL_DIR="/opt/smart-node/communication"
VENV_DIR="$INSTALL_DIR/venv"
ENV_FILE="$INSTALL_DIR/.env"
LOG_DIR="/var/log/smart-node/communication"
# The telegram adapter persists its getUpdates offset here.
COMP_STATE_DIR="$INSTALL_DIR/state"

# The agent-facing CLI. It is NOT part of the service tree: agents call it
# directly, so it lives under /srv/tools where core-api already tells agents
# to look (lib/agent_fs.py composes instructions citing this exact path).
# Shipped as tools/comm.sh beside the component source; excluded from the
# service tree sync so exactly one copy exists on the node.
CLI_SRC="$SRC_DIR/tools/comm.sh"
CLI_DIR="/srv/tools/comm"
CLI_PATH="$CLI_DIR/comm.sh"
# The agent contract for that CLI. Registering it as a tool is what makes it
# reach the agent at all: a registered tool composes into the agent's own
# instructions. Shipping the file without this row leaves agents unaware the
# tool exists -- the same failure previously fixed for planner and node-admin,
# where an agent without a contract hand-rolled curl and stayed
# unrunnable.
CLI_USAGE_MD="$SCRIPT_DIR/comm-tool-usage.md"

# Native systemd units. The telegram + dispatch daemons are always
# installed; the email channel is a timer-driven oneshot, wired only when
# the operator names a mail service (see external_mail_requested).
UNIT_TELEGRAM="smart-node-communication-telegram.service"
UNIT_DISPATCH="smart-node-communication-dispatch.service"
UNIT_TELEGRAM_PATH="/etc/systemd/system/$UNIT_TELEGRAM"
UNIT_DISPATCH_PATH="/etc/systemd/system/$UNIT_DISPATCH"
UNIT_EMAIL="smart-node-communication-email.service"
UNIT_EMAIL_TIMER="smart-node-communication-email.timer"
UNIT_EMAIL_PATH="/etc/systemd/system/$UNIT_EMAIL"
UNIT_EMAIL_TIMER_PATH="/etc/systemd/system/$UNIT_EMAIL_TIMER"
# The outbound sender: the node's ONE exit. Unconditional -- it drains
# every queued outbound row whatever the channel, including the rows core-api
# writes for /api/messages/send and /reply. Wiring it to the email channel would
# leave a telegram-only node unable to answer at all, which is the state every
# shipped node was in: the code calls this timer the sole transmitter, the
# installer never installed it, and replies were composed, recorded and never
# sent, with nothing raising an error.
UNIT_SEND="smart-node-communication-send-outbound.service"
UNIT_SEND_TIMER="smart-node-communication-send-outbound.timer"
UNIT_SEND_PATH="/etc/systemd/system/$UNIT_SEND"
UNIT_SEND_TIMER_PATH="/etc/systemd/system/$UNIT_SEND_TIMER"

# The dispatcher companion's install tree -- its .env carries the
# generate-once BRIN_API_KEY and the brin loopback PORT.
DISPATCHER_ENV="/opt/smart-node/dispatcher/.env"

# Per-agent mailbox passwords on the mail service, installed from what the
# operator supplies (generate-once, keyed PW_<addr>). The multi-mailbox poller
# reads it to poll each agent box, and the sender to authenticate as it.
MAIL_AGENT_ACCOUNTS="/opt/smart-node/mail/agent-accounts.env"

# Resolved at run time: each channel is wired only when its inputs exist.
TG_WIRED=""
# Resolved at run time, only when the email channel is wired.
EMAIL_WIRED=""
EMAIL_TOPOLOGY=""       # local (mail companion) | external (a host we do not run)
EMAIL_IMAP_PORT=993
EMAIL_SMTP_PORT=587
EMAIL_TLS_VERIFY=1
EMAIL_ACCOUNTS_SRC=""   # operator-supplied PW_<addr> file, when given
# Set when the email channel came from the node's own .env rather than from
# this run's inputs. The distinction is not cosmetic: an adopted
# channel already has its accounts file on disk, and rewriting that file from
# an empty NODE_MAIL_ACCT would replace real mailbox passwords with one blank
# key -- silently, since the poller skips a box it finds no entry for.
EMAIL_ADOPTED=""
EMAIL_HOST=""        # the mail service (fallback for both protocols)
EMAIL_IMAP_HOST=""   # what the poller dials
EMAIL_SMTP_HOST=""   # what the sender dials
NODE_MAIL_ACCT=""    # node@<domain> -- the brain's mailbox
EMAIL_NODE_PW=""     # the node account password (from the mail companion)

# here, never guessed.
NODE_ENV_FILE="/etc/smart-node/node.env"

# The fast-channel contract this companion PUBLISHES for core-api.
# Written by publish_fast_channels(); read by the core-api unit through
# EnvironmentFile=-, so it may be absent on a node where no channel is wired.
FAST_CHANNELS_ENV="/etc/smart-node/fast-channels.env"

# Owner budget-alert contract consumed by core-api and gateway.  This
# file is non-secret: it contains an actor, channel and real mailbox address;
# the password remains only in MAIL_AGENT_ACCOUNTS.
BUDGET_NOTIFICATIONS_ENV="/etc/smart-node/budget-notifications.env"

# lib.sh is installed by B01-core; the companion has no fallback.
LIB="/usr/local/lib/smart-node/lib.sh"
if [[ -r "$LIB" ]]; then
    # shellcheck source=/dev/null
    source "$LIB"
else
    echo "[ERROR] lib.sh not installed ($LIB) -- run B01-core first" >&2
    exit 1
fi

# The integration/tool registration helper, exactly as documents and memory
# source it. Without this block sn_integration_register is never defined, and
# register_comm_tool below logs one WARN and returns 0 -- so the comm tool, and
# with it the written contract in comm-tool-usage.md, never reached a node at
# all. The file being pushed beside the script was necessary and not sufficient:
# nothing here read it.
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
    --agent-cli)      MODE="agent-cli" ;;
    *)                die "unknown arg: $1 (use --help)" ;;
esac

# ---------- helpers ----------

PYTHON_BIN="python3"

# B02 persistence contract -- resolved from persistence.env at run time.
PG_CLUSTER=""   # e.g. 16/smartnode
PG_CORE_DB=""   # smartnode
PG_PORT=""      # cluster port
PG_IDENT=""     # pg_ident.conf path
PG_UNIT=""      # postgresql@16-smartnode

# load_persistence_contract -- read the B02 connection contract. Every
# value the companion needs to reach and configure the core DB comes
# from here; an incomplete contract means B02 has not run.
load_persistence_contract() {
    local env="${SMART_NODE_PERSISTENCE_ENV:-/etc/smart-node/persistence.env}"
    [[ -r "$env" ]] \
        || die "persistence contract missing ($env) -- run B02 first"
    PG_CLUSTER=$(_envfile_get "$env" SMART_NODE_PG_CLUSTER)
    PG_CORE_DB=$(_envfile_get "$env" SMART_NODE_PG_CORE_DB)
    PG_PORT=$(_envfile_get   "$env" SMART_NODE_PG_PORT)
    PG_IDENT=$(_envfile_get  "$env" SMART_NODE_PG_IDENT)
    [[ -n "$PG_CLUSTER" && -n "$PG_CORE_DB" && -n "$PG_PORT" && -n "$PG_IDENT" ]] \
        || die "incomplete persistence contract ($env) -- run B02 first"
    # cluster "16/smartnode" -> systemd unit "postgresql@16-smartnode".
    PG_UNIT="postgresql@${PG_CLUSTER/\//-}"
}

# psql_core -- run psql against the core DB as the postgres OS user
# (cluster superuser via peer auth). ON_ERROR_STOP so a failed statement
# is a non-zero exit, not a silent partial.
psql_core() {
    runuser -u postgres -- psql --cluster "$PG_CLUSTER" \
        -d "$PG_CORE_DB" -v ON_ERROR_STOP=1 -X "$@"
}

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

# tg_wired_check -- true when the telegram channel can be wired, i.e. both
# bot credentials are present.
#
# Telegram used to be mandatory: without a bot token the whole companion
# refused to install. That made a mail-only node impossible -- the operator
# had to mint a BotFather bot for a channel they did not want, purely to get
# the email poller. It also blocked the topology that actually runs in
# production, where mail lives on the host DMS.
#
# Channels are uniformly optional: each is
# wired when its inputs are present. require_a_channel() enforces the one
# real constraint -- a transport with no channel at all is pointless.
tg_wired_check() {
    [[ -n "${SMART_NODE_TG_TOKEN:-}" && -n "${SMART_NODE_TG_CHANNEL_ID:-}" ]]
}

# require_a_channel -- refuse an install that would transport nothing.
# Called after every channel has been resolved, so the message can name
# exactly what is missing instead of demanding one specific channel.
require_a_channel() {
    [[ -n "$TG_WIRED" || -n "$EMAIL_WIRED" ]] && return 0
    die "no channel can be wired -- give the node at least one:
  telegram: SMART_NODE_TG_TOKEN + SMART_NODE_TG_CHANNEL_ID (a per-node bot from BotFather)
  email:    SMART_NODE_MAIL_HOST + credentials (an external mailserver -- what both
            real nodes use), or install the mail companion for a node-local one
Installing the transport with no channel would leave the node unable to send or receive anything."
}

# external_mail_requested -- the operator named the mail service this node
# reads and sends through. It is the only way the email channel is wired.
external_mail_requested() {
    [[ -n "${SMART_NODE_MAIL_HOST:-}" ]]
}

# ---------------------------------------------------------------------------
# Carrying an installed node's own configuration forward.
#
# .env is rewritten WHOLESALE on every run. Before this block, a key the run
# was not given was not left alone -- it was deleted. So the supported way to
# add a second channel destroyed the first: re-running with SMART_NODE_TG_TOKEN
# stripped IMAP_HOST, SMTP_HOST, the egress allowlist and AGENT_ACCOUNTS_FILE,
# the node kept running, mail simply stopped being read, and nothing said so.
# The same edge takes CONTACT_GATE back to `off` and empties the allowlist --
# a re-run silently un-does a security decision the operator made once.
#
# The rule here: an operator input given on THIS run wins; otherwise whatever
# the node already has is kept; otherwise the documented default. Being set
# but empty counts as given, so clearing a value on purpose still works.
#
# This is the general form of the SN_BUSINESS_NAME carry-forward previously used for one key -- and the failure appeared from a RE-install, not an install,
# which is exactly this failure seen from outside.

# _carry_forward VAR ENV_KEY [DEFAULT] -- echo the value write_env should use.
_carry_forward() {
    local var="$1" key="$2" def="${3:-}" prev=""
    # Set-but-empty is a deliberate value, so test for existence, not content.
    if [[ -n "${!var+x}" ]]; then printf '%s' "${!var}"; return 0; fi
    prev=$(_envfile_get "$ENV_FILE" "$key")
    if [[ -n "$prev" ]]; then printf '%s' "$prev"; return 0; fi
    printf '%s' "$def"
}

# adopt_telegram_from_env -- true when this node already has a wired telegram
# channel. Feeds the same SMART_NODE_* variables the operator would have
# supplied, so everything downstream (units, FAST_CHANNELS, the registry row)
# sees a wired channel and needs no notion of where it came from.
adopt_telegram_from_env() {
    local tok chan
    tok=$(_envfile_get "$ENV_FILE" TG_TOKEN)
    chan=$(_envfile_get "$ENV_FILE" TG_CHANNEL_ID)
    [[ -n "$tok" && -n "$chan" ]] || return 1
    SMART_NODE_TG_TOKEN="$tok"
    SMART_NODE_TG_CHANNEL_ID="$chan"
    return 0
}

# adopt_email_from_env -- the same for the email channel. IMAP_HOST is the
# marker: the poller cannot run without it, so its presence is what makes a
# node's email channel real. Credentials are NOT re-derived -- the accounts
# file already on disk stays untouched (see EMAIL_ADOPTED).
adopt_email_from_env() {
    local imap
    imap=$(_envfile_get "$ENV_FILE" IMAP_HOST)
    [[ -n "$imap" ]] || return 1
    EMAIL_IMAP_HOST="$imap"
    EMAIL_HOST="$imap"
    EMAIL_SMTP_HOST=$(_envfile_get "$ENV_FILE" SMTP_HOST); [[ -n "$EMAIL_SMTP_HOST" ]] || EMAIL_SMTP_HOST="$imap"
    EMAIL_IMAP_PORT=$(_envfile_get "$ENV_FILE" IMAP_PORT); [[ -n "$EMAIL_IMAP_PORT" ]] || EMAIL_IMAP_PORT=993
    EMAIL_SMTP_PORT=$(_envfile_get "$ENV_FILE" SMTP_PORT); [[ -n "$EMAIL_SMTP_PORT" ]] || EMAIL_SMTP_PORT=587
    EMAIL_TLS_VERIFY=$(_envfile_get "$ENV_FILE" EMAIL_TLS_VERIFY); [[ -n "$EMAIL_TLS_VERIFY" ]] || EMAIL_TLS_VERIFY=1
    # The single-mailbox fallback identity, when this node uses one.
    NODE_MAIL_ACCT=$(_envfile_get "$ENV_FILE" IMAP_USER)
    EMAIL_NODE_PW=$(_envfile_get "$ENV_FILE" IMAP_PASSWORD)
    EMAIL_TOPOLOGY="external"
    EMAIL_ADOPTED=1
    return 0
}

# adopt_email_from_secret_store -- read mailbox credentials from the node's
# secret store. Called when external_mail_requested
# and adopt_email_from_env both fail: no --mail-host flag, no existing .env.
#
# The store holds one ref per mailbox account (49 observed refs):
#   mailbox.<addr_underscored>  -- value is the account password
#
# The address is encoded as addr_underscored: the FIRST underscore separates
# local part from domain; remaining underscores are dots in the domain. E.g.:
#   mailbox.alex_example_com  ->  address alex@example.com, password = value
#   mailbox.admin_example_ink  ->  address admin@example.ink, password = value
#
# The mail host is the domain part of the address. An optional two-segment ref
#   mailbox.<addr_underscored>.host  (value = hostname override)
# is checked first; if absent the domain part of the decoded address is used.
#
# Returns 0 and sets EMAIL_* when at least one mailbox ref is found;
# returns 1 and leaves EMAIL_* untouched when the store has nothing useful.
# Never fatal -- a missing or unreachable store falls back to the normal path.
adopt_email_from_secret_store() {
    # Network location is not identity: every agent OS process can reach
    # loopback. This installer runs as root, so use the unix socket and let
    # SO_PEERCRED prove the operator instead.
    local secrets_sock="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
    [[ -S "$secrets_sock" ]] || return 1
    local secrets_base="http://localhost"
    local refs_json
    refs_json=$(curl -sS -m 10 --unix-socket "$secrets_sock" \
        "${secrets_base}/api/secrets/refs" 2>/dev/null) || return 1
    # Find the first mailbox.<addr_underscored> ref (exactly two dot-segments).
    # /api/secrets/refs returns {"refs":[...],"count":N} -- read the "refs" key.
    local addr_ref addr_underscored
    addr_ref=$(python3 -c '
import sys, json
try:
    data = json.loads(sys.stdin.read())
    for r in data.get("refs", []):
        parts = r.split(".")
        if len(parts) == 2 and parts[0] == "mailbox":
            print(r); break
except Exception: pass
' 2>/dev/null <<<"$refs_json")
    [[ -n "$addr_ref" ]] || return 1
    addr_underscored="${addr_ref#mailbox.}"
    # Resolve password (the value of the two-segment ref).
    local acct_pw
    acct_pw=$(curl -sS -m 10 --unix-socket "$secrets_sock" \
        "${secrets_base}/api/secrets/resolve?ref=${addr_ref}" 2>/dev/null \
        | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("value",""))
except: pass' 2>/dev/null)
    # Decode address: split on FIRST underscore -> local@domain (dots from rest).
    # alex_example_com -> local=alex, domain=example.com -> alex@example.com
    local acct_addr
    acct_addr=$(python3 -c "
s='${addr_underscored}'
sep=s.find('_')
if sep>=0:
    print(s[:sep]+'@'+s[sep+1:].replace('_','.'))
else:
    print(s)
" 2>/dev/null)
    # Mail host: optional override ref, else the domain part of the address.
    local host
    host=$(curl -sS -m 10 --unix-socket "$secrets_sock" \
        "${secrets_base}/api/secrets/resolve?ref=${addr_ref}.host" 2>/dev/null \
        | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("value",""))
except: pass' 2>/dev/null)
    [[ -n "$host" ]] || host="${acct_addr#*@}"
    [[ -n "$host" ]] || return 1
    EMAIL_HOST="$host"
    EMAIL_IMAP_HOST="$host"
    EMAIL_SMTP_HOST="$host"
    EMAIL_IMAP_PORT="${SMART_NODE_MAIL_IMAP_PORT:-993}"
    EMAIL_SMTP_PORT="${SMART_NODE_MAIL_SMTP_PORT:-587}"
    EMAIL_TLS_VERIFY="${SMART_NODE_MAIL_TLS_VERIFY:-1}"
    EMAIL_TOPOLOGY="external"
    NODE_MAIL_ACCT="$acct_addr"
    EMAIL_NODE_PW="$acct_pw"
    return 0
}

# resolve_email_external -- settle the email channel against a mailserver the
# node does NOT host. This is the topology both real nodes run: the MX target
# is the host holding the valid PTR, certificate and DKIM, and the node reads
# its mailboxes from there. Canon supported only the local companion until
# an uninstalled sender, so every such node was assembled by hand.
#
# The multi-mailbox poller needs no per-node account: it iterates
# sn_config.agent_channels and looks each address up in the accounts file, so
# credentials are the only thing that must come from the operator.
resolve_email_external() {
    EMAIL_HOST="$SMART_NODE_MAIL_HOST"
    # Per-protocol names, so the service can be moved with one DNS change and
    # no node edits. They fall back to the single host for a deployment that
    # has only one name.
    EMAIL_IMAP_HOST="${SMART_NODE_MAIL_IMAP_HOST:-$SMART_NODE_MAIL_HOST}"
    EMAIL_SMTP_HOST="${SMART_NODE_MAIL_SMTP_HOST:-$SMART_NODE_MAIL_HOST}"
    EMAIL_IMAP_PORT="${SMART_NODE_MAIL_IMAP_PORT:-993}"
    EMAIL_SMTP_PORT="${SMART_NODE_MAIL_SMTP_PORT:-587}"
    # Verification ON. It used to default off, on the reasoning that the node
    # dialled a private address whose certificate names the public host. The
    # node dials the public names now, the certificate is real, and a node is
    # the only place that would notice if it stopped being.
    EMAIL_TLS_VERIFY="${SMART_NODE_MAIL_TLS_VERIFY:-1}"
    EMAIL_TOPOLOGY="external"

    local src="${SMART_NODE_MAIL_ACCOUNTS_FILE:-}"
    local acct="${SMART_NODE_MAIL_ACCOUNT:-}"
    local acct_pw="${SMART_NODE_MAIL_ACCOUNT_PW:-}"

    if [[ -n "$src" ]]; then
        [[ -r "$src" ]] \
            || die "SMART_NODE_MAIL_ACCOUNTS_FILE=$src is not readable"
        # The hyphen is not decoration: key derivation replaces ONLY @
        # and . with _, so a hyphenated domain keeps its hyphen -- an address at
        # example-co.com derives PW_hello_example-co_com. A validator without it
        # rejects a file this installer wrote itself, and the install dies
        # claiming the file holds no keys. The validator is the side that was
        # wrong: nothing sources this file as shell (only the Python parsers
        # read it), and changing derivation instead would orphan every key
        # already written on an installed node.
        grep -qE '^[[:space:]]*PW_[A-Za-z0-9_-]+=' "$src" \
            || die "SMART_NODE_MAIL_ACCOUNTS_FILE=$src holds no PW_<addr> lines -- keys are the address with @ and . replaced by _ (e.g. PW_hello_example_com)"
        EMAIL_ACCOUNTS_SRC="$src"
    elif [[ -n "$acct" && -n "$acct_pw" ]]; then
        # Shorthand: one mailbox. Generate the same file shape the poller
        # expects, so growing to several boxes later is adding lines, not
        # changing mechanism.
        NODE_MAIL_ACCT="$acct"
        EMAIL_NODE_PW="$acct_pw"
        EMAIL_ACCOUNTS_SRC=""
    else
        die "SMART_NODE_MAIL_HOST is set but no mailbox credentials were given.
  Provide SMART_NODE_MAIL_ACCOUNTS_FILE (PW_<addr> lines), or
  SMART_NODE_MAIL_ACCOUNT + SMART_NODE_MAIL_ACCOUNT_PW for a single mailbox."
    fi
}

# write_accounts_file -- install this node's mailbox passwords where the
# poller looks for them. Root-owned, group-readable by the service user only.
write_accounts_file() {
    [[ "$EMAIL_TOPOLOGY" == external ]] || return 0
    # An adopted channel brought no credentials with it -- the file on
    # disk IS the credentials. Writing here would derive one key from an empty
    # address and replace every real mailbox password with it, and the poller
    # reports nothing: a box with no entry is skipped, not an error.
    if [[ -n "$EMAIL_ADOPTED" ]]; then
        log "mailbox accounts file left as installed ($MAIL_AGENT_ACCOUNTS) -- channel carried forward, no new credentials given"
        return 0
    fi
    install -d -m 2755 -o root -g "$SMART_NODE_GROUP" "$(dirname "$MAIL_AGENT_ACCOUNTS")"
    local tmp; tmp=$(mktemp)
    if [[ -n "$EMAIL_ACCOUNTS_SRC" ]]; then
        cat "$EMAIL_ACCOUNTS_SRC" > "$tmp"
    else
        # PW_ key derivation must match poll_mailboxes._cred_key():
        # the address with @ and . replaced by _.
        local key="PW_${NODE_MAIL_ACCT//@/_}"; key="${key//./_}"
        {
            echo "# Smart Node -- mailbox passwords for this node's boxes."
            echo "# Keys are the address with @ and . replaced by _."
            echo "$key=$EMAIL_NODE_PW"
        } > "$tmp"
    fi
    install -m 0640 -o root -g "$SMART_NODE_GROUP" "$tmp" "$MAIL_AGENT_ACCOUNTS"
    rm -f "$tmp"
    manifest_record file "$MAIL_AGENT_ACCOUNTS"
    log "wrote mailbox passwords -> $MAIL_AGENT_ACCOUNTS"
}

# The local topology is gone. resolve_email_target() used to read the mail
# companion's container IP and node@ password, and mail_companion_present()
# used to gate it. Both were removed with the companion itself: a node
# hosts no mail, and the copy that companion left on reference-node held shadows of the
# real mailboxes, its own passwords and an unpublished DKIM key.

# ---------- modes ----------

# probe_before -- a re-run (our units present) proceeds idempotently: the
# tree is re-synced, the venv reused, the units rewritten. .env is
# preserved so a manually-corrected credential is not clobbered.
probe_before() {
    if systemctl list-unit-files "$UNIT_DISPATCH" >/dev/null 2>&1 \
       && systemctl cat "$UNIT_DISPATCH" >/dev/null 2>&1; then
        log "communication services already present -- idempotent re-run"
    elif [[ -d "$INSTALL_DIR" ]]; then
        log "WARN: $INSTALL_DIR exists but the service units do not -- treating as a resumed run"
    fi
}

# install_tree -- copy the communication source into the install tree.
# --delete-style refresh: the tree mirrors the source, but venv/, .env
# and state/ (runtime, not source content) are kept.
install_tree() {
    [[ -d "$SRC_DIR" ]] \
        || die "communication source missing ($SRC_DIR) -- the companion ships it as a sibling dir"
    [[ -f "$SRC_DIR/dispatch.py" && -f "$SRC_DIR/requirements.txt" ]] \
        || die "communication source incomplete ($SRC_DIR) -- no dispatch.py / requirements.txt"
    [[ -f "$SRC_DIR/adapters/telegram/adapter.py" ]] \
        || die "communication source incomplete ($SRC_DIR) -- telegram adapter absent"
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$INSTALL_DIR"
    manifest_record dir "$INSTALL_DIR"
    log "syncing communication source -> $INSTALL_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude 'venv/' --exclude '.env' --exclude 'state/' \
            --exclude 'tools/' \
            --exclude '__pycache__/' --exclude '*.pyc' \
            "$SRC_DIR"/ "$INSTALL_DIR"/
    else
        # rsync-free fallback: wipe everything but the kept runtime
        # state, recopy.
        find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
            ! -name venv ! -name .env ! -name state \
            -exec rm -rf {} + 2>/dev/null || true
        cp -a "$SRC_DIR"/. "$INSTALL_DIR"/
        find "$INSTALL_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
    fi
    # The agent CLI is deployed to /srv/tools, never carried inside the
    # service tree -- one copy, at the path core-api points agents to.
    rm -rf "$INSTALL_DIR/tools" 2>/dev/null || true
    # Legacy: older installs left an adapters/voice tree here. The voice
    # companion owns voice now (its own repo, its own install script); this
    # only clears the leftover inside OUR install dir.
    # venv module -- never carry it inside the communication tree.
    rm -rf "$INSTALL_DIR/adapters/voice" 2>/dev/null || true
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$COMP_STATE_DIR"
    # inbound attachment byte store. The communication adapter
    # writes here; the retrieval API (core-api) reads here. 0750 so only the
    # service user and the smart-node-agents group can enter. The path is
    # exported to .env as ATTACHMENT_DIR so the adapter and the core-api
    # companion read it from config rather than hardcoding it.
    install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" /srv/data/attachments
    # Commit hash marker for sn-version (vendored snapshot has no .git).
    echo "${SN_BUILD_COMMIT:-unknown}" > "$INSTALL_DIR/.sn-commit"
    chown -R "$SERVICE_USER:$SMART_NODE_GROUP" "$INSTALL_DIR"
}

# install_agent_cli -- deploy the agent-facing communication CLI.
#
# Agents run as their own users and call this script directly, so it is
# owned by root and only readable/executable by them -- an agent must not be
# able to rewrite the tool it is trusted to use. The path is not a choice:
# core-api composes agent instructions that cite /srv/tools/comm/comm.sh
# (companions/core-api/lib/agent_fs.py). Before this, a node shipped that
# instruction with no such file -- the agent was pointed at nothing.
install_agent_cli() {
    [[ -f "$CLI_SRC" ]] \
        || die "agent CLI missing ($CLI_SRC) -- the companion ships it as tools/comm.sh"
    bash -n "$CLI_SRC" \
        || die "agent CLI failed syntax check ($CLI_SRC)"
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" /srv/tools
    install -d -m 0755 -o root -g "$SMART_NODE_GROUP" "$CLI_DIR"
    install -m 0755 -o root -g "$SMART_NODE_GROUP" "$CLI_SRC" "$CLI_PATH"
    manifest_record dir  "$CLI_DIR"
    manifest_record file "$CLI_PATH"
    log "agent CLI installed -> $CLI_PATH"
}

# register_comm_tool -- publish the CLI in the node tool registry WITH its
# contract, so it composes into the instructions of every agent granted it.
#
# The file on disk is not enough: an agent learns a tool exists from the
# registry, not from the filesystem. Registering without usage_md is equally
# useless -- that is how reference-node ended up with a description-less `email` row
# that never reproduced on any other node.
register_comm_tool() {
    declare -F sn_integration_register >/dev/null || {
        log "WARN: sn_integration_register unavailable -- comm tool not registered"
        return 0
    }
    [[ -f "$CLI_USAGE_MD" ]] \
        || die "comm tool contract missing ($CLI_USAGE_MD) -- the companion ships it beside this script"
    local tools_tmp; tools_tmp=$(mktemp)
    if command -v jq >/dev/null 2>&1; then
        jq -n --rawfile usage "$CLI_USAGE_MD" \
            '[{name:"comm",kind:"cli",title:"Channel messages",description:"Read the messages addressed to you on any written channel, and answer them in-thread.",usage_md:$usage}]' \
            > "$tools_tmp"
    else
        # jq-free fallback: the row still lands, minus the contract body.
        printf '[{"name":"comm","kind":"cli","title":"Channel messages","description":"Read the messages addressed to you on any written channel, and answer them in-thread."}]' \
            > "$tools_tmp"
        log "WARN: jq absent -- comm tool registered without its usage contract"
    fi
    sn_integration_register \
        --slug           comm \
        --label          "Channel messages" \
        --purpose        "Communication" \
        `# core-api accepts system | app | external | mcp-only. "cli" was` \
        `# rejected with HTTP 422, which took the tools payload down with it,` \
        `# and the line below announced success anyway.` \
        --kind           app \
        --status         available \
        --install-path   "$CLI_PATH" \
        --description    "The node's written-channel inbox and outbox." \
        --companion-slug "$STAGE_ID" \
        --tools-json     "$tools_tmp" || true
    rm -f "$tools_tmp"
    # Report what happened, not that something was attempted. The previous
    # unconditional line said the contract had been registered while the row
    # did not exist, which is the failure mode this whole companion keeps
    # producing: a check that passes whether or not the thing worked.
    if tool_registered_check; then
        log "comm tool registered with its agent contract"
    else
        log "WARN: comm tool NOT registered -- agents will not be told this tool exists"
    fi
}

# tool_registered_check -- ask the registry, do not assume the POST worked.
tool_registered_check() {
    # `|| true`: this is a boolean check, and a database that is down or a
    # cluster name that does not resolve must make it answer "no", not end the
    # whole companion. Without it psql's non-zero status reaches the assignment
    # and `set -euo pipefail` exits here, naming no failing check --
    # the shape that hid the mail delivery failure.
    local n=""
    n=$(psql_core -tAc \
            "SELECT count(*) FROM sn_core.tools WHERE name='comm' AND coalesce(usage_md,'') <> ''" \
            2>/dev/null | tr -d ' ' || true)
    [[ "$n" == "1" ]]
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

# setup_db_role -- the peer-auth PostgreSQL role and its grants. The role
# name equals the OS service user; create_service_user already queued /
# wrote the `smartnode` ident line. The role gets the messages registry
# and nothing else: SELECT/INSERT on the immutable messages table,
# SELECT/INSERT/UPDATE on message_state (INSERT covers the AFTER INSERT
# seed trigger, which runs with the inserting role's privileges).
setup_db_role() {
    log "ensuring PostgreSQL role $SERVICE_USER + messages-registry grants"
    psql_core <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$SERVICE_USER') THEN
        CREATE ROLE "$SERVICE_USER" WITH LOGIN;
    END IF;
END
\$\$;
GRANT CONNECT ON DATABASE "$PG_CORE_DB" TO "$SERVICE_USER";
GRANT USAGE ON SCHEMA public TO "$SERVICE_USER";
GRANT SELECT, INSERT ON TABLE messages TO "$SERVICE_USER";
GRANT SELECT, INSERT, UPDATE ON TABLE message_state TO "$SERVICE_USER";
-- SCHEMA-QUALIFIED grants. The four lines above are unqualified, and psql runs
-- them with the default search_path ("\$user", public) -- so on a node that has
-- BOTH public.messages and sn_core.messages they granted on the PUBLIC copy,
-- while the runtime reads sn_core (its DSN sets search_path=sn_core,sn_config,
-- public). The service therefore had rights on a table it never touches and
-- none on the ones it does. Kept above for older nodes; the truth is here.
GRANT USAGE ON SCHEMA sn_core TO "$SERVICE_USER";
GRANT USAGE ON SCHEMA sn_config TO "$SERVICE_USER";
GRANT SELECT, INSERT ON TABLE sn_core.messages TO "$SERVICE_USER";
GRANT SELECT, INSERT, UPDATE ON TABLE sn_core.message_state TO "$SERVICE_USER";
-- the adapters stamp one identity verdict per inbound message (who it is
-- from and how strongly we know). INSERT only; the row is written once, at
-- arrival, and never revised.
GRANT SELECT, INSERT ON TABLE sn_core.message_identity TO "$SERVICE_USER";
-- The agent lock. EXECUTE only: the functions are SECURITY DEFINER
-- precisely so this role never needs UPDATE on sn_core.agent_runtime, which
-- would hand it everything else on that table along with the lock.
GRANT EXECUTE ON FUNCTION sn_core.claim_agent(text, text, integer) TO "$SERVICE_USER";
GRANT EXECUTE ON FUNCTION sn_core.release_agent(text) TO "$SERVICE_USER";
-- INSERT needs the id sequences behind those tables.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA sn_core TO "$SERVICE_USER";
-- The multi-mailbox poller resolves which addresses this node owns, and to
-- which agent each belongs, from the channel registry. Read-only: the
-- binding is made in the panel, never by the transport.
GRANT SELECT ON TABLE sn_config.agent_channels TO "$SERVICE_USER";
GRANT SELECT ON TABLE sn_config.channels TO "$SERVICE_USER";
-- The communication gate reads the contact grants at the transmit
-- point: may THIS agent write to THIS recipient on THIS channel. Read-only --
-- grants are made through core-api, never by the transport. Without these three
-- the sender fails every row with
--   InsufficientPrivilege: permission denied for table agent_contacts
-- which looks like a transport fault and is a missing GRANT.
GRANT SELECT ON TABLE sn_config.contacts TO "$SERVICE_USER";
GRANT SELECT ON TABLE sn_config.contact_channels TO "$SERVICE_USER";
GRANT SELECT ON TABLE sn_config.agent_contacts TO "$SERVICE_USER";
-- ...and which AGENT that binding names, plus the owner's own addresses. The
-- dispatch layer from 1.2.0 resolves the recipient before it wakes anyone, so
-- without these two it fails every message with
--   InsufficientPrivilege: permission denied for table agents
-- and the node registers mail it can never answer. Granted by hand on one node
-- during a manual repair; the installer never learned it, so every node built since would
-- have hit the same wall the moment the transport was updated.
GRANT SELECT ON TABLE sn_config.agents TO "$SERVICE_USER";
GRANT SELECT ON TABLE sn_config.user_channels TO "$SERVICE_USER";
-- The audit journal. The adapters emit message.inbound on every ingest, into a
-- table this role could not write: has_table_privilege(...,'sn_core.events',
-- 'INSERT') was false, and the emit is wrapped in \`except: pass\`, so it failed
-- silently on every message ever received. A node processed mail for weeks with
-- an empty journal while its own architecture document told agents to read that
-- journal to find out what happened. reference-node never showed it because its ident map
-- sends this service to the OWNER role, which can write anything.
-- events.actor is an FK onto sn_core.actors and the emitter self-registers the
-- component actor first, so INSERT on actors is part of the same permission or
-- the whole emit aborts on its first statement.
-- SELECT as well as INSERT, and not for reading: the actor self-registration
-- uses ON CONFLICT (id) DO NOTHING, and ON CONFLICT must read the conflicting
-- row. With INSERT alone Postgres refuses with "permission denied for table
-- actors" while has_table_privilege(...,'INSERT') answers true -- a
-- contradiction that costs an hour if you trust the privilege check.
GRANT INSERT, SELECT ON TABLE sn_core.events TO "$SERVICE_USER";
GRANT INSERT, SELECT ON TABLE sn_core.actors TO "$SERVICE_USER";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA sn_core TO "$SERVICE_USER";
-- attachment metadata table. The communication adapter
-- writes one row per stored attachment (INSERT); the retrieval API
-- (core-api) reads them with its own role. SELECT here is for the
-- ON CONFLICT DO NOTHING dedup path on re-ingest.
GRANT SELECT, INSERT ON TABLE sn_core.message_attachments TO "$SERVICE_USER";
SQL
    enable_wired_channels
}

# enable_wired_channels -- mark the channels this install actually wired as
# enabled in the node's channel registry.
#
# The registry ships every channel row disabled, and the poller filters on it
# (JOIN sn_config.channels ... AND c.enabled). So a node could have the
# transport installed, the units running, the mailbox bound to an agent -- and
# still poll ZERO mailboxes, silently and with exit 0, because the registry row
# nobody had touched said the channel was off. The installer wired the
# transport but never told the registry.
#
# Only channels this run wired are enabled; nothing is ever disabled here, so
# an operator's choice in the panel is not overwritten by a re-run.
enable_wired_channels() {
    local slug
    for slug in $( [[ -n "$EMAIL_WIRED" ]] && echo email
                   [[ -n "$TG_WIRED"    ]] && echo telegram ); do
        psql_core -q <<SQL
UPDATE sn_config.channels
   SET enabled = true,
       status  = 'available'
 WHERE slug = '$slug' AND enabled = false;
SQL
        log "channel registry: '$slug' marked enabled"
    done
    # The `smartnode` ident map must carry this OS-user -> role line for
    # peer auth to resolve. create_service_user appends it when
    # SMART_NODE_PG_IDENT is set; ensure it regardless, then reload the
    # cluster so the change takes effect.
    local ident_line="smartnode  $SERVICE_USER  $SERVICE_USER"
    if [[ -f "$PG_IDENT" ]] && ! grep -qF "$ident_line" "$PG_IDENT"; then
        echo "$ident_line" >> "$PG_IDENT"
        log "added the $SERVICE_USER line to the smartnode ident map"
    fi
    if systemctl is-active "$PG_UNIT" >/dev/null 2>&1; then
        systemctl reload "$PG_UNIT" >/dev/null 2>&1 \
            || systemctl restart "$PG_UNIT" >/dev/null 2>&1 \
            || die "failed to reload $PG_UNIT after the ident-map change"
    else
        pg_ctlcluster "${PG_CLUSTER%%/*}" "${PG_CLUSTER##*/}" reload >/dev/null 2>&1 || true
    fi
}

# write_env -- the runtime config, loaded by systemd via EnvironmentFile.
# The brin URL + API key are copied from the dispatcher companion's own
# .env (the dispatcher owns the generate-once key). 0600, service-user
# owned -- it carries the telegram token.
write_env() {
    [[ -r "$DISPATCHER_ENV" ]] \
        || die "dispatcher companion .env not readable ($DISPATCHER_ENV) -- install the dispatcher companion first"
    local brin_key brin_port brin_host
    brin_key=$(_envfile_get "$DISPATCHER_ENV" BRIN_API_KEY)
    brin_port=$(_envfile_get "$DISPATCHER_ENV" PORT)
    # Read the ADDRESS brin actually binds, do not assume it. It moved off
    # loopback to the docker bridge so containers can reach it, and a consumer
    # that hardcodes 127.0.0.1 simply stops finding it. Older nodes have no
    # HOST key at all -- those really are on loopback, so that is the fallback.
    brin_host=$(_envfile_get "$DISPATCHER_ENV" HOST)
    [[ -n "$brin_key" ]] \
        || die "no BRIN_API_KEY in $DISPATCHER_ENV -- the dispatcher companion install is incomplete"
    [[ -n "$brin_port" ]] || brin_port=8101
    [[ -n "$brin_host" ]] || brin_host=127.0.0.1
    local interval; interval=$(_carry_forward SMART_NODE_DISPATCH_INTERVAL DISPATCH_INTERVAL 3)

    # The business this node writes as. send_outbound.py puts it in the
    # From display name and the signature; core-api's notifications read the
    # same key and quietly substitute "Smart Node" when it is missing, so a lost
    # value does not fail -- the node just starts writing as somebody else.
    #
    # BOTH HALVES MATTER. The input is new; the carry-forward is what fixes the
    # nodes that are already installed. .env is rewritten wholesale on every
    # run, so before the carry-forward existed, a second companion run silently
    # stripped the name from every outbound message -- which is how this reached
    # us: not from an install, but from a RE-install.
    local business_name; business_name=$(_carry_forward SMART_NODE_BUSINESS_NAME SN_BUSINESS_NAME)

    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node communication companion runtime config -- communication.sh@$STAGE_VERSION."
        echo "# Mode 0600. Loaded by systemd via EnvironmentFile=."
        echo "# Core DB over the local socket, peer auth -- no password in the DSN."
        echo "# search_path puts sn_core first so db.py's unqualified \`messages\`"
        echo "# INSERT lands in sn_core.messages (the registry schema on current nodes)."
        echo "SMARTNODE_DB_DSN=dbname=$PG_CORE_DB port=$PG_PORT options='-c search_path=sn_core,sn_config,public'"
        echo "COMMUNICATION_STATE_DIR=$COMP_STATE_DIR"
        # path to the inbound attachment byte store.
        # The adapter writes here; core-api reads here through the retrieval API.
        echo "ATTACHMENT_DIR=/srv/data/attachments"
        if [[ -n "$business_name" ]]; then
            echo "# The business this node writes as: the From display name and"
            echo "# the signature on outbound messages. Unset -> no name line."
            echo "SN_BUSINESS_NAME=$business_name"
        fi
        if [[ -n "$TG_WIRED" ]]; then
            echo "# telegram channel"
            echo "TG_TOKEN=$SMART_NODE_TG_TOKEN"
            echo "TG_CHANNEL_ID=$SMART_NODE_TG_CHANNEL_ID"
        fi
        echo "# dispatch layer -> brin (the dispatcher companion, at the address it binds)"
        echo "BRIN_API_URL=http://$brin_host:$brin_port"
        echo "BRIN_API_KEY=$brin_key"
        echo "DISPATCH_INTERVAL=$interval"
        # Which channels the dispatch layer answers ITSELF, rather than leaving
        # to the agent's wake loop. This is not tuning: with it empty the layer
        # logs "no FAST_CHANNELS configured -- idle" and the node goes SILENT --
        # mail arrives, is registered, and nobody ever replies, because the wake
        # loop needs a per-agent LLM credential the cron user does not have.
        # an operator set this by hand on one node and the installer never learned it,
        # so every install would have shipped a mute node. List what was wired.
        local fast_channels=""
        [[ -n "$EMAIL_WIRED" ]] && fast_channels="email"
        [[ -n "$TG_WIRED"    ]] && fast_channels="${fast_channels:+$fast_channels,}telegram"
        # One owner, one override. An operator moving a channel to the back
        # Studio (mail answered by the agent's own wake rather than by the
        # dispatcher) sets this, and BOTH readers see the same value because
        # core-api is told by the contract file below rather than deriving it
        # a second time.
        fast_channels="${SMART_NODE_FAST_CHANNELS:-$fast_channels}"
        if [[ -n "$fast_channels" ]]; then
            echo "FAST_CHANNELS=$fast_channels"
        fi
        if [[ -n "$EMAIL_WIRED" ]]; then
            if [[ "$EMAIL_TOPOLOGY" == external ]]; then
                echo "# email channel -- an EXTERNAL mailserver this node does"
                echo "# not run. It is the MX target and holds the valid PTR,"
                echo "# certificate and DKIM; the node only reads its boxes."
            fi
            echo "IMAP_HOST=${EMAIL_IMAP_HOST:-$EMAIL_HOST}"
            echo "IMAP_PORT=$EMAIL_IMAP_PORT"
            echo "IMAP_FOLDER=INBOX"
            echo "SMTP_HOST=${EMAIL_SMTP_HOST:-$EMAIL_HOST}"
            echo "SMTP_PORT=$EMAIL_SMTP_PORT"
            # The single-mailbox identity is a FALLBACK. The multi-mailbox
            # poller derives every address from sn_config.agent_channels and
            # looks its password up in the accounts file, so an external node
            # with several boxes needs none of these.
            if [[ -n "$NODE_MAIL_ACCT" ]]; then
                echo "IMAP_USER=$NODE_MAIL_ACCT"
                echo "IMAP_PASSWORD=$EMAIL_NODE_PW"
                echo "SMTP_USER=$NODE_MAIL_ACCT"
                echo "SMTP_PASSWORD=$EMAIL_NODE_PW"
                echo "SMTP_FROM=$NODE_MAIL_ACCT"
                echo "EMAIL_CHANNEL_ID=$NODE_MAIL_ACCT"
            fi
            echo "EMAIL_TLS_VERIFY=$EMAIL_TLS_VERIFY"
            # Egress allowlist. Who this node may write to is the operator's
            # decision, so it is configuration and never a literal in the
            # sender. Both empty means nothing external can be emailed.
            # Carried forward when this run does not name one: an
            # allowlist emptied by a re-run silences every outbound message,
            # and the sender reports each one blocked, not failed.
            echo "EMAIL_ALLOWED_DOMAINS=$(_carry_forward SMART_NODE_MAIL_ALLOWED_DOMAINS EMAIL_ALLOWED_DOMAINS)"
            echo "EMAIL_ALLOWED_ADDRESSES=$(_carry_forward SMART_NODE_MAIL_ALLOWED_ADDRESSES EMAIL_ALLOWED_ADDRESSES)"
            echo "# Per-agent mailbox passwords (the generate-once store,"
            echo "# keyed PW_<addr>). The multi-mailbox"
            echo "# poller iterates sn_config.agent_channels and looks each up"
            echo "# here; the IMAP_USER/PASSWORD above are the single-mailbox"
            echo "# fallback. A mailbox with no entry is skipped, not fatal."
            echo "AGENT_ACCOUNTS_FILE=$MAIL_AGENT_ACCOUNTS"
        fi
        # The communication gate. Outside the email block on
        # purpose: it governs every channel, not just mail.
        echo "# Communication gate. on = a message may only go to a recipient"
        echo "# granted to the sending agent as a contact (deny by default);"
        echo "# off = the verdict is still journalled as message.blocked but"
        echo "# nothing is refused, so you can see what a node WOULD stop"
        echo "# before it starts stopping it. Manage contacts and grants via"
        echo "# core-api /api/contacts and /api/agents/<slug>/contacts."
        echo "# Default ON for new nodes. A fresh node has no agents"
        echo "# bound yet, so the gate is a safe no-op until agents are"
        echo "# provisioned from the panel. Once agents exist, grant the owner"
        echo "# as a contact of each agent (panel: agents/<slug>/contacts)"
        echo "# before wiring outbound channels, or those agents go silent."
        # Carried forward when this run does not name it. Switching
        # the gate off is a decision, and a re-run wiring an unrelated channel
        # is not the operator making it.
        echo "CONTACT_GATE=$(_carry_forward SMART_NODE_CONTACT_GATE CONTACT_GATE on)"
        # The inbound half. A SEPARATE switch, because it answers a
        # different question and has a different cost.
        echo "# Inbound gate. on = a message whose sender is not a contact"
        echo "# granted to the agent that owns the receiving mailbox is"
        echo "# recorded and never routed, so no model is billed for it;"
        echo "# off = the verdict is journalled and the message is routed"
        echo "# anyway. Off by default ON PURPOSE: a public inbox must not"
        echo "# stop noticing people who have not been granted yet. Switch it"
        echo "# on for a node whose mailbox is an existing address with"
        echo "# history, or one that must only ever answer its owner."
        echo "CONTACT_GATE_INBOUND=$(_carry_forward SMART_NODE_CONTACT_GATE_INBOUND CONTACT_GATE_INBOUND off)"
    } > "$tmp"
    install -m 0600 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" "$tmp" "$ENV_FILE"
    rm -f "$tmp"
    manifest_record file "$ENV_FILE"
}

# publish_fast_channels -- hand core-api the one value it needs from here.
#
# /api/agents/with-pending-work subtracts FAST_CHANNELS from the wake criterion
# so the Runner does not wake an agent for a message the dispatcher is already
# answering. core-api never received it: the value lived only in this
# companion's .env, core-api read an empty list, and both paths answered every
# message -- two turns on one agent, one of them replying with the wrong tools
#
# A separate 0644 file rather than a line in the unit, because the two
# companions install in the wrong order for that: core-api is written first and
# would carry an empty value until the next run. A separate file rather than
# core-api reading THIS .env, because that file is 0600 and carries the
# telegram token; the fast-channel list is not a secret and must not travel
# with one.
publish_fast_channels() {
    local value before=""
    value=$(_envfile_get "$ENV_FILE" FAST_CHANNELS)
    [[ -r "$FAST_CHANNELS_ENV" ]] && before=$(_envfile_get "$FAST_CHANNELS_ENV" FAST_CHANNELS)
    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node fast-channel contract -- written by communication.sh."
        echo "# Do not edit by hand. Read by core-api via EnvironmentFile=-."
        echo "#"
        echo "# The channels the dispatch layer answers ITSELF. Everything not"
        echo "# listed here reaches its agent through the Runner instead, which"
        echo "# wakes the agent to read its own inbox and reply with its own"
        echo "# tools. A channel listed on BOTH paths is answered twice."
        echo "FAST_CHANNELS=$value"
    } > "$tmp"
    if ! install -m 0644 -o root -g root "$tmp" "$FAST_CHANNELS_ENV" 2>/dev/null; then
        rm -f "$tmp"
        log "WARN: could not write $FAST_CHANNELS_ENV -- core-api keeps the fast-channel list it already had"
        return 0
    fi
    rm -f "$tmp"
    manifest_record file "$FAST_CHANNELS_ENV"

    # Restart only on a CHANGE. core-api reads this file at start, so a value
    # that moved while it was running is a value it does not have.
    [[ "$before" == "$value" ]] && return 0
    systemctl is-active --quiet smart-node-core-api.service 2>/dev/null || return 0
    log "FAST_CHANNELS ${before:-(none)} -> ${value:-(none)} -- restarting core-api"
    systemctl restart smart-node-core-api.service 2>/dev/null \
        || log "WARN: core-api restart failed -- run 'systemctl restart smart-node-core-api' by hand"
}

# _mailbox_has_password ADDRESS -- true only for an address this node can
# authenticate as.  Selecting a syntactically plausible From is the failure
# the mail channel is removing; the password store is the proof that the mailbox is real.
_mailbox_has_password() {
    local address="$1" key
    [[ "$address" == *@* && -r "$MAIL_AGENT_ACCOUNTS" ]] || return 1
    key="PW_${address//@/_}"
    key="${key//./_}"
    grep -Fq "$key=" "$MAIL_AGENT_ACCOUNTS"
}

# _resolve_notify_sender -- print the system-alert mailbox, or nothing when no
# email channel exists.  Explicit operator choice wins; otherwise preserve the
# installed choice, use the single-mailbox identity, then find the oldest
# enabled agent mailbox that has credentials.  The latter is what makes an
# update of a multi-mailbox node converge without a hand-written drop-in.
_resolve_notify_sender() {
    [[ -n "$EMAIL_WIRED" ]] || return 0

    local candidate="" previous=""
    previous=$(_envfile_get "$BUDGET_NOTIFICATIONS_ENV" SN_NOTIFY_SENDER)

    if [[ -n "${SMART_NODE_NOTIFY_EMAIL_SENDER+x}" ]]; then
        candidate="${SMART_NODE_NOTIFY_EMAIL_SENDER}"
        if ! _mailbox_has_password "$candidate"; then
            die "SMART_NODE_NOTIFY_EMAIL_SENDER=$candidate is not a mailbox with installed credentials in $MAIL_AGENT_ACCOUNTS"
        fi
        printf '%s' "$candidate"
        return 0
    fi

    if [[ -n "$previous" ]] && _mailbox_has_password "$previous"; then
        printf '%s' "$previous"
        return 0
    fi
    if [[ -n "$NODE_MAIL_ACCT" ]] && _mailbox_has_password "$NODE_MAIL_ACCT"; then
        printf '%s' "$NODE_MAIL_ACCT"
        return 0
    fi

    # instance_ref is the actual mailbox address.  Ask the registry instead of
    # trying to reverse PW_<address> keys (that encoding is not reversible).
    while IFS= read -r candidate; do
        [[ -n "$candidate" ]] || continue
        if _mailbox_has_password "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done < <(psql_core -Atc \
        "SELECT ac.instance_ref
           FROM sn_config.agent_channels ac
           JOIN sn_config.channels c ON c.id = ac.channel_id
           JOIN sn_config.agents a ON a.id = ac.agent_id
          WHERE c.slug = 'email'
            AND ac.enabled = true
            AND a.enabled = true
            AND a.archived_at IS NULL
          ORDER BY ac.created_at, a.id" 2>/dev/null || true)

    die "email is wired but no real sender can be selected for owner alerts. Set SMART_NODE_NOTIFY_EMAIL_SENDER to a configured mailbox whose password is installed in $MAIL_AGENT_ACCOUNTS"
}

# publish_budget_notifications -- one installer-owned decision shared by the
# producer (gateway) and outbox owner (core-api).  communication runs after both
# services on a fresh install, so it restarts them only when the contract value
# changes, just as publish_fast_channels does for core-api.
publish_budget_notifications() {
    local sender="" before="" after=""
    sender=$(_resolve_notify_sender)
    [[ -r "$BUDGET_NOTIFICATIONS_ENV" ]] \
        && before=$(sed -n -E '/^(BUDGET_NOTIFY_ACTOR|BUDGET_NOTIFY_CHANNEL|SN_NOTIFY_SENDER)=/p' "$BUDGET_NOTIFICATIONS_ENV")

    local tmp; tmp=$(mktemp)
    {
        echo "# Smart Node owner-notification contract -- written by communication.sh."
        echo "# Non-secret. Do not edit by hand. Passwords remain in the mailbox credential store."
        echo "BUDGET_NOTIFY_ACTOR=admin"
        echo "BUDGET_NOTIFY_CHANNEL=email"
        if [[ -n "$sender" ]]; then
            echo "SN_NOTIFY_SENDER=$sender"
        fi
    } > "$tmp"
    install -m 0644 -o root -g root "$tmp" "$BUDGET_NOTIFICATIONS_ENV"
    rm -f "$tmp"
    manifest_record file "$BUDGET_NOTIFICATIONS_ENV"
    after=$(sed -n -E '/^(BUDGET_NOTIFY_ACTOR|BUDGET_NOTIFY_CHANNEL|SN_NOTIFY_SENDER)=/p' "$BUDGET_NOTIFICATIONS_ENV")

    if [[ -n "$sender" ]]; then
        log "owner budget alerts -> email from configured mailbox $sender"
    else
        log "owner budget alerts remain retryable -- no email channel/sender is installed"
    fi
    [[ "$before" == "$after" ]] && return 0
    local unit
    for unit in smart-node-core-api.service smart-node-gateway.service; do
        systemctl is-active --quiet "$unit" 2>/dev/null || continue
        log "owner notification contract changed -- restarting $unit"
        systemctl restart "$unit" 2>/dev/null \
            || log "WARN: $unit restart failed -- restart it after fixing the service"
    done
}

# _unit_file UNIT_PATH DESCRIPTION EXEC_MODULE [EXTRA_AFTER]
# Write one native systemd unit. Both communication services share the
# same shape: the service user, the .env, a hardened sandbox.
_unit_file() {
    local path="$1" desc="$2" module="$3" extra_after="${4:-}"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
[Unit]
Description=$desc
After=network-online.target nss-lookup.target $PG_UNIT.service $extra_after
Wants=network-online.target nss-lookup.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
# Wait for the resolver to actually work before polling. On a fresh node the
# container DNS is often not ready when network-online.target fires, and the
# adapter's poll loop dies on the first NameResolutionError without exiting
# (so Restart=always never fires). Block up to ~60s for DNS, then start anyway.
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 30); do getent hosts \${SN_DNS_PROBE:-api.telegram.org} >/dev/null 2>&1 && exit 0; sleep 2; done; exit 0'
ExecStart=$VENV_DIR/bin/python -m $module
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$INSTALL_DIR $LOG_DIR
StandardOutput=append:$LOG_DIR/$module.log
StandardError=append:$LOG_DIR/$module.log

[Install]
WantedBy=multi-user.target
EOF
    install -m 0644 -o root -g root "$tmp" "$path"
    rm -f "$tmp"
}

# write_email_units -- the email channel's systemd units. Unlike the
# telegram/dispatch daemons email ingestion is a poll: one pass per tick
# (email is not latency-sensitive). So a Type=oneshot service plus a
# .timer that drives it every SMART_NODE_EMAIL_POLL_INTERVAL seconds. The
# oneshot has no [Install] -- the timer is the install unit. The pass is
# poll_mailboxes.py: it iterates every enabled mailbox in
# sn_config.agent_channels (per-agent boxes), not a single fixed account.
write_email_units() {
    local interval="${SMART_NODE_EMAIL_POLL_INTERVAL:-60}"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
[Unit]
Description=Smart Node communication -- email ingest (multi-mailbox poll)
After=network-online.target $PG_UNIT.service
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python poll_mailboxes.py
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$INSTALL_DIR $LOG_DIR /srv/data/attachments
StandardOutput=append:$LOG_DIR/poll_mailboxes.log
StandardError=append:$LOG_DIR/poll_mailboxes.log
EOF
    install -m 0644 -o root -g root "$tmp" "$UNIT_EMAIL_PATH"
    cat > "$tmp" <<EOF
[Unit]
Description=Smart Node communication -- email adapter poll timer

[Timer]
OnBootSec=60
OnUnitActiveSec=$interval
AccuracySec=10s
Unit=$UNIT_EMAIL

[Install]
WantedBy=timers.target
EOF
    install -m 0644 -o root -g root "$tmp" "$UNIT_EMAIL_TIMER_PATH"
    rm -f "$tmp"
    manifest_record systemd-unit "$UNIT_EMAIL"
    manifest_record systemd-unit "$UNIT_EMAIL_TIMER"
}

# write_send_outbound_units -- the node's single exit.
#
# A oneshot plus a timer, like the email poll, because transmitting is a drain
# pass and not a daemon: it reads every outbound row sitting at 'new', sends
# each on its own channel, and settles the row. Every channel leaves this way --
# email, telegram, slack, and whatever core-api queued -- so the egress guard
# and the outbound safety envelope have exactly one place to stand.
#
# The interval matches reference-node, the node that works: 2s. It is not a latency
# compromise -- a field measurement found 47s of a 57s reply inside the wake-and-inbox
# loop and cut this timer from 20s to 2s, then concluded "the loop, not the
# intervals, is the bottleneck". At 2s the courier costs a reply about as much
# as sending inline did, without giving up the one exit.
write_send_outbound_units() {
    local interval="${SMART_NODE_SEND_OUTBOUND_INTERVAL:-2}"
    local tmp; tmp=$(mktemp)
    cat > "$tmp" <<EOF
[Unit]
Description=Smart Node communication -- outbound sender (the node's single exit)
After=network-online.target $PG_UNIT.service
Wants=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SMART_NODE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python send_outbound.py
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=true
ProtectHome=true
ReadWritePaths=$INSTALL_DIR $LOG_DIR
StandardOutput=append:$LOG_DIR/send_outbound.log
StandardError=append:$LOG_DIR/send_outbound.log
EOF
    install -m 0644 -o root -g root "$tmp" "$UNIT_SEND_PATH"
    cat > "$tmp" <<EOF
[Unit]
Description=Smart Node communication -- outbound sender timer

[Timer]
OnBootSec=20s
OnUnitActiveSec=$interval
AccuracySec=1s
Unit=$UNIT_SEND

[Install]
WantedBy=timers.target
EOF
    install -m 0644 -o root -g root "$tmp" "$UNIT_SEND_TIMER_PATH"
    rm -f "$tmp"
    manifest_record systemd-unit "$UNIT_SEND"
    manifest_record systemd-unit "$UNIT_SEND_TIMER"
}








# install_services -- write and activate the systemd units. The telegram
# adapter ingests; the dispatch layer wakes brin and replies. When the
# mail companion is present the email channel is wired too (timer-driven).
# _daemon_units -- the long-running units this install actually owns.
# The dispatch layer is always there; the telegram ingest daemon only when
# that channel is wired. Email is a timer, not a daemon, so it is handled
# separately and never appears here.
_daemon_units() {
    [[ -n "$TG_WIRED" ]] && printf '%s ' "$UNIT_TELEGRAM"
    printf '%s\n' "$UNIT_DISPATCH"
}

install_services() {
    if [[ -n "$TG_WIRED" ]]; then
        _unit_file "$UNIT_TELEGRAM_PATH" \
            "Smart Node communication -- telegram adapter (ingest daemon)" \
            "adapters.telegram.adapter"
        manifest_record systemd-unit "$UNIT_TELEGRAM"
    fi
    # The dispatch layer needs brin; order it after the dispatcher unit
    # (soft -- it retries in its scan loop regardless).
    _unit_file "$UNIT_DISPATCH_PATH" \
        "Smart Node communication -- dispatch layer (registry -> brin -> reply)" \
        "dispatch" "smart-node-dispatcher.service"
    manifest_record systemd-unit "$UNIT_DISPATCH"
    [[ -n "$EMAIL_WIRED" ]] && write_email_units
    # Unconditional: the sole exit for every channel, not an email accessory.
    write_send_outbound_units
    systemctl daemon-reload
    local unit
    for unit in $(_daemon_units); do
        systemctl enable "$unit" >/dev/null 2>&1 \
            || die "systemctl enable $unit failed"
        systemctl restart "$unit" \
            || die "systemctl restart $unit failed"
    done
    if [[ -n "$EMAIL_WIRED" ]]; then
        # The email channel is enabled via its .timer, not the oneshot.
        systemctl enable "$UNIT_EMAIL_TIMER" >/dev/null 2>&1 \
            || die "systemctl enable $UNIT_EMAIL_TIMER failed"
        systemctl restart "$UNIT_EMAIL_TIMER" \
            || die "systemctl restart $UNIT_EMAIL_TIMER failed"
    fi
    # The outbound sender, likewise enabled via its .timer. Not gated on any
    # channel: a node with only telegram still needs its replies to leave.
    systemctl enable "$UNIT_SEND_TIMER" >/dev/null 2>&1 \
        || die "systemctl enable $UNIT_SEND_TIMER failed"
    systemctl restart "$UNIT_SEND_TIMER" \
        || die "systemctl restart $UNIT_SEND_TIMER failed"
}

# wait_for_services -- a started Type=simple unit reports active almost
# at once; give each a short grace window to survive its first loop
# (a bad token / DSN crashes the process and systemd flips it to
# failed). Polls is-active against a 20s deadline.
wait_for_services() {
    local deadline=$(( $(date +%s) + 20 )) unit ok
    for unit in $(_daemon_units); do
        ok=""
        while (( $(date +%s) < deadline )); do
            if systemctl is-active "$unit" >/dev/null 2>&1; then
                ok=1; break
            fi
            sleep 2
        done
        [[ -n "$ok" ]] \
            || die "$unit did not stay active -- check: journalctl -u $unit"
        log "$unit is active"
    done
    # The email channel is a timer: a timer reports active as soon as it
    # is started, so a single is-active check is enough (no grace loop).
    if [[ -n "$EMAIL_WIRED" ]]; then
        systemctl is-active "$UNIT_EMAIL_TIMER" >/dev/null 2>&1 \
            || die "$UNIT_EMAIL_TIMER did not activate -- check: systemctl status $UNIT_EMAIL_TIMER"
        log "$UNIT_EMAIL_TIMER is active"
    fi
}

write_report_section() {
    # The email line appears only when that channel was wired;
    # each carries a leading newline so an unwired install leaves no gap.
    local email_line=""
    [[ -n "$EMAIL_WIRED" ]] && email_line="
            $UNIT_EMAIL_TIMER  (email poll -- $NODE_MAIL_ACCT)"
    # Report what was actually wired -- every channel is optional now.
    local channels="" tg_line=""
    [[ -n "$TG_WIRED" ]]    && channels="telegram"
    [[ -n "$EMAIL_WIRED" ]] && channels="${channels:+$channels + }email"
    [[ -n "$TG_WIRED" ]] && tg_line="$UNIT_TELEGRAM  (telegram ingest daemon)
            "
    local body
    body=$(cat <<EOF
**Version:** communication@$STAGE_VERSION
**Marked done:** $(date -Iseconds)

\`\`\`
component:  communication (source tree, no vendored snapshot)
install:    $INSTALL_DIR
db role:    $SERVICE_USER  (peer auth, messages registry only)
config:     $ENV_FILE  (0600; channel credentials + brin key)
agent CLI:  $CLI_PATH  (registered as tool \`comm\`)
channels:   $channels
services:   $tg_line$UNIT_DISPATCH  (dispatch layer -> brin)$email_line
brin:       http://127.0.0.1  (the dispatcher companion, loopback)
\`\`\`

Channel adapters are transport only -- they land messages in the B02
messages registry. The dispatch layer is the realtime trigger: it
reads the registry, wakes brin, and sends the reply on the originating
channel. The telegram channel is always wired; the email channel is
wired when the mail companion is present (a timer-driven poll). The
available -- it is a container, not a venv module, managed through a
oneshot systemd unit that drives docker compose. The web (WebSocket)
adapter is deferred work and is not installed here.
EOF
)
    report_section "Companion communication" "$body"
}

run_intent() {
    require_root
    log "companion communication -- start"
    require_stage B02
    require_stage dispatcher
    log_init "$STAGE_ID"
    load_persistence_contract
    ensure_python
    probe_before
    # Every channel is optional and resolved the same way: wired when its
    # inputs are present, skipped with a reason when they are not. The node
    # must end up with at least one -- see require_a_channel below.
    if tg_wired_check; then
        TG_WIRED=1
        log "telegram credentials present -- wiring the telegram channel"
    elif adopt_telegram_from_env; then
        TG_WIRED=1
        log "telegram channel already wired on this node -- carrying its configuration forward"
    else
        log "telegram channel skipped -- no bot credentials (set SMART_NODE_TG_TOKEN + SMART_NODE_TG_CHANNEL_ID)"
    fi
    # One topology: the node reads and sends through a mail service it does not
    # host. Resolve before write_env / install_services consume the result.
    if external_mail_requested; then
        resolve_email_external
        EMAIL_WIRED=1
        log "mail service configured -- wiring the email channel (imap $EMAIL_IMAP_HOST, smtp $EMAIL_SMTP_HOST)"
    elif adopt_email_from_env; then
        EMAIL_WIRED=1
        log "email channel already wired on this node -- carrying its configuration forward, accounts file untouched"
    elif adopt_email_from_secret_store; then
        # Credentials deposited in the node's secret store (e.g. during initial
        # panel setup). The companion reads them back so an update run without
        # --mail-host can still update the mail channel.
        EMAIL_WIRED=1
        log "email channel credentials found in the secret store -- wiring the email channel (imap $EMAIL_IMAP_HOST)"
    else
        log "email channel skipped -- no SMART_NODE_MAIL_HOST, no existing .env config, no mailbox credentials in the secret store"
    fi
    require_a_channel
    # Service account + its owned home/log dirs + the ident-map line.
    create_service_user "$SERVICE_SHORT" >/dev/null
    install_tree
    install_agent_cli
    build_venv
    setup_db_role
    write_accounts_file
    write_env
    publish_fast_channels
    publish_budget_notifications
    install_services
    wait_for_services
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,dispatcher"
    register_comm_tool
    write_report_section
    # Final verify pass; record check count.
    VERIFY_ERRORS=0
    VERIFY_TOTAL=0
    run_verify_checks
    local errors=$VERIFY_ERRORS
    if (( errors > 0 )); then
        stage_mark "$STAGE_ID" failed \
            "version=communication@$STAGE_VERSION" \
            "checks_passed=$((VERIFY_TOTAL - errors))/$VERIFY_TOTAL" \
            "reason=post-run verify failed"
        die "$errors verification check(s) failed after run"
    fi
    stage_mark "$STAGE_ID" done \
        "version=communication@$STAGE_VERSION" \
        "checks_passed=$VERIFY_TOTAL/$VERIFY_TOTAL"
    if declare -F log_event >/dev/null; then
        log_event "system:installer" "companion.installed" "companion" "$STAGE_ID" \
            "{\"version\":\"$STAGE_VERSION\",\"component\":\"communication\",\"checks\":$VERIFY_TOTAL}"
    fi
    log "companion communication -- done ($VERIFY_TOTAL/$VERIFY_TOTAL checks)"
}

run_verify_checks() {
    # Resolve the persistence contract for the DB-facing checks; a
    # --verify run does not go through run_intent.
    [[ -n "$PG_CLUSTER" ]] || load_persistence_contract
    # 1. B02 precondition.
    verify_check "stage B02 done" require_stage B02
    # 2. dispatcher companion precondition.
    verify_check "companion dispatcher done" require_stage dispatcher
    # 3. service user exists.
    verify_check "service user $SERVICE_USER exists" id -u "$SERVICE_USER"
    # 4. install tree present.
    verify_check "install dir $INSTALL_DIR exists" test -d "$INSTALL_DIR"
    # 5. dispatch layer installed in the tree.
    verify_check "dispatch.py installed" test -f "$INSTALL_DIR/dispatch.py"
    # 6. telegram adapter installed in the tree.
    verify_check "telegram adapter installed" \
        test -f "$INSTALL_DIR/adapters/telegram/adapter.py"
    # 6b. agent CLI deployed where core-api tells agents to look.
    verify_check "agent CLI installed at $CLI_PATH" test -x "$CLI_PATH"
    # 6c. agent CLI is root-owned -- an agent must not rewrite its own tool.
    verify_check "agent CLI owned by root" bash -c "
        [[ \$(stat -c '%U' '$CLI_PATH' 2>/dev/null) == 'root' ]]"
    # 7. venv interpreter present.
    verify_check "venv interpreter present" test -x "$VENV_DIR/bin/python"
    # 8. core deps importable in the venv.
    verify_check "venv has psycopg2 + requests" \
        "$VENV_DIR/bin/python" -c "import psycopg2, requests"
    # 9. .env present and 0600.
    verify_check ".env present 0600" bash -c "
        [[ -f '$ENV_FILE' && \$(stat -c '%a' '$ENV_FILE' 2>/dev/null) == 600 ]]"
    # 10. .env owned by the service user.
    verify_check ".env owned by $SERVICE_USER" bash -c "
        [[ \$(stat -c '%U' '$ENV_FILE' 2>/dev/null) == '$SERVICE_USER' ]]"
    # 11. .env carries the telegram token -- only when that channel is wired.
    # Asserted against the unit file so --verify judges the node as installed,
    # not as this run configured it.
    if [[ -f "$UNIT_TELEGRAM_PATH" ]]; then
        verify_check ".env holds TG_TOKEN" bash -c "
            grep -qE '^TG_TOKEN=.+' '$ENV_FILE'"
    fi
    # 12. .env carries the brin API key.
    verify_check ".env holds BRIN_API_KEY" bash -c "
        grep -qE '^BRIN_API_KEY=.+' '$ENV_FILE'"
    # 13. .env carries the core-DB DSN.
    verify_check ".env holds SMARTNODE_DB_DSN" bash -c "
        grep -qE '^SMARTNODE_DB_DSN=.+' '$ENV_FILE'"
    # 14. PostgreSQL role exists.
    verify_check "PostgreSQL role $SERVICE_USER exists" bash -c "
        [[ \$(runuser -u postgres -- psql --cluster '$PG_CLUSTER' -d '$PG_CORE_DB' -tAc \
            \"SELECT 1 FROM pg_roles WHERE rolname='$SERVICE_USER'\" 2>/dev/null) == 1 ]]"
    # 15. ident map carries the service-user line.
    verify_check "smartnode ident map has the $SERVICE_USER line" bash -c "
        grep -qE '^smartnode[[:space:]]+$SERVICE_USER[[:space:]]+$SERVICE_USER\b' '$PG_IDENT'"
    # 16. the service user can actually reach the core DB -- this
    #     exercises peer auth + the ident map + the CONNECT grant.
    verify_check "service user connects to the core DB" \
        runuser -u "$SERVICE_USER" -- "$VENV_DIR/bin/python" -c \
        "import psycopg2; psycopg2.connect('dbname=$PG_CORE_DB port=$PG_PORT').close()"
    # --- telegram channel: only when it was wired (the unit exists).
    # Gated on the unit file -- not TG_WIRED -- so a --verify run, which does
    # not go through run_intent, validates it just the same. A mail-only node
    # has no telegram unit and must still verify clean.
    if [[ -f "$UNIT_TELEGRAM_PATH" ]]; then
        # 17. telegram unit installed.
        verify_check "unit $UNIT_TELEGRAM installed" test -f "$UNIT_TELEGRAM_PATH"
        # 18. telegram unit enabled.
        verify_check "unit $UNIT_TELEGRAM enabled" systemctl is-enabled "$UNIT_TELEGRAM"
        # 19. telegram unit active.
        verify_check "unit $UNIT_TELEGRAM active" systemctl is-active "$UNIT_TELEGRAM"
    fi
    # 20. dispatch unit installed.
    verify_check "unit $UNIT_DISPATCH installed" test -f "$UNIT_DISPATCH_PATH"
    # 21. dispatch unit enabled.
    verify_check "unit $UNIT_DISPATCH enabled" systemctl is-enabled "$UNIT_DISPATCH"
    # 22. dispatch unit active.
    verify_check "unit $UNIT_DISPATCH active" systemctl is-active "$UNIT_DISPATCH"
    # 23. companion manifest written.
    verify_check "companions.d/communication.json written" \
        test -r "$SMART_NODE_COMPANIONS_DIR/communication.json"
    # 24. install-report has the communication section.
    verify_check "install-report has the communication section" \
        bash -c "grep -qFx '## Companion communication' '$SMART_NODE_REPORT'"
    # 25. manifest records the install dir, the agent CLI and the dispatch
    # unit. The telegram unit is only recorded when that channel was wired,
    # so it is not part of the baseline assertion.
    verify_check "manifest records the communication artifacts" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"dir\",\"value\":\"$INSTALL_DIR\",' \"\$m\" &&
        grep -qF '{\"type\":\"file\",\"value\":\"$CLI_PATH\",' \"\$m\" &&
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$UNIT_DISPATCH\",' \"\$m\""
    # --- the node's single exit. Unconditional: asserted on every
    # install, because a node that cannot transmit cannot answer on ANY
    # channel, and the failure is silent -- replies queue forever and nothing
    # errors. These four checks are the ones whose absence shipped that state.
    # 26. outbound sender oneshot installed.
    verify_check "unit $UNIT_SEND installed" test -f "$UNIT_SEND_PATH"
    # 27. outbound sender timer installed.
    verify_check "unit $UNIT_SEND_TIMER installed" test -f "$UNIT_SEND_TIMER_PATH"
    # 28. outbound sender timer enabled.
    verify_check "unit $UNIT_SEND_TIMER enabled" systemctl is-enabled "$UNIT_SEND_TIMER"
    # 29. outbound sender timer active.
    verify_check "unit $UNIT_SEND_TIMER active" systemctl is-active "$UNIT_SEND_TIMER"
    # 30. and it is recorded, so uninstall can take it away again.
    verify_check "manifest records the outbound sender" bash -c "
        m=/var/lib/smart-node/manifest.jsonl
        [[ -f \"\$m\" ]] || exit 1
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$UNIT_SEND\",' \"\$m\" &&
        grep -qF '{\"type\":\"systemd-unit\",\"value\":\"$UNIT_SEND_TIMER\",' \"\$m\""
    # --- email channel: only when it was wired (the email unit exists).
    # Gating on the unit file -- not EMAIL_WIRED -- so a --verify run,
    # which does not go through run_intent, validates it just the same.
    if [[ -f "$UNIT_EMAIL_PATH" ]]; then
        # 31. email oneshot service installed.
        verify_check "unit $UNIT_EMAIL installed" test -f "$UNIT_EMAIL_PATH"
        # 32. email timer installed.
        verify_check "unit $UNIT_EMAIL_TIMER installed" test -f "$UNIT_EMAIL_TIMER_PATH"
        # 33. email timer enabled.
        verify_check "unit $UNIT_EMAIL_TIMER enabled" systemctl is-enabled "$UNIT_EMAIL_TIMER"
        # 34. email timer active.
        verify_check "unit $UNIT_EMAIL_TIMER active" systemctl is-active "$UNIT_EMAIL_TIMER"
        # 35. email adapter installed in the tree.
        verify_check "email adapter installed" \
            test -f "$INSTALL_DIR/adapters/email/adapter.py"
        # 36. .env carries the mailbox host.
        verify_check ".env holds IMAP_HOST" bash -c "
            grep -qE '^IMAP_HOST=.+' '$ENV_FILE'"
        # 37. the node can name a sender. Two legitimate shapes, and asserting
        # only the first would fail exactly the topology both real nodes use:
        #   single mailbox   -- SMTP_FROM in .env
        #   several mailboxes -- addresses come from sn_config.agent_channels
        #                        and passwords from the accounts file, so
        #                        .env carries no per-box identity at all.
        verify_check "email identity resolvable (SMTP_FROM or a mailbox accounts file)" bash -c "
            grep -qE '^SMTP_FROM=.+' '$ENV_FILE' && exit 0
            f=\$(sed -n 's/^AGENT_ACCOUNTS_FILE=//p' '$ENV_FILE' | tail -1)
            [[ -n \"\$f\" && -r \"\$f\" ]] && grep -qE '^[[:space:]]*PW_[A-Za-z0-9_-]+=.+' \"\$f\""
    fi
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
# operator-reversible; the companion owns an explicit --uninstall in
# addition to the manifest replay uninstall-from.sh performs.
run_uninstall_mode() {
    require_root
    log_init "$STAGE_ID"
    log "companion communication -- uninstall"
    local unit
    for unit in "$UNIT_SEND_TIMER" "$UNIT_SEND" \
                "$UNIT_EMAIL_TIMER" "$UNIT_EMAIL" "$UNIT_TELEGRAM" "$UNIT_DISPATCH"; do
        if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
        fi
    done
    rm -f "$UNIT_TELEGRAM_PATH" "$UNIT_DISPATCH_PATH" \
          "$UNIT_EMAIL_PATH" "$UNIT_EMAIL_TIMER_PATH" \
          "$UNIT_SEND_PATH" "$UNIT_SEND_TIMER_PATH"
    systemctl daemon-reload >/dev/null 2>&1 || true
    rm -rf "$INSTALL_DIR"
    unregister_companion "$STAGE_ID"
    # Drop the PostgreSQL role -- revoke its grants first (a role with
    # privileges still granted cannot be dropped). Best-effort: a missing
    # persistence contract must not abort the rest of the teardown, so
    # the contract is read softly here, not via the die-ing loader.
    local penv="${SMART_NODE_PERSISTENCE_ENV:-/etc/smart-node/persistence.env}"
    if [[ -r "$penv" ]]; then
        PG_CLUSTER=$(_envfile_get "$penv" SMART_NODE_PG_CLUSTER)
        PG_CORE_DB=$(_envfile_get "$penv" SMART_NODE_PG_CORE_DB)
    fi
    if [[ -n "$PG_CLUSTER" && -n "$PG_CORE_DB" ]] \
       && [[ "$(runuser -u postgres -- psql --cluster "$PG_CLUSTER" -d "$PG_CORE_DB" -tAc \
            "SELECT 1 FROM pg_roles WHERE rolname='$SERVICE_USER'" 2>/dev/null)" == 1 ]]; then
        psql_core <<SQL 2>/dev/null || log "WARN: could not drop role $SERVICE_USER -- drop it by hand"
REVOKE ALL ON TABLE messages, message_state FROM "$SERVICE_USER";
REVOKE ALL ON SCHEMA public FROM "$SERVICE_USER";
REVOKE ALL ON DATABASE "$PG_CORE_DB" FROM "$SERVICE_USER";
DROP ROLE IF EXISTS "$SERVICE_USER";
SQL
    fi
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        userdel "$SERVICE_USER" >/dev/null 2>&1 || true
    fi
    # The agent CLI. Remove the file and the dir it owns; /srv/tools itself
    # is shared ground for other components' tools -- never remove that.
    rm -f "$CLI_PATH"
    rmdir "$CLI_DIR" 2>/dev/null || true
    rm -f "$SMART_NODE_STATE_DIR/$STAGE_ID.state"
    log "companion communication -- uninstalled (log dir $LOG_DIR + the ident-map line left for inspection)"
}

# run_contracts_only -- write contracts without the install steps.
#
# INSTALL (skip): ensure_python (interpreter, no state written),
#   probe_before (log-only, no state written), create_service_user (OS
#   account, one-time), install_tree (application files, version-gated),
#   install_agent_cli (sn-comm binary, version-gated), build_venv (pip
#   install, version-gated), setup_db_role (pg role, one-time).
#
# CONTRACT (run): load_persistence_contract (needed to resolve channel
#   env vars), channel-wiring checks (set TG_WIRED and EMAIL_WIRED flags
#   consumed by write_accounts_file, write_env, and install_services),
#   write_accounts_file (mailbox passwords file at $MAIL_AGENT_ACCOUNTS --
#   if absent or stale the email poller cannot authenticate; idempotent,
#   skips adopted channels), write_env (env file read by the comm service
#   on every restart -- must be current), install_services (systemd unit
#   files -- must be current), register_companion (companions.d record for
#   version tracking), register_comm_tool (sn_config row -- must be
#   current), stage_mark done.
run_contracts_only() {
    require_root
    log "companion communication -- contracts only (v$STAGE_VERSION)"
    load_persistence_contract
    # Channel flags must be resolved so write_accounts_file, write_env, and
    # install_services know which channels to wire. Adopt whatever the node
    # already has.
    if tg_wired_check; then
        TG_WIRED=1
    elif adopt_telegram_from_env; then
        TG_WIRED=1
    fi
    if external_mail_requested; then
        resolve_email_external
        EMAIL_WIRED=1
    elif adopt_email_from_env; then
        EMAIL_WIRED=1
    fi
    write_accounts_file
    write_env
    publish_fast_channels
    publish_budget_notifications
    install_services
    register_companion "$STAGE_ID" "$STAGE_VERSION" "B02,dispatcher"
    register_comm_tool
    stage_mark "$STAGE_ID" done \
        "version=communication@$STAGE_VERSION"
    log "companion communication -- contracts only done"
}

# run_agent_cli -- the agent CLI and its contract, on a node with no channel.
#
# Every agent's instructions cite /srv/tools/comm/comm.sh (core-api
# lib/agent_fs.py), and agent-to-agent messages go through it, whether or not
# the node has Telegram or mail. install-on ran this companion only when a
# channel was given, so a channel-less node had no comm.sh at all: its agents
# were told to use a tool that was not there, and the e2e agent-comms check
# died on "comm.sh: No such file or directory" on the 1.12.46 stand.
#
# Installs the CLI and registers the comm tool, nothing else: no service
# account, no venv, no units, no stage mark, because no channel is wired and
# the companion proper is still not installed. A later run with a channel
# installs the rest. Checks its own result and fails if either is missing.
run_agent_cli() {
    require_root
    log "companion communication -- agent CLI only, no channel on this node (v$STAGE_VERSION)"
    load_persistence_contract
    install_agent_cli
    register_comm_tool
    [[ -x "$CLI_PATH" ]] || die "agent CLI not installed at $CLI_PATH"
    tool_registered_check || die "comm tool not registered with its contract"
    log "companion communication -- agent CLI only done"
}

case "$MODE" in
    run)             run_intent ;;
    verify)          run_verify_mode ;;
    status)          run_status_mode ;;
    uninstall)       run_uninstall_mode ;;
    contracts-only)  run_contracts_only ;;
    agent-cli)       run_agent_cli ;;
esac
