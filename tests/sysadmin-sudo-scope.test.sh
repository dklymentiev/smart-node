#!/usr/bin/env bash
#
# sysadmin-sudo-scope.test.sh -- B01 check 22, the effective sudo scope of
# every agent-* member of grp-role-sysadmin that is also in smart-node-agents
# through the validated SN_OP wrapper.
#
# The check it descends from grepped `sudo -l` output for the Cmnd_Alias NAMES
# (SN_WORK_PERM / SN_WORK_GROUP). sudo expands aliases and never prints their
# names, so that check failed on every node that has the user and passed on
# every stand. The current check compares expanded commands using
# sudoers_alias_commands and sudo_ll_within against the SHIPPED sidecar. It keeps that
# plumbing but retargets it: the subject is the role group %grp-role-sysadmin
# rather than a single user (which could be renamed), and the alias is SN_OP
# (the validated sn-op wrapper),
# not the superseded SN_WORK_PERM / SN_WORK_GROUP.
#
# This test exercises the comparison against recorded `sudo -ll` output shapes:
#
#   1. the shipped sidecar expands to the single command of SN_OP; an alias
#      name that is not there is an error, not an empty allowed set.
#   2. the narrow SN_OP grant passes, in both sudo output forms (1.9.15 on
#      a node running sudo 1.9.15 and a container running sudo 1.9.13).
#   3. every widening fails: (ALL) NOPASSWD: ALL, (root) NOPASSWD: ALL,
#      membership of group sudo, one extra command, a run-as other than
#      root, a run-as group, a rule source that is not a sudoers entry, a
#      user with no grant at all, and any residual mention of the legacy
#      SN_WORK_PERM / SN_WORK_GROUP aliases (they do not appear in the SN_OP
#      canon, so a sudo -ll line naming their commands falls outside).
#   4. B01 --verify check 22 calls sysadmin_sudo_scope_ok, which reads
#      `sudo -ll` for each measured member (from the group walk), and
#      compares against the SN_OP canon of the sidecar. No alias-name grep
#      of sudo output remains.
#
# Usage: tests/sysadmin-sudo-scope.test.sh [--baseline REF]
#   --baseline REF  run against bootstrap/lib.sh and B01-core.sh at REF. On
#                   an older baseline it fails if the helpers are absent or
#                   the sidecar names SN_WORK_PERM/SN_WORK_GROUP instead of SN_OP.

set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
BASELINE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --baseline) BASELINE="${2:?--baseline needs a ref}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch() {  # repo-relative path -> $WORK/<basename>
    local rel="$1" dst="$WORK/$(basename "$1")"
    if [[ -n "$BASELINE" ]]; then
        git -C "$ROOT" show "$BASELINE:$rel" > "$dst" 2>/dev/null \
            || { echo "  [setup] cannot read $rel at $BASELINE" >&2; return 1; }
    else
        cp "$ROOT/$rel" "$dst" 2>/dev/null \
            || { echo "  [setup] missing $rel in working tree" >&2; return 1; }
    fi
}
fetch bootstrap/lib.sh || exit 2
fetch bootstrap/B01-core.sh || exit 2
fetch provision/sudoers/agent-max.sudoers || exit 2

