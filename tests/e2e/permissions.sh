#!/usr/bin/env bash
# E2E: the declared permission surface, on a LIVE Smart Node.
#
# The permission check performs an ACTION: a real agent tries to
# write into the core and is refused by the operating system. Everything here
# is done as the actual agent-<slug> users on the node, never simulated.
#
# What it asserts:
#   1. the stage is installed and the node matches the declared list
#   2. the group split holds -- agents in the agent group, never the service one
#   3. NEGATIVE: agents cannot write the core tree, the tools they run, the
#      agent definitions, the identity store, or the node's secrets
#   4. POSITIVE: agents can still do their job -- read their model credential,
#      write their own logs/state, the cron log, the scratch area
#   5. re-running the stage changes nothing (idempotent)
#   6. a reopened hole is DETECTED (verify reports drift) and REPAIRED (apply)
#   7. an undeclared writable path is REFUSED, not silently accepted
#
# 6 and 7 mutate the node and restore it. They are skipped without root.
#
# Node-local. Exit 0 = pass, 1 = fail. Filesystem permission checks.
set -uo pipefail

STAGE="${SN_PERMISSIONS:-/usr/local/lib/smart-node/permissions.sh}"
LIST="${SN_PERMISSIONS_LIST:-/usr/local/lib/smart-node/permissions.list}"
SERVICE_GROUP="${SMART_NODE_GROUP:-smart-node}"
AGENT_GROUP="${SMART_NODE_AGENT_GROUP:-smart-node-agents}"

fail=0
chk(){ if [ "$1" = 1 ]; then echo "  PASS: $2"; else echo "  FAIL: $2"; fail=1; fi; }
skip(){ echo "  SKIP: $1"; }

AGENTS=$(getent passwd | awk -F: '$1 ~ /^agent-/ {print $1}')
IS_ROOT=0; [ "$(id -u)" = 0 ] && IS_ROOT=1

echo "[e2e] permissions | stage=$STAGE | agents=$(echo "$AGENTS" | tr '\n' ' ')"

# --- 1. the stage is present and the node matches the list -----------------
[ -x "$STAGE" ]; chk $((1-$?)) "permission stage installed ($STAGE)"
[ -r "$LIST" ];  chk $((1-$?)) "declared list installed ($LIST)"
if [ -x "$STAGE" ]; then
    out=$("$STAGE" --verify 2>&1); rc=$?
    if [ "$rc" = 0 ]; then chk 1 "node matches the declared surface"
    else
        chk 0 "node matches the declared surface (rc=$rc)"
        echo "$out" | grep -E 'DRIFT|REFUSE' | head -20 | sed 's/^/      /'
    fi
fi

# --- 2. the group split ----------------------------------------------------
getent group "$AGENT_GROUP" >/dev/null 2>&1; chk $((1-$?)) "agent group '$AGENT_GROUP' exists"
for u in $AGENTS; do
    pg=$(id -gn "$u" 2>/dev/null)
    [ "$pg" = "$AGENT_GROUP" ]; chk $((1-$?)) "$u primary group is $AGENT_GROUP (is: $pg)"
    if id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qx "$SERVICE_GROUP"; then
        chk 0 "$u is NOT in the service group $SERVICE_GROUP"
    else
        chk 1 "$u is NOT in the service group $SERVICE_GROUP"
    fi
done

# --- 3. NEGATIVE: the boundary itself --------------------------------------
# Each probe is "can this agent do the thing the owner said no agent may do".
# A probe that SUCCEEDS is a failure of the boundary; it is cleaned up either way.
probe_no_create() {  # user path label
    local u="$1" p="$2" label="$3" f="$2/.e2e-probe-$1-$$"
    if [ ! -d "$p" ]; then skip "$label (path absent: $p)"; return; fi
    if sudo -n -u "$u" touch "$f" 2>/dev/null; then
        chk 0 "$label -- $u CREATED a file in $p"
        rm -f "$f" 2>/dev/null
    else
        chk 1 "$label"
    fi
}
probe_no_write() {   # user file label
    local u="$1" f="$2" label="$3"
    if [ ! -e "$f" ]; then skip "$label (file absent: $f)"; return; fi
    if sudo -n -u "$u" test -w "$f" 2>/dev/null; then
        chk 0 "$label -- $u can WRITE $f"
    else
        chk 1 "$label"
    fi
}
probe_no_read() {    # user file label
    local u="$1" f="$2" label="$3"
    if [ ! -e "$f" ]; then skip "$label (file absent: $f)"; return; fi
    if sudo -n -u "$u" test -r "$f" 2>/dev/null; then
        chk 0 "$label -- $u can READ $f"
    else
        chk 1 "$label"
    fi
}

for u in $AGENTS; do
    probe_no_create "$u" /opt/smart-node            "$u cannot create in the core tree"
    probe_no_create "$u" /srv/tools                 "$u cannot create in the tools it runs"
    probe_no_create "$u" /srv/agents                "$u cannot create in the agent definitions"
    probe_no_create "$u" /var/lib/smart-node/authum/auth "$u cannot create in the identity store"
    probe_no_write  "$u" /var/lib/smart-node/authum/auth/users.json "$u cannot rewrite the account store"
    probe_no_read   "$u" /etc/smart-node/age-key.txt "$u cannot read the age master key"
    probe_no_write  "$u" /etc/smart-node/integrations.sops.yaml "$u cannot rewrite the secret store"
    probe_no_write  "$u" /opt/smart-node/communication/adapters/email/adapter.py \
                    "$u cannot modify mail adapter code another service executes"
done

# --- 4. POSITIVE: agents can still do their job ----------------------------
# Half of this task's risk is locking the agents out in a way that looks like
# an LLM outage. These probes are as load-bearing as the negative ones.
for u in $AGENTS; do
    slug="${u#agent-}"
    # Absent is NOT a permission failure: a headless install defers the LLM
    # login, so the file is legitimately missing on a fresh node (validate.sh
    # raises its own advisory for that). Only an EXISTING credential the agent
    # cannot read is the lock-out this check exists to catch.
    if [ ! -e /etc/smart-node/agent-runtime.env ]; then
        skip "$u model credential (agent-runtime.env absent -- LLM login deferred)"
    elif sudo -n -u "$u" test -r /etc/smart-node/agent-runtime.env 2>/dev/null; then
        chk 1 "$u can read its model credential"
    else
        chk 0 "$u can read its model credential -- LOCKED OUT, it will look like an LLM outage"
    fi
    for sub in logs state; do
        d="/srv/agents/$slug/$sub"
        if [ -d "$d" ]; then
            f="$d/.e2e-probe-$$"
            if sudo -n -u "$u" touch "$f" 2>/dev/null; then chk 1 "$u can write its own $sub"; rm -f "$f"
            else chk 0 "$u can write its own $sub"; fi
        else skip "$u own $sub (absent)"; fi
    done
    for d in /var/log/smart-node/cron /opt/smart-node/scratch; do
        if [ -d "$d" ]; then
            f="$d/.e2e-probe-$u-$$"
            if sudo -n -u "$u" touch "$f" 2>/dev/null; then chk 1 "$u can write $d"; rm -f "$f"
            else chk 0 "$u can write $d"; fi
        else skip "$u write $d (absent)"; fi
    done
done