PASS=0
FAIL=0
pass() { echo "  [PASS] $*"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

# shellcheck source=/dev/null
source "$WORK/lib.sh"

if ! declare -F sudoers_alias_commands >/dev/null || ! declare -F sudo_ll_within >/dev/null; then
    fail "lib.sh defines no sudoers_alias_commands / sudo_ll_within -- check 22 has nothing to compare with"
    echo "sysadmin-sudo-scope: $PASS passed, $FAIL failed"
    exit 1
fi

fx() { sed 's/^|/\t/'; }

# The SN_OP alias in the shipped sidecar expands to exactly one command.
NARROW_CMDS='|/usr/local/sbin/sn-op'

NARROW_ENTRY="Sudoers entry:
    RunAsUsers: root
    Options: !authenticate
    Commands:
$NARROW_CMDS"

# Measured member is agent-max (the personal-name form of the sysadmin per
# a member of grp-role-sysadmin and smart-node-agents).
HEAD_1915='Matching Defaults entries for agent-max on example-node:
    env_reset, mail_badpass, secure_path=/usr/local/sbin\:/usr/local/bin\:/usr/sbin\:/usr/bin\:/sbin\:/bin\:/snap/bin, use_pty

User agent-max may run the following commands on example-node:
'
HEAD_1913='User agent-max may run the following commands on example-node:
'

# ---- 1. the allowed set --------------------------------------------------------

ALLOWED="$WORK/allowed"
if sudoers_alias_commands "$WORK/agent-max.sudoers" SN_OP > "$ALLOWED" 2>/dev/null \
        && [[ $(wc -l < "$ALLOWED") -eq 1 ]] \
        && diff -q "$ALLOWED" <(fx <<< "$NARROW_CMDS" | sed 's/^\t//') >/dev/null; then
    pass "1: sidecar expands to the single SN_OP command (/usr/local/sbin/sn-op)"
else
    fail "1: sidecar expansion is not the 1 expected SN_OP command (got $(wc -l < "$ALLOWED"))"
fi
if sudoers_alias_commands "$WORK/agent-max.sudoers" SN_OP SN_OP_RENAMED >/dev/null 2>&1; then
    fail "1: an alias missing from the sidecar was accepted"
else
    pass "1: an alias missing from the sidecar is an error"
fi

# ---- 2 and 3. the comparison ---------------------------------------------------

expect() {  # pass|fail  label  <<< sudo -ll output
    local want="$1" label="$2" reason rc
    reason="$(fx | sudo_ll_within "$ALLOWED" 2>&1)"; rc=$?
    if [[ $want == pass && $rc -eq 0 ]]; then
        pass "2: $label passes"
    elif [[ $want == fail && $rc -ne 0 ]]; then
        pass "3: $label fails ($reason)"
    else
        fail "$label: wanted $want, got rc=$rc ${reason:+($reason)}"
    fi
}

expect pass "narrow SN_OP grant, sudo 1.9.15 form" <<< "${HEAD_1915}
Sudoers entry: /etc/sudoers.d/agent-max
    RunAsUsers: root
    Options: !authenticate
    Commands:
$NARROW_CMDS"

expect pass "narrow SN_OP grant, sudo 1.9.13 form (golden)" <<< "${HEAD_1913}
$NARROW_ENTRY"

expect fail "(ALL) NOPASSWD: ALL drop-in" <<< "${HEAD_1913}
Sudoers entry:
    RunAsUsers: ALL
    Options: !authenticate
    Commands:
|ALL

$NARROW_ENTRY"

expect fail "(root) NOPASSWD: ALL drop-in" <<< "${HEAD_1913}
Sudoers entry:
    RunAsUsers: root
    Options: !authenticate
    Commands:
|ALL

$NARROW_ENTRY"

expect fail "member of group sudo" <<< "${HEAD_1913}
Sudoers entry:
    RunAsUsers: ALL
    RunAsGroups: ALL
    Commands:
|ALL

$NARROW_ENTRY"

expect fail "one extra command (/bin/bash)" <<< "${HEAD_1913}
$NARROW_ENTRY
|/bin/bash"

expect fail "run as postgres" <<< "${HEAD_1913}
Sudoers entry:
    RunAsUsers: postgres
    Options: !authenticate
    Commands:
|/usr/local/sbin/sn-op"

expect fail "run as root plus a group" <<< "${HEAD_1913}
Sudoers entry:
    RunAsUsers: root
    RunAsGroups: shadow
    Commands:
|/usr/local/sbin/sn-op"

expect fail "a rule source that is not a sudoers entry" <<< "${HEAD_1913}
$NARROW_ENTRY

LDAP Role: sysadmins
    RunAsUsers: root
    Commands:
|/usr/local/sbin/sn-op"

expect fail "residual legacy SN_WORK_PERM commands from a stale drop-in" <<< "${HEAD_1913}
$NARROW_ENTRY

Sudoers entry:
    RunAsUsers: root
    Options: !authenticate
    Commands:
|/bin/chgrp -R grp-* /work/
|/bin/chmod [0-9]* /work/"

expect fail "user with no grant at all" <<< 'User agent-max is not allowed to run sudo on example-node.'

# ---- 4. B01 check 22 is wired to it --------------------------------------------

B01="$WORK/B01-core.sh"
if grep -qE '^[[:space:]]+sysadmin_sudo_scope_ok[[:space:]]*$' "$B01" \
        && grep -q "verify_check \"grp-role-sysadmin members' effective sudo = SN_OP only\"" "$B01" \
        && grep -qE 'getent group grp-role-sysadmin' "$B01" \
        && grep -q 'smart-node-agents' "$B01" \
        && grep -qE 'sudo -n -ll -U "\$m"' "$B01" \
        && grep -q 'sudo_ll_within' "$B01" \
        && grep -q 'sudoers_alias_commands "\$canon" SN_OP' "$B01"; then
    pass "4: B01 check 22 walks grp-role-sysadmin and compares each member's sudo -ll against the SN_OP canon"
else
    fail "4: B01 check 22 does not call sysadmin_sudo_scope_ok / walk the group / compare via sudo_ll_within against SN_OP"
fi
if grep -qE 'grep -q "SN_(WORK_PERM|WORK_GROUP|OP)"[[:space:]]+<<<' "$B01"; then
    fail "4: B01 still greps sudo output for an alias name, which sudo never prints"
else
    pass "4: B01 no longer greps sudo output for alias names"
fi
if grep -qE 'sudoers_alias_commands[^\n]+SN_WORK_(PERM|GROUP)' "$B01"; then
    fail "4: B01 still names SN_WORK_PERM / SN_WORK_GROUP as the canon"
else
    pass "4: B01 canon is SN_OP only; SN_WORK_PERM / SN_WORK_GROUP no longer named as canon"
fi

echo "sysadmin-sudo-scope: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