# --- 4b. POSITIVE for SERVICES, not just agents -----------------------------
# The lesson that cost a working login: the suite checked that agents are
# refused and that agents still work, and nothing checked that the SERVICE
# whose data was being locked down could still read it. Tightening
# users.json to 0660 root:smart-node closed world-read and locked Authum out
# of its own account store -- password login returned a PHP warning instead of
# JSON, while passkey login kept working, so it looked fine from outside.
#
# Any file owned by an in-container uid is a service's data. Prove the service
# can still use it, from inside its own container.
if command -v docker >/dev/null 2>&1 \
   && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx smart-node-authum; then
    for f in users.json credentials.json; do
        # Absent is not a lockout. credentials.json only exists once someone has
        # registered a passkey; a password-only node legitimately has no such
        # file, and calling that a failure hides the real ones.
        if ! docker exec -u 82 smart-node-authum sh -c "test -e /app/auth/$f" 2>/dev/null; then
            skip "authum $f (not present -- nothing registered yet)"
        elif docker exec -u 82 smart-node-authum sh -c "cat /app/auth/$f >/dev/null 2>&1"; then
            chk 1 "authum can read its own $f"
        else
            chk 0 "authum can read its own $f -- the identity provider is locked out of its store"
        fi
    done
    if docker exec -u 82 smart-node-authum sh -c "test -w /app/auth/users.json"; then
        chk 1 "authum can write its account store"
    else
        chk 0 "authum can write its account store"
    fi
else
    skip "authum service access (container not running)"
fi

# --- 5. idempotence --------------------------------------------------------
if [ "$IS_ROOT" = 1 ] && [ -x "$STAGE" ]; then
    "$STAGE" >/dev/null 2>&1
    second=$("$STAGE" 2>&1); rc=$?
    changed=$(echo "$second" | grep -c '^fixed ')
    { [ "$rc" = 0 ] && [ "$changed" = 0 ]; }; chk $((1-$?)) "second apply run changes nothing (idempotent)"
else
    skip "idempotence (needs root)"
fi

# --- 6. a reopened hole is detected AND repaired ---------------------------
if [ "$IS_ROOT" = 1 ] && [ -x "$STAGE" ] && [ -d /srv/tools ]; then
    before=$(stat -c %a /srv/tools)
    chmod g+w /srv/tools
    "$STAGE" --verify >/dev/null 2>&1; rc=$?
    [ "$rc" != 0 ]; chk $((1-$?)) "a reopened hole is DETECTED by --verify"
    "$STAGE" >/dev/null 2>&1
    after=$(stat -c %a /srv/tools)
    [ "$after" = "$before" ]; chk $((1-$?)) "a reopened hole is REPAIRED by apply ($before -> $after)"
    [ "$after" = "$before" ] || chmod "$before" /srv/tools
else
    skip "reopen/repair (needs root)"
fi

# --- 7. an undeclared writable path is refused -----------------------------
if [ "$IS_ROOT" = 1 ] && [ -x "$STAGE" ] && [ -d /opt/smart-node ]; then
    probe=/opt/smart-node/.e2e-undeclared-$$
    mkdir -p "$probe" && chmod 0775 "$probe"
    "$STAGE" --verify >/dev/null 2>&1; rc=$?
    [ "$rc" = 2 ]; chk $((1-$?)) "an undeclared writable path is REFUSED (rc=$rc, want 2)"
    rmdir "$probe" 2>/dev/null
    "$STAGE" --verify >/dev/null 2>&1; rc=$?
    [ "$rc" = 0 ]; chk $((1-$?)) "surface is clean again after the probe is removed"
else
    skip "undeclared refusal (needs root)"
fi

# --- 8. the project work area ----------------------
#
# A project is a row, a unix group and /srv/projects/<slug> at 2770 setgid. The
# only claim worth testing is that a MEMBER can write there and a NON-MEMBER
# cannot, and that claim is worthless unless both halves run.
#
# CONSTRUCT THE FAILING STATE. "run a command as a non-member, expect non-zero"
# passes on a typo in the path, on a missing directory, on a user that does not
# exist. So the member case runs FIRST and in the same directory; if it fails,
# the negative half proves nothing and this says so instead of reporting green.
# The runbook drills found three checks that passed whether or not the thing
# worked, and this section is written against that.
#
# Node-mutating, so root only. Everything it makes, it removes.
PROJECT_ROOT="${SN_PROJECT_ROOT:-/srv/projects}"
member=$(echo "$AGENTS" | sed -n 1p)
outsider=$(echo "$AGENTS" | sed -n 2p)
if [ "$IS_ROOT" != 1 ]; then
    skip "project work area (needs root)"
elif [ -z "$member" ]; then
    skip "project work area (no agent users on this node)"
else
    pslug="e2e-area-$$"
    pgroup="project-$pslug"
    pdir="$PROJECT_ROOT/$pslug"

    install -d -m 0755 -o root -g root "$PROJECT_ROOT"
    groupadd --system "$pgroup" >/dev/null 2>&1
    mkdir -p "$pdir" && chgrp "$pgroup" "$pdir" && chmod 2770 "$pdir"
    usermod -a -G "$pgroup" "$member" >/dev/null 2>&1

    mode=$(stat -c %a "$pdir" 2>/dev/null)
    [ "$mode" = "2770" ]; chk $((1-$?)) "project dir is 2770 setgid (is: ${mode:-absent})"
    owner=$(stat -c %G "$pdir" 2>/dev/null)
    [ "$owner" = "$pgroup" ]; chk $((1-$?)) "project dir is owned by its group (is: ${owner:-none})"

    # The shared root is traversable by all and writable by none: a project must
    # not be creatable by making a directory.
    root_mode=$(stat -c %a "$PROJECT_ROOT" 2>/dev/null)
    [ "$root_mode" = "755" ]; chk $((1-$?)) "$PROJECT_ROOT is 0755 (is: ${root_mode:-absent})"

    # POSITIVE FIRST. `sg` picks up the new group without waiting for a login --
    # a process keeps the group list it started with, and without this the test
    # would fail for a reason that has nothing to do with the boundary.
    mfile="$pdir/.e2e-member-$$"
    if sudo -n -u "$member" sg "$pgroup" -c "touch '$mfile'" 2>/dev/null; then
        chk 1 "a member ($member) can write in the project area"
        fgroup=$(stat -c %G "$mfile" 2>/dev/null)
        [ "$fgroup" = "$pgroup" ]
        chk $((1-$?)) "a file created there inherits the project group -- setgid works (is: ${fgroup:-none})"
        member_ok=1
    else
        chk 0 "a member ($member) can write in the project area"
        member_ok=0
    fi

    # NEGATIVE, and only meaningful because the positive above passed.
    if [ -z "$outsider" ]; then
        skip "a non-member is refused (only one agent user on this node)"
    elif [ "$member_ok" != 1 ]; then
        skip "a non-member is refused (the member case failed -- this would prove nothing)"
    elif sudo -n -u "$outsider" touch "$pdir/.e2e-outsider-$$" 2>/dev/null; then
        chk 0 "a NON-member ($outsider) is refused -- it WROTE in $pdir"
        rm -f "$pdir/.e2e-outsider-$$"
    else
        chk 1 "a non-member ($outsider) is refused"
        # ...and refused by the operating system, not by a prompt: the same user
        # cannot even list the directory.
        if sudo -n -u "$outsider" ls "$pdir" >/dev/null 2>&1; then
            chk 0 "a non-member cannot even list the project area -- it LISTED $pdir"
        else
            chk 1 "a non-member cannot even list the project area"
        fi
    fi

    rm -f "$pdir"/.e2e-* 2>/dev/null
    rmdir "$pdir" 2>/dev/null
    gpasswd -d "$member" "$pgroup" >/dev/null 2>&1
    groupdel "$pgroup" >/dev/null 2>&1
    [ ! -d "$pdir" ]; chk $((1-$?)) "cleanup removed the test project area"
fi

# --- 9. the agent's own work area ----------------------------------
# /srv/projects is the SHARED area and does not cover this: an agent with no
# project still needs somewhere of its own, and until it had one "the developer
# writes code" was impossible on a node -- a permissions fact, not a policy.
for u in $AGENTS; do
    slug="${u#agent-}"
    wdir="/srv/agents/$slug/work"
    if [ ! -d "$wdir" ]; then skip "$u can write its own work/ (absent: $wdir)"; continue; fi
    wf="$wdir/.e2e-work-$$"
    if sudo -n -u "$u" touch "$wf" 2>/dev/null; then
        chk 1 "$u can write its own work/"
        rm -f "$wf"
    else
        chk 0 "$u can write its own work/ -- it CANNOT"
    fi
    # ...while the agent directory ROOT stays out of reach: the config files
    # there are owned by the provisioner, and an agent rewriting its own
    # CLAUDE.md is an agent editing the instructions it is judged against.
    probe_no_create "$u" "/srv/agents/$slug" "$u cannot create in its own agent dir root"
done

# --- 10. the GATEWAY path arrives as the agent ---------------------
# Sections 8 and 9 prove what the filesystem does for an agent's own uid. They
# say nothing about whether the gateway ever HANDS the process that uid, and
# previously it did not: every agent woken through the gateway ran as
# smart-node-gateway, so a per-agent group check on that path was testing the
# gateway account. Everything below is about the delivery, not the filesystem.
GW_SUDOERS="/etc/sudoers.d/smart-node-gateway-runas"
GW_UNIT="/etc/systemd/system/smart-node-gateway.service"
GW_USER="smart-node-gateway"
if [ ! -f "$GW_UNIT" ]; then
    skip "gateway path checks (no gateway companion on this node)"
else
    [ -f "$GW_SUDOERS" ]; chk $((1-$?)) "gateway runas rule installed"
    if [ "$IS_ROOT" = 1 ] && [ -f "$GW_SUDOERS" ]; then
        visudo -c -f "$GW_SUDOERS" >/dev/null 2>&1; chk $((1-$?)) "gateway runas rule parses"
    fi
    # An ABSOLUTE CLI path. The gateway's own default for this is the bare name
    # `claude` resolved through PATH, and a sudoers rule cannot match that -- so
    # a node with a bare path has a rule that permits nothing and a gateway that
    # cannot spawn. The failure is at the first chat, not at install.
    grep -qE '^Environment=CLI_PATH=/' "$GW_UNIT"
    chk $((1-$?)) "gateway unit carries an absolute CLI_PATH"
    # NoNewPrivileges must be OFF on the RUNTIME unit, and this check exists to
    # stop a well-meaning hardening pass from turning it back on: sudo is setuid,
    # and with the flag set the drop fails and every agent silently shares one
    # uid again. Since the door and the runtime became two processes the door
    # spawns nothing and keeps NoNewPrivileges=true; the agent processes are the
    # runtime's, so the drop is the runtime unit's to allow. A missing runtime
    # unit fails this check, it does not skip it.
    RT_UNIT="/etc/systemd/system/smart-node-runtime.service"
    grep -qE '^NoNewPrivileges=false' "$RT_UNIT" 2>/dev/null
    chk $((1-$?)) "runtime unit allows the privilege drop (NoNewPrivileges=false)"
    grep -qE '^NoNewPrivileges=true' "$GW_UNIT"
    chk $((1-$?)) "gateway door keeps NoNewPrivileges=true (it spawns nothing)"
    if [ "$IS_ROOT" = 1 ]; then
        # The PAIR. "root is refused" passes just as well on a rule that permits
        # nothing at all, so the permitted case is asserted in the same breath.
        # What the gateway is ARMED to run, which is no longer the CLI itself.
        # The privilege drop now goes through the credential wrapper the gateway
        # installs (CLI_AGENT_WRAPPER_PATH), and the sudoers rule names that.
        # Asserting CLI_PATH asserted a shape the product had left behind, so
        # this check failed on a healthy node while the drop worked perfectly.
        # CLI_PATH is still read as the fallback, for a node from before the
        # wrapper existed.
        CLI_ABS=$(grep -oE '^Environment=CLI_AGENT_WRAPPER_PATH=\S+' "$GW_UNIT" | cut -d= -f3)
        [ -n "$CLI_ABS" ] || CLI_ABS=$(grep -oE '^Environment=CLI_PATH=\S+' "$GW_UNIT" | cut -d= -f3)
        CLI_ABS="${CLI_ABS:-/usr/local/bin/claude}"
        sudo -n -l -U "$GW_USER" 2>/dev/null | grep -q "(%$AGENT_GROUP).*$CLI_ABS"
        chk $((1-$?)) "gateway MAY run the CLI as %$AGENT_GROUP"
        if sudo -n -l -U "$GW_USER" -u root "$CLI_ABS" >/dev/null 2>&1; then
            chk 0 "gateway may NOT run the CLI as root -- it CAN"
        else
            chk 1 "gateway may NOT run the CLI as root"
        fi
    else
        skip "gateway runas pair (needs root to read another user's sudo rights)"
    fi
fi

# --- 11. the kernel names the caller on core-api's socket ----------
# The socket is where an agent's identity stops being a header it typed. If the
# directory loses its group or its setgid bit the socket becomes unreachable and
# every agent silently falls back to the TCP path -- which the API then refuses,
# so the node stops working rather than quietly weakening. Checked here because
# it is a PERMISSION fact: a mode and a group are the whole mechanism.
API_SOCK="${SN_CORE_API_UDS:-/run/smart-node/api/core-api.sock}"

# SN_CORE_API used to be read straight from the environment, and nothing on the
# node supplies it: run-all.sh sources no environment at all. So the TCP half of
# the check below never reached a host, curl returned 000, and a check that
# wanted a 403 read as a failure on a healthy node. Derive it instead, from the
# same integrations catalog core-api itself is registered in -- an address baked
# in here would rot the first time the docker network is renumbered.
if [ -z "${SN_CORE_API:-}" ]; then
    _pg_cluster="${SMART_NODE_PG_CLUSTER:-}"
    if [ -z "$_pg_cluster" ] && [ -r /etc/smart-node/persistence.env ]; then
        # shellcheck source=/dev/null
        . /etc/smart-node/persistence.env
        _pg_cluster="${SMART_NODE_PG_CLUSTER:-}"
    fi
    SN_CORE_API=$(runuser -u postgres -- psql ${_pg_cluster:+--cluster "$_pg_cluster"} \
        -d "${SMART_NODE_PG_CORE_DB:-smartnode}" -At \
        -c "SELECT endpoint FROM sn_config.integrations WHERE slug = 'core-api' AND archived_at IS NULL" \
        2>/dev/null)
fi
API_SOCK_DIR=$(dirname "$API_SOCK")
if [ ! -e "$API_SOCK" ]; then
    skip "core-api socket checks (no socket at $API_SOCK)"
else
    [ "$(stat -c '%a %G' "$API_SOCK_DIR" 2>/dev/null)" = "2750 $AGENT_GROUP" ]
    chk $((1-$?)) "socket dir is 2750 and group $AGENT_GROUP"
    [ "$(stat -c '%a %G' "$API_SOCK" 2>/dev/null)" = "660 $AGENT_GROUP" ]
    chk $((1-$?)) "socket is 0660 and inherited group $AGENT_GROUP (setgid works)"
    for u in $AGENTS; do
        slug="${u#agent-}"
        # Both halves, one agent, one run. Refused over TCP, accepted over the
        # socket. Either alone is satisfied by a broken node: a node whose API is
        # down refuses everything, and a node with no rule accepts everything.
        tcp=$(sudo -n -u "$u" curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
              -H "Remote-User: $slug" \
              "${SN_CORE_API:-}/api/planner/api/v1/tasks?limit=1" 2>/dev/null)
        uds=$(sudo -n -u "$u" curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
              --unix-socket "$API_SOCK" \
              "http://core-api/api/planner/api/v1/tasks?limit=1" 2>/dev/null)
        [ "$tcp" = "403" ]; chk $((1-$?)) "$slug asserting its name over TCP is refused (got $tcp)"
        case "$uds" in 2*) chk 1 "$slug over the socket is accepted (got $uds)";;
                       *)  chk 0 "$slug over the socket is accepted -- got $uds";; esac
        break   # one agent proves the mechanism; the rest share the same rule
    done
fi

echo "================================"
if [ "$fail" = 0 ]; then echo "[e2e] permissions: ALL GREEN"; else echo "[e2e] permissions: FAILURES"; fi
exit $fail
