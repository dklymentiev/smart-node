#!/usr/bin/env bash
#
# permissions-sn-op.test.sh -- source-level gates for the validated wrapper.
#
# The wrapper /usr/local/sbin/sn-op replaces the fnmatch(3)-unsafe glob-based
# Cmnd_Aliases from the old sudoers policy with a single validating exec. This test
# runs the wrapper in SNOP_DRY_RUN=1 mode so no root, no live core-api and no
# real sudoers state are required to prove the argument validation.
#
# Cases E1-E11 exercise escapes allowed by the old glob shape. E12-E17 cover
# the Tier A/B gates. E18 is the
# one-shot-consumption dry-run assertion (grep for the code path plus a
# dry-run acceptance with SNOP_DRY_RUN_APPROVAL_OK=1; the full replay-refuse
# needs a live core-api and is deferred to the stand integration).
#
# Runs against the working tree (default) or against a git ref (--baseline).
# The baseline mode can demonstrate failure on an older glob-based sudoers
# grant, where path traversal and direct chmod through a glob were accepted.
#
# Usage:
#   tests/permissions-sn-op.test.sh                  # working tree
#   tests/permissions-sn-op.test.sh --baseline REF   # inspect an older ref
#
# Exit: 0 all pass, 1 any fail, 2 setup error.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---- resolve source-of-truth files (working tree or baseline ref) ----------

fetch() {
    local rel="$1" dst="$2"
    if [[ -n "$BASELINE" ]]; then
        git -C "$ROOT" show "$BASELINE:$rel" > "$dst" 2>/dev/null \
            || { echo "  [setup] cannot read $rel at $BASELINE" >&2; return 1; }
    else
        [[ -r "$ROOT/$rel" ]] \
            || { echo "  [setup] missing $rel in working tree" >&2; return 1; }
        cp "$ROOT/$rel" "$dst"
    fi
    return 0
}

SNOP="$WORK/sn-op"
SUDOERS="$WORK/agent-max.sudoers"
B01="$WORK/B01-core.sh"

SNOP_PRESENT=1
if ! fetch provision/sbin/sn-op "$SNOP"; then
    SNOP_PRESENT=0
fi
chmod +x "$SNOP" 2>/dev/null || true

SUDOERS_PRESENT=1
if ! fetch provision/sudoers/agent-max.sudoers "$SUDOERS"; then
    SUDOERS_PRESENT=0
fi

fetch bootstrap/B01-core.sh "$B01" || exit 2

WHAT="${BASELINE:-working tree}"
echo "permissions-sn-op ($WHAT)"

PASS=0; FAIL=0; CANNOT=0
pass() { PASS=$((PASS+1)); echo "  ok   $*"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
# A case whose fixtures this machine cannot stage did not run: not a pass, not a failure.
cannot() { CANNOT=$((CANNOT+1)); echo "  CANNOT RUN $*"; }

# Helper: invoke sn-op in dry-run and return "ACCEPT"/"REFUSE:<name>".
snop_dry() {
    if (( SNOP_PRESENT == 0 )); then
        echo "REFUSE:SNOP_ABSENT"
        return
    fi
    local out rc
    out="$(SNOP_DRY_RUN=1 bash "$SNOP" "$@" 2>&1)"
    rc=$?
    if (( rc == 0 )) && grep -q '^would-exec:' <<< "$out"; then
        echo "ACCEPT"
    else
        # Named errors come out as "[sn-op] SN_OP_...: msg". Pull the first
        # such name for the caller to grep against.
        local name
        name="$(grep -oE 'SN_OP_[A-Z_]+' <<< "$out" | head -n1)"
        [[ -z "$name" ]] && name="UNKNOWN"
        echo "REFUSE:$name"
    fi
}

# Helper: same but permitting a passing approval in dry-run.
snop_dry_approve_ok() {
    if (( SNOP_PRESENT == 0 )); then
        echo "REFUSE:SNOP_ABSENT"
        return
    fi
    local out rc
    out="$(SNOP_DRY_RUN=1 SNOP_DRY_RUN_APPROVAL_OK=1 bash "$SNOP" "$@" 2>&1)"
    rc=$?
    if (( rc == 0 )) && grep -q '^would-exec:' <<< "$out"; then
        echo "ACCEPT"
    else
        local name
        name="$(grep -oE 'SN_OP_[A-Z_]+' <<< "$out" | head -n1)"
        [[ -z "$name" ]] && name="UNKNOWN"
        echo "REFUSE:$name"
    fi
}

# ---- E1: path traversal via /work/.. --------------------------------------

r="$(snop_dry chmod 4755 /work/../usr/bin/bash)"
if [[ "$r" == REFUSE:* ]]; then
    pass "E1: sn-op chmod 4755 /work/../usr/bin/bash -> refused ($r)"
else
    fail "E1: expected refusal, got: $r"
fi

# ---- E2: path traversal with -R chgrp --------------------------------------

r="$(snop_dry chgrp -R grp-role-sysadmin /work/../etc)"
if [[ "$r" == REFUSE:* ]]; then
    pass "E2: sn-op chgrp -R grp-role-sysadmin /work/../etc -> refused ($r)"
else
    fail "E2: expected refusal, got: $r"
fi

# ---- E3: setfacl on /etc (outside /work) ----------------------------------

r="$(snop_dry setfacl -m u:agent-sysadmin:rw /etc/sudoers)"
if [[ "$r" == REFUSE:* ]]; then
    pass "E3: sn-op setfacl on /etc/sudoers -> refused ($r)"
else
    fail "E3: expected refusal, got: $r"
fi

# ---- E4: argument splitting (space in path) -------------------------------

r="$(snop_dry chmod 4755 "/work/x /etc/passwd")"
if [[ "$r" == REFUSE:* ]]; then
    pass "E4: sn-op chmod 4755 with space-injected path -> refused ($r)"
else
    fail "E4: expected refusal, got: $r"
fi

# ---- E5: direct sudo /bin/chmod is not in sudoers scope --------------------
#
# Source-level: prove /etc/sudoers.d/agent-max does NOT name any binary
# other than /usr/local/sbin/sn-op. The current form has exactly
# one Cmnd_Alias (SN_OP) with one path. In the old form the
# sudoers names /bin/chgrp, /bin/chmod, /usr/bin/setfacl, /usr/sbin/groupadd,
# /usr/sbin/groupmod, /usr/bin/gpasswd -- test is expected to FAIL there,
# which this assertion rejects.

if (( SUDOERS_PRESENT == 0 )); then
    fail "E5: provision/sudoers/agent-max.sudoers absent"
else
    # every absolute path in the sudoers file, minus comments
    paths_in_sudoers="$(grep -Ev '^\s*(#|$)' "$SUDOERS" | grep -oE '/[A-Za-z][A-Za-z0-9_./-]*' | sort -u || true)"
    # allowed paths in the SN_OP sudoers form
    allowed_re='^/usr/local/sbin/sn-op$'
    banned=""
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        if ! [[ "$p" =~ $allowed_re ]]; then
            banned="$banned $p"
        fi
    done <<< "$paths_in_sudoers"
    if [[ -z "$banned" ]]; then
        pass "E5: sudoers scope is /usr/local/sbin/sn-op only (direct /bin/chmod refused)"
    else
        fail "E5: sudoers names paths outside SN_OP scope:$banned"
    fi
fi

# ---- E6: symlink escape ----------------------------------------------------
#
# Construct a real symlink under /work whose target is /etc, then invoke
# sn-op on a path THROUGH that link. realpath -m follows the link and lands
# outside /work, so the wrapper must refuse. This tests the "realpath -m
# resolves inside /work" gate and the "if realpath -e disagrees, refuse"
# gate. Uses $ROOT (which is under /work/projects/) so the fixture lives
# entirely inside our tree.

E6_LINK="$ROOT/tests/.tmp_e6_symlink_$$"
ln -sf /etc "$E6_LINK" 2>/dev/null || true
if [[ ! -L "$E6_LINK" ]]; then
    fail "E6: could not create symlink fixture at $E6_LINK"
else
    r="$(snop_dry chmod 755 "$E6_LINK/passwd")"
    if [[ "$r" == REFUSE:* ]]; then
        pass "E6: sn-op chmod through /work-local symlink to /etc -> refused ($r)"
    else
        fail "E6: expected refusal, got: $r"
    fi
    rm -f "$E6_LINK"
fi

# ---- E7: extra positional path (variable trailing paths) -------------------

r="$(snop_dry chgrp grp-x /work/y /work/z)"
if [[ "$r" == REFUSE:* ]]; then
    pass "E7: sn-op chgrp with two trailing paths -> refused ($r)"
else
    fail "E7: expected refusal, got: $r"
fi

# ---- E8: group name outside grp-* prefix ----------------------------------

r="$(snop_dry groupadd rootgroup)"
if [[ "$r" == "REFUSE:SN_OP_ARG_GROUP" ]]; then
    pass "E8: sn-op groupadd rootgroup -> refused (SN_OP_ARG_GROUP)"
else
    fail "E8: expected REFUSE:SN_OP_ARG_GROUP, got: $r"
fi

# ---- E9: user outside agent-*|admin ---------------------------------------

r="$(snop_dry gpasswd-add root grp-role-sysadmin)"
if [[ "$r" == "REFUSE:SN_OP_ARG_USER" ]]; then
    pass "E9: sn-op gpasswd-add root ... -> refused (SN_OP_ARG_USER)"
else
    fail "E9: expected REFUSE:SN_OP_ARG_USER, got: $r"
fi

# ---- E10: chgrp on /work itself (accepted) ---------------------------------

r="$(snop_dry chgrp grp-role-sysadmin /work)"
if [[ "$r" == "ACCEPT" ]]; then
    pass "E10: sn-op chgrp grp-role-sysadmin /work -> accepted"
else
    fail "E10: expected ACCEPT, got: $r"
fi

# ---- E11: chgrp on /work/downloads (accepted) ------------------------------

r="$(snop_dry chgrp grp-role-sysadmin /work/downloads)"
if [[ "$r" == "ACCEPT" ]]; then
    pass "E11: sn-op chgrp grp-role-sysadmin /work/downloads -> accepted"
else
    fail "E11: expected ACCEPT, got: $r"
fi

# ---- E12: unit not smart-node-* -------------------------------------------

r="$(snop_dry service-restart authum)"
if [[ "$r" == "REFUSE:SN_OP_ARG_UNIT_SHAPE" ]]; then
    pass "E12: sn-op service-restart authum -> refused (SN_OP_ARG_UNIT_SHAPE)"
else
    fail "E12: expected REFUSE:SN_OP_ARG_UNIT_SHAPE, got: $r"
fi

# ---- E13: container not in prefix set -------------------------------------

r="$(snop_dry container-restart traefik)"
if [[ "$r" == "REFUSE:SN_OP_ARG_CONTAINER_SHAPE" ]]; then
    pass "E13: sn-op container-restart traefik -> refused (SN_OP_ARG_CONTAINER_SHAPE)"
else
    fail "E13: expected REFUSE:SN_OP_ARG_CONTAINER_SHAPE, got: $r"
fi

# ---- E14: negative journal-tail lines --------------------------------------

r="$(snop_dry journal-tail smart-node-core -100)"
if [[ "$r" == "REFUSE:SN_OP_ARG_LINES" ]]; then
    pass "E14: sn-op journal-tail smart-node-core -100 -> refused (SN_OP_ARG_LINES)"
else
    fail "E14: expected REFUSE:SN_OP_ARG_LINES, got: $r"
fi

# ---- E15: mv destination outside /work -------------------------------------

r="$(snop_dry mv /work/x /etc/y)"
if [[ "$r" == "REFUSE:SN_OP_PATH_OUTSIDE_WORK" ]]; then
    pass "E15: sn-op mv /work/x /etc/y -> refused (SN_OP_PATH_OUTSIDE_WORK on dst)"
else
    fail "E15: expected REFUSE:SN_OP_PATH_OUTSIDE_WORK, got: $r"
fi

# ---- E16: rm without --approval (Tier B) -----------------------------------

r="$(snop_dry rm /work/_tmp/scratch)"
if [[ "$r" == "REFUSE:SN_OP_APPROVAL_REQUIRED" ]]; then
    pass "E16: sn-op rm /work/_tmp/scratch -> refused (SN_OP_APPROVAL_REQUIRED)"
else
    fail "E16: expected REFUSE:SN_OP_APPROVAL_REQUIRED, got: $r"
fi

# ---- E17: Tier B with an approval that does not pass the check -------------
#
# Dry-run without SNOP_DRY_RUN_APPROVAL_OK refuses at the approval gate
# (SN_OP_APPROVAL_DRY_RUN). This proves the code path is reached under the
# task_id shape and rejects when the live check would have said "not in
# approved state". The live-check variant is exercised on the stand.

r="$(snop_dry service-stop smart-node-voice --approval 9999)"
if [[ "$r" == REFUSE:SN_OP_APPROVAL_* ]]; then
    pass "E17: sn-op service-stop smart-node-voice --approval 9999 -> refused ($r)"
else
    fail "E17: expected REFUSE:SN_OP_APPROVAL_*, got: $r"
fi

# ---- E18: Tier B under a passing approval, one-shot code path present -----
#
# With SNOP_DRY_RUN_APPROVAL_OK=1 the approval gate accepts, so the wrapper
# reaches the exec step (ACCEPT). The one-shot-consumption check itself is
# implemented in the wrapper (grep for the events query and the "consumed"
# refusal). Full replay-refuse is stand-only because it needs an actual
# sn_core.events row from the first exec.

r="$(snop_dry_approve_ok service-stop smart-node-voice --approval 42)"
if [[ "$r" == "ACCEPT" ]]; then
    # Now confirm the one-shot check code exists in the wrapper source.
    if grep -q 'SN_OP_APPROVAL_CONSUMED' "$SNOP" \
       && grep -q 'exec_ok\|exec_error' "$SNOP"; then
        pass "E18: sn-op service-stop under passing approval -> accepted; one-shot code path present"
    else
        fail "E18: accepted but one-shot consumption code missing in wrapper"
    fi
else
    fail "E18: expected ACCEPT under SNOP_DRY_RUN_APPROVAL_OK=1, got: $r"
fi

# ---- Assertion 1: sudo -l shape ---------------------------------------------
#
# The sudoers file (source of truth) shows exactly ONE Cmnd_Alias line, one
# grant to the role group %grp-role-sysadmin, and no SN_WORK_PERM /
# SN_WORK_GROUP / (ALL) NOPASSWD: ALL. This is how sudo will see it after
# visudo -c. Subject moved from single user `agent-sysadmin` to
# %grp-role-sysadmin so the grant survives user renames and supports another
# sysadmin without a sudoers edit.

if (( SUDOERS_PRESENT == 1 )); then
    body="$(grep -Ev '^\s*(#|$)' "$SUDOERS" || true)"
    if grep -q 'Cmnd_Alias SN_OP' <<< "$body" \
       && grep -q '%grp-role-sysadmin ALL=(root) NOPASSWD: SN_OP' <<< "$body" \
       && ! grep -qE '\bagent-sysadmin\b[[:space:]]+ALL=' <<< "$body" \
       && ! grep -qE 'SN_WORK_PERM|SN_WORK_GROUP' <<< "$body" \
       && ! grep -qE '\(ALL(:[[:space:]]*ALL)?\)[[:space:]]+NOPASSWD:[[:space:]]*ALL' <<< "$body"; then
        pass "assertion 1: sudoers names only SN_OP granted to %grp-role-sysadmin (no user subject, no legacy aliases, no broad ALL)"
    else
        fail "assertion 1: sudoers file must grant only SN_OP to %grp-role-sysadmin"
    fi
fi

# ---- Assertion 2: sudoers passes visudo -cf --------------------------------

if (( SUDOERS_PRESENT == 1 )); then
    if command -v visudo >/dev/null 2>&1; then
        if visudo -cf "$SUDOERS" >/dev/null 2>&1; then
            pass "assertion 2: sudoers passes visudo -cf"
        else
            fail "assertion 2: sudoers refused by visudo -cf"
        fi
    else
        fail "assertion 2: visudo not available (required to validate sudoers)"
    fi
fi

# ---- Assertion 3: B01 installs sn-op and the sudoers, verify checks updated

if grep -q 'install_sn_op' "$B01" \
   && grep -q '/usr/local/sbin/sn-op' "$B01" \
   && grep -q 'install_agent_max_sudoers' "$B01" \
   && grep -q '/etc/sudoers.d/agent-max' "$B01" \
   && grep -q 'sn-op wrapper installed' "$B01"; then
    pass "assertion 3: B01 installs sn-op + agent-max.sudoers and asserts them in --verify"
else
    fail "assertion 3: B01 missing install_sn_op or the verify check for sn-op"
fi

# ---- Assertion 4: B01 --verify refuses the superseded aliases --------------
#
# The SN_OP scope check is done by sysadmin_sudo_scope_ok,
# which walks members of grp-role-sysadmin and pipes each member's `sudo -ll`
# through sudo_ll_within against the SN_OP alias of the shipped sidecar. That
# comparison IS the refusal of any legacy SN_WORK_PERM / SN_WORK_GROUP grant:
# neither alias appears in the SN_OP canon, so a sudo -ll line naming a legacy
# alias command is not in `allowed` and gets refused by sudo_ll_within. The
# assertion here checks the wiring is intact and does not silently regress to
# a per-file grep.

if ! grep -q 'SN_OP' "$B01"; then
    fail "assertion 4: B01 --verify has no SN_OP scope check"
elif grep -qE 'sudoers_alias_commands "\$canon" SN_OP' "$B01" \
     && grep -qE 'sudo -n -ll -U "\$m"' "$B01" \
     && grep -q 'sudo_ll_within' "$B01" \
     && ! grep -qE 'sudoers_alias_commands[^\n]+SN_WORK_(PERM|GROUP)' "$B01"; then
    pass "assertion 4: B01 --verify refuses legacy SN_WORK_PERM/SN_WORK_GROUP grants via sudo_ll_within against the SN_OP canon"
else
    fail "assertion 4: B01 --verify SN_OP scope check missing sudo_ll_within comparison or still names legacy aliases as canon"
fi

# ---- Assertion 5: wrapper emits events + fallback path ---------------------

if (( SNOP_PRESENT == 1 )); then
    if grep -q '/run/smart-node/api/core-api.sock' "$SNOP" \
       && grep -q '/var/log/sn-op-fallback.jsonl' "$SNOP" \
       && grep -q 'FAIL-LOUD\|SN_OP_EMIT_FAIL' "$SNOP"; then
        pass "assertion 5: wrapper emits sn_core.events via core-api socket + JSONL fallback with FAIL-LOUD"
    else
        fail "assertion 5: wrapper missing event emit + fallback + FAIL-LOUD"
    fi
fi

# ---- Assertion 6: wrapper approval-check code path is present --------------

if (( SNOP_PRESENT == 1 )); then
    if grep -q 'verify_approval' "$SNOP" \
       && grep -q 'SN_OP_APPROVAL_STALE' "$SNOP" \
       && grep -q 'SN_OP_APPROVAL_CONSUMED' "$SNOP" \
       && grep -q 'SN_OP_APPROVAL_ARGV_MISMATCH' "$SNOP" \
       && grep -q 'FRESHNESS_SECONDS' "$SNOP"; then
        pass "assertion 6: wrapper approval-check enforces state, freshness, argv-match, one-shot"
    else
        fail "assertion 6: wrapper missing one or more approval-check gates"
    fi
fi

# ---- Assertion 7: no sh -c, no $*, no variable trailing path in exec calls -
#
# Grep the wrapper source EXCLUDING comment lines: the design docstring
# legitimately names the forbidden constructs (Never sh -c, never $*) and
# would otherwise self-match.

if (( SNOP_PRESENT == 1 )); then
    non_comments="$(grep -Ev '^\s*#' "$SNOP" || true)"
    if grep -qE '\bsh -c\b|\$\*' <<< "$non_comments"; then
        fail "assertion 7: wrapper uses forbidden 'sh -c' or '\$*' construct in code"
    else
        pass "assertion 7: wrapper does not use sh -c or \$* in code (quoted -- args only)"
    fi
fi

# ---- Assertion 8: B01 removes /etc/sudoers.d/smart-node-sysadmin -----------
#
# The old /etc/sudoers.d/smart-node-sysadmin file granted
# agent-sysadmin ALL=(ALL) NOPASSWD: ALL. It must not survive alongside SN_OP:
# sudo combines grants, so the old file would restore unrestricted access.
#
# Source-level: B01 has a removal step (remove_superseded_sysadmin_sudoers or
# equivalent) AND a verify_check asserting absence.

if grep -q 'remove_superseded_sysadmin_sudoers\|rm.*smart-node-sysadmin' "$B01" \
   && grep -q 'superseded smart-node-sysadmin sudoers file absent' "$B01" \
   && grep -q '/etc/sudoers.d/smart-node-sysadmin' "$B01"; then
    pass "assertion 8: B01 removes and verify-checks /etc/sudoers.d/smart-node-sysadmin (migration)"
else
    fail "assertion 8: B01 missing removal step OR verify-check for /etc/sudoers.d/smart-node-sysadmin"
fi

# ---- Assertion 9: B01 removes /etc/sudoers.d/owner-agent-max-temporary -----
#
# The temporary bridge grant at owner-agent-max-temporary bypasses the
# assertion-8 removal
# above. Once the SN_OP scoped grant is in place, the temporary file is
# strictly widening and must be removed on the same install pass.
#
# Source-level: B01 has a removal step (remove_temporary_owner_agent_max_sudoers
# or an equivalent rm) AND a verify_check asserting absence.

if grep -q 'remove_temporary_owner_agent_max_sudoers\|rm.*owner-agent-max-temporary' "$B01" \
   && grep -q 'temporary owner-agent-max-temporary sudoers file absent' "$B01" \
   && grep -q '/etc/sudoers.d/owner-agent-max-temporary' "$B01"; then
    pass "assertion 9: B01 removes and verify-checks /etc/sudoers.d/owner-agent-max-temporary (temporary bridge)"
else
    fail "assertion 9: B01 missing removal step OR verify-check for /etc/sudoers.d/owner-agent-max-temporary"
fi

# ---- E19: args_json carries the argv ---------------------------------------
#
# Found on the stand run: `local -n arr="$1"` called with a caller array also
# named `arr` is a circular nameref, every event recorded args [] and the
# approval argv check compared against "". Run the real function on a real
# array named `arr`, the way every verb calls it.

if (( SNOP_PRESENT == 1 )); then
    fn="$(sed -n '/^_args_json() {/,/^}/p' "$SNOP")"
    r="$(bash -c "$fn"'
arr=(smart-node-voice "a b")
_args_json arr' 2>&1)"
    if [[ "$r" == '["smart-node-voice","a b"]' ]]; then
        pass "E19: _args_json on a caller array named arr -> $r"
    else
        fail "E19: _args_json lost the argv (circular nameref?), got: $r"
    fi
else
    fail "E19: sn-op absent"
fi

# ---- E20: approval checks fail closed ---------------------------------------
#
# Found on the stand run: the events read answered 403 {"detail": ...}, which
# is valid JSON, `.events[]?` counted zero and the same approval ran twice.
# The consumption check must accept only an events array, and an empty argv
# must never be taken as bound to a task.

if (( SNOP_PRESENT == 1 ))    && grep -q "jq -e '.events | type == \"array\"'" "$SNOP"    && grep -q 'empty argv cannot be bound to an approval' "$SNOP"; then
    pass "E20: consumption check requires an events array; empty argv refused"
else
    fail "E20: consumption check accepts any JSON body, or empty argv is not refused"
fi

# ---- E21: the approval is the owner's approve, read from approved_at --------
#
# A real approval could never pass the old check. sn-op wanted
# previous_status == awaiting_approval, which POST /tasks/{id}/approve does not
# write, and a fresh comment authored by 'admin'. Only the owner can approve,
# and approve alone stamps approved_at, so in_progress + a fresh approved_at is
# the owner's approval. This runs the real wrapper (not dry-run) with a stand-in
# curl that answers the planner and the events endpoint the way core-api does,
# and a unit name that does not exist, so nothing on the host is touched.
# "Accepted" is the wrapper recording its accepted event, i.e. every approval
# check passed.

if (( SNOP_PRESENT == 1 )); then
    FAKE="$WORK/fakebin"; mkdir -p "$FAKE"
    cat > "$FAKE/curl" <<'CURL'
#!/usr/bin/env bash
# Stand-in for curl over the core-api socket: planner task, its comments, the
# events read, and event POSTs (logged, answered 201).
url=""; post=0; body=""
while (( $# )); do
    case "$1" in
        -X) [[ "$2" == POST ]] && post=1; shift ;;
        -d) body="$2"; shift ;;
        http*) url="$1" ;;
    esac
    shift
done
if (( post )); then printf '%s\n' "$body" >> "$E21_DIR/posted"; printf '201'; exit 0; fi
case "$url" in
    */comments) printf '[]' ;;
    */api/planner/api/v1/tasks/*) cat "$E21_DIR/task.json" ;;
    */api/events*) printf '{"events": []}' ;;
    *) exit 7 ;;
esac
CURL
    chmod +x "$FAKE/curl"
    e21() {  # $1 = approved_at (ISO or empty) -> ACCEPT | REFUSE:<name>
        local d="$WORK/e21.$RANDOM"; mkdir -p "$d"
        jq -n --arg a "$1" '{id: 77, status: "in_progress", previous_status: null,
                             approved_at: (if $a == "" then null else $a end),
                             updated_at: (now | todate),
                             description: "sn-op service-stop smart-node-e21-no-such-unit"}' \
            > "$d/task.json"
        local out
        out="$(E21_DIR="$d" PATH="$FAKE:$PATH" SNOP_DRY_RUN=0 \
               bash "$SNOP" service-stop smart-node-e21-no-such-unit --approval 77 2>&1)"
        if grep -q '"outcome":"accepted"' "$d/posted" 2>/dev/null; then
            echo ACCEPT
        else
            local name; name="$(grep -oE 'SN_OP_[A-Z_]+' <<< "$out" | head -n1)"
            echo "REFUSE:${name:-UNKNOWN}"
        fi
    }
    r="$(e21 "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    [[ "$r" == ACCEPT ]] \
        && pass "E21a: owner approval (in_progress, fresh approved_at, no previous_status, no admin comment) -> accepted" \
        || fail "E21a: a real owner approval must pass, got: $r"
    r="$(e21 "")"
    [[ "$r" == REFUSE:SN_OP_APPROVAL_STATE ]] \
        && pass "E21b: in_progress without approved_at -> refused ($r)" \
        || fail "E21b: expected REFUSE:SN_OP_APPROVAL_STATE, got: $r"
    r="$(e21 "$(date -u -d '-10 minutes' +%Y-%m-%dT%H:%M:%SZ)")"
    [[ "$r" == REFUSE:SN_OP_APPROVAL_STALE ]] \
        && pass "E21c: approved_at older than the window -> refused ($r)" \
        || fail "E21c: expected REFUSE:SN_OP_APPROVAL_STALE, got: $r"
else
    fail "E21: sn-op absent"
fi

# ---- E22: unit-install (Tier B) -- a unit file is code run as root ----------
#
# The boundary is the CONTENT of the file, not its name: a smart-node-* service
# with no User= runs as root. So the verb binds the file to its sha256 (the sha
# rides in argv, which the approval check already binds) and refuses a service
# that would run as root.

# validate_path only admits paths under /work, so the fixture files have to live
# there. The checkout is under /work on the node; anywhere else the cases cannot
# run and say so instead of passing.
E22W="$ROOT/.e22-fixtures-$$"
E22U="$WORK/e22-unitdir"; mkdir -p "$E22U"
export SNOP_DRY_RUN_UNIT_DIR="$E22U"
E22_OK=0
if [[ "$E22W" == /work/* ]] && mkdir -p "$E22W" 2>/dev/null; then
    trap 'rm -rf "$WORK" "$E22W"' EXIT
    E22_OK=1
fi

if (( E22_OK == 1 )); then
    printf '[Unit]\nDescription=t\n[Timer]\nOnBootSec=60s\nOnUnitActiveSec=5min\n[Install]\nWantedBy=timers.target\n' > "$E22W/smart-node-e22.timer"
    printf '[Unit]\nDescription=t\n[Service]\nType=oneshot\nUser=agent-max\nExecStart=/usr/bin/true\n' > "$E22W/smart-node-e22.service"
    printf '[Service]\nType=oneshot\nExecStart=/usr/bin/true\n' > "$E22W/smart-node-e22-nouser.service"
    printf '[Service]\nUser=root\nExecStart=/usr/bin/true\n' > "$E22W/smart-node-e22-root.service"
    printf '[Service]\nUser=agent-max\nExecStart=+/usr/bin/true\n' > "$E22W/smart-node-e22-plus.service"
    printf '[Service]\nUser=agent-max\nExecStart=/usr/bin/true\nExecStartPre=!/usr/bin/true\n' > "$E22W/smart-node-e22-bang.service"
    sha() { sha256sum "$1" | awk '{print $1}'; }
    T="$E22W/smart-node-e22.timer"; S="$E22W/smart-node-e22.service"

    r="$(snop_dry_approve_ok unit-install smart-node-e22.timer "$T" "$(sha "$T")" --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E22a: valid smart-node-*.timer with matching sha -> accepted" || fail "E22a: expected ACCEPT, got: $r"
    r="$(snop_dry_approve_ok unit-install smart-node-e22.service "$S" "$(sha "$S")" --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E22b: valid service with User=agent-max -> accepted" || fail "E22b: expected ACCEPT, got: $r"
    r="$(snop_dry unit-install smart-node-e22.timer "$T" "$(sha "$T")")"
    [[ "$r" == REFUSE:SN_OP_APPROVAL_REQUIRED ]] && pass "E22c: no --approval -> refused ($r)" || fail "E22c: expected REFUSE:SN_OP_APPROVAL_REQUIRED, got: $r"
    r="$(snop_dry_approve_ok unit-install smart-node-e22.timer "$T" 0000000000000000000000000000000000000000000000000000000000000000 --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_SHA_MISMATCH ]] && pass "E22d: sha mismatch -> refused ($r)" || fail "E22d: expected REFUSE:SN_OP_UNIT_SHA_MISMATCH, got: $r"
    r="$(snop_dry_approve_ok unit-install sn-event-monitor.timer "$T" "$(sha "$T")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_ARG_UNIT_SHAPE ]] && pass "E22e: unit not smart-node-* -> refused ($r)" || fail "E22e: expected REFUSE:SN_OP_ARG_UNIT_SHAPE, got: $r"
    r="$(snop_dry_approve_ok unit-install smart-node-e22.path "$T" "$(sha "$T")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_SUFFIX ]] && pass "E22f: suffix other than .service/.timer -> refused ($r)" || fail "E22f: expected REFUSE:SN_OP_UNIT_SUFFIX, got: $r"
    r="$(snop_dry_approve_ok unit-install smart-node-e22.timer /etc/passwd "$(sha /etc/passwd)" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_PATH_OUTSIDE_WORK ]] && pass "E22g: source outside /work -> refused ($r)" || fail "E22g: expected REFUSE:SN_OP_PATH_OUTSIDE_WORK, got: $r"
    r="$(snop_dry_approve_ok unit-install smart-node-e22.timer "$E22W/../../etc/passwd" 0000000000000000000000000000000000000000000000000000000000000000 --approval 77)"
    [[ "$r" == REFUSE:SN_OP_PATH_TRAVERSAL ]] && pass "E22h: .. in source -> refused ($r)" || fail "E22h: expected REFUSE:SN_OP_PATH_TRAVERSAL, got: $r"
    for pair in "nouser:smart-node-e22-nouser.service" "root:smart-node-e22-root.service" "plus:smart-node-e22-plus.service" "bang:smart-node-e22-bang.service"; do
        tag="${pair%%:*}"; f="$E22W/${pair#*:}"
        r="$(snop_dry_approve_ok unit-install "${pair#*:}" "$f" "$(sha "$f")" --approval 77)"
        [[ "$r" == REFUSE:SN_OP_UNIT_RUNS_AS_ROOT ]] && pass "E22i-$tag: service that would run as root -> refused ($r)" || fail "E22i-$tag: expected REFUSE:SN_OP_UNIT_RUNS_AS_ROOT, got: $r"
    done
    r="$(snop_dry_approve_ok unit-install smart-node-e22.timer "$T" "$(sha "$T")" --approval 77 extra)"
    [[ "$r" == REFUSE:SN_OP_ARG_COUNT ]] && pass "E22j: extra argument -> refused ($r)" || fail "E22j: expected REFUSE:SN_OP_ARG_COUNT, got: $r"
else
    cannot "E22: the checkout is not under /work, so unit-install fixtures cannot be staged (run the test from a /work checkout)"
fi

# ---- E23: replacing units, hidden prefixes, timers, unit-enable -------------

if (( E22_OK == 1 )); then
    # A unit that is already there and is not ours (a shipped one) is never replaced.
    printf '[Service]\nUser=agent-max\nExecStart=/usr/bin/true\n' > "$E22W/smart-node-e23-shipped.service"
    printf '[Service]\nUser=smart-node-core\nExecStart=/bin/false\n' > "$E22U/smart-node-e23-shipped.service"
    r="$(snop_dry_approve_ok unit-install smart-node-e23-shipped.service "$E22W/smart-node-e23-shipped.service" "$(sha "$E22W/smart-node-e23-shipped.service")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_EXISTS ]] && pass "E23a: existing unit without our marker -> not replaced ($r)" || fail "E23a: expected REFUSE:SN_OP_UNIT_EXISTS, got: $r"

    # One we installed earlier may be replaced (update in place).
    printf '# installed-by: sn-op unit-install\n[Service]\nUser=agent-max\nExecStart=/bin/false\n' > "$E22U/smart-node-e23-ours.service"
    printf '[Service]\nUser=agent-max\nExecStart=/usr/bin/true\n' > "$E22W/smart-node-e23-ours.service"
    r="$(snop_dry_approve_ok unit-install smart-node-e23-ours.service "$E22W/smart-node-e23-ours.service" "$(sha "$E22W/smart-node-e23-ours.service")" --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E23b: a unit carrying our marker may be replaced" || fail "E23b: expected ACCEPT, got: $r"

    # Prefix combinations and line continuation must not hide a root command.
    printf '[Service]\nUser=agent-max\nExecStart=-+/usr/bin/true\n' > "$E22W/smart-node-e23-combo.service"
    r="$(snop_dry_approve_ok unit-install smart-node-e23-combo.service "$E22W/smart-node-e23-combo.service" "$(sha "$E22W/smart-node-e23-combo.service")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_RUNS_AS_ROOT ]] && pass "E23c: ExecStart=-+... (combined prefixes) -> refused ($r)" || fail "E23c: expected REFUSE:SN_OP_UNIT_RUNS_AS_ROOT, got: $r"
    printf '[Service]\nUser=agent-max\nExecStart=\\\n+/usr/bin/true\n' > "$E22W/smart-node-e23-cont.service"
    r="$(snop_dry_approve_ok unit-install smart-node-e23-cont.service "$E22W/smart-node-e23-cont.service" "$(sha "$E22W/smart-node-e23-cont.service")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_CONTINUATION ]] && pass "E23d: trailing backslash (hides a + prefix) -> refused ($r)" || fail "E23d: expected REFUSE:SN_OP_UNIT_CONTINUATION, got: $r"
    printf '[Service]\nUser=agent-max\nUser=root\nExecStart=/usr/bin/true\n' > "$E22W/smart-node-e23-twouser.service"
    r="$(snop_dry_approve_ok unit-install smart-node-e23-twouser.service "$E22W/smart-node-e23-twouser.service" "$(sha "$E22W/smart-node-e23-twouser.service")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_RUNS_AS_ROOT ]] && pass "E23e: a second User=root after a good one -> refused ($r)" || fail "E23e: expected REFUSE:SN_OP_UNIT_RUNS_AS_ROOT, got: $r"
    printf '[Service]\nUser=agent-max\nAmbientCapabilities=CAP_SYS_ADMIN\nExecStart=/usr/bin/true\n' > "$E22W/smart-node-e23-cap.service"
    r="$(snop_dry_approve_ok unit-install smart-node-e23-cap.service "$E22W/smart-node-e23-cap.service" "$(sha "$E22W/smart-node-e23-cap.service")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_ELEVATES ]] && pass "E23f: AmbientCapabilities= -> refused ($r)" || fail "E23f: expected REFUSE:SN_OP_UNIT_ELEVATES, got: $r"

    # A timer may only start a unit of the allowed shape.
    printf '[Timer]\nOnBootSec=60s\nUnit=ssh.service\n' > "$E22W/smart-node-e23-timer.timer"
    r="$(snop_dry_approve_ok unit-install smart-node-e23-timer.timer "$E22W/smart-node-e23-timer.timer" "$(sha "$E22W/smart-node-e23-timer.timer")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_TIMER_TARGET ]] && pass "E23g: timer Unit=ssh.service -> refused ($r)" || fail "E23g: expected REFUSE:SN_OP_UNIT_TIMER_TARGET, got: $r"

    # unit-enable works only on what unit-install put there.
    r="$(snop_dry unit-enable smart-node-e23-ours.service)"
    [[ "$r" == ACCEPT ]] && pass "E23h: unit-enable on a unit we installed -> accepted" || fail "E23h: expected ACCEPT, got: $r"
    r="$(snop_dry unit-enable smart-node-e23-shipped.service)"
    [[ "$r" == REFUSE:SN_OP_UNIT_NOT_OURS ]] && pass "E23i: unit-enable on a shipped unit -> refused ($r)" || fail "E23i: expected REFUSE:SN_OP_UNIT_NOT_OURS, got: $r"
    r="$(snop_dry unit-enable ssh.service)"
    [[ "$r" == REFUSE:SN_OP_ARG_UNIT_SHAPE ]] && pass "E23j: unit-enable ssh.service -> refused ($r)" || fail "E23j: expected REFUSE:SN_OP_ARG_UNIT_SHAPE, got: $r"
fi

# ---- E24: table verbs (run / verbs) ----------------------------------------
# The operations beyond the fixed verbs live in a root-owned table, so a new one
# is a row and not an edit of the wrapper. The boundary is therefore what a row
# can and cannot say: where its executable sits, who can rewrite it, and how
# each argument is shaped. Fixtures stand in for the root-owned files.

if (( SNOP_PRESENT == 1 )) && grep -q 'verb_run' "$SNOP"; then
    E24W="$WORK/e24"; mkdir -p "$E24W"; chmod 700 "$E24W"
    printf '#!/bin/bash\nexit 0\n' > "$E24W/ok.sh";    chmod 755 "$E24W/ok.sh"
    printf '#!/bin/bash\nexit 0\n' > "$E24W/open.sh";  chmod 777 "$E24W/open.sh"
    ln -s "$E24W/ok.sh" "$E24W/link.sh"
    cat > "$E24W/table.json" <<JSON
{"verbs": {
  "ok":      {"description": "fixture", "exec": ["$E24W/ok.sh"], "args": ["[a-z]+", "[0-9]+"]},
  "bash":    {"exec": ["/usr/bin/bash", "-c"], "args": ["[a-z]+"]},
  "open":    {"exec": ["$E24W/open.sh"]},
  "link":    {"exec": ["$E24W/link.sh"]},
  "dotdot":  {"exec": ["$E24W/../e24/ok.sh"]},
  "relative":{"exec": ["ok.sh"]},
  "gated":   {"tier": "B", "exec": ["$E24W/ok.sh"], "args": ["[a-z]+"]}
}}
JSON
    chmod 644 "$E24W/table.json"
    export SNOP_DRY_RUN_VERB_TABLE="$E24W/table.json"

    r="$(snop_dry run ok alpha 42)"
    [[ "$r" == ACCEPT ]] && pass "E24a: run of a table row with well-shaped arguments -> accepted" || fail "E24a: expected ACCEPT, got: $r"
    r="$(snop_dry run ok -x 42)"
    [[ "$r" == REFUSE:SN_OP_RUN_ARG_OPTION ]] && pass "E24b: an argument that starts with '-' -> refused ($r)" || fail "E24b: expected REFUSE:SN_OP_RUN_ARG_OPTION, got: $r"
    r="$(snop_dry run ok ALPHA 42)"
    [[ "$r" == REFUSE:SN_OP_RUN_ARG_SHAPE ]] && pass "E24c: an argument that misses its regex -> refused ($r)" || fail "E24c: expected REFUSE:SN_OP_RUN_ARG_SHAPE, got: $r"
    r="$(snop_dry run ok alpha)"
    [[ "$r" == REFUSE:SN_OP_ARG_COUNT ]] && pass "E24d: too few arguments -> refused ($r)" || fail "E24d: expected REFUSE:SN_OP_ARG_COUNT, got: $r"
    r="$(snop_dry run nope)"
    [[ "$r" == REFUSE:SN_OP_RUN_UNKNOWN ]] && pass "E24e: a name that is not in the table -> refused ($r)" || fail "E24e: expected REFUSE:SN_OP_RUN_UNKNOWN, got: $r"
    r="$(snop_dry run bash alpha)"
    [[ "$r" == REFUSE:SN_OP_RUN_EXEC ]] && pass "E24f: a row that points at a shell -> refused ($r)" || fail "E24f: expected REFUSE:SN_OP_RUN_EXEC, got: $r"
    r="$(snop_dry run open)"
    [[ "$r" == REFUSE:SN_OP_RUN_EXEC ]] && pass "E24g: an executable others can rewrite -> refused ($r)" || fail "E24g: expected REFUSE:SN_OP_RUN_EXEC, got: $r"
    r="$(snop_dry run link)"
    [[ "$r" == REFUSE:SN_OP_RUN_EXEC ]] && pass "E24h: an executable reached through a symlink -> refused ($r)" || fail "E24h: expected REFUSE:SN_OP_RUN_EXEC, got: $r"
    r="$(snop_dry run dotdot)"
    [[ "$r" == REFUSE:SN_OP_RUN_EXEC ]] && pass "E24i: an executable path with '..' -> refused ($r)" || fail "E24i: expected REFUSE:SN_OP_RUN_EXEC, got: $r"
    r="$(snop_dry run relative)"
    [[ "$r" == REFUSE:SN_OP_RUN_EXEC ]] && pass "E24j: a relative executable path -> refused ($r)" || fail "E24j: expected REFUSE:SN_OP_RUN_EXEC, got: $r"
    r="$(snop_dry run ok 'alpha;id' 42)"
    [[ "$r" == REFUSE:SN_OP_RUN_ARG_SHAPE ]] && pass "E24k: shell syntax in an argument -> refused ($r)" || fail "E24k: expected REFUSE:SN_OP_RUN_ARG_SHAPE, got: $r"
    r="$(snop_dry run ok $'alpha\nx' 42)"
    [[ "$r" == REFUSE:SN_OP_RUN_ARG_SHAPE ]] && pass "E24l: a newline in an argument -> refused ($r)" || fail "E24l: expected REFUSE:SN_OP_RUN_ARG_SHAPE, got: $r"
    r="$(snop_dry run gated alpha)"
    [[ "$r" == REFUSE:SN_OP_APPROVAL_REQUIRED ]] && pass "E24m: a tier B row without --approval -> refused ($r)" || fail "E24m: expected REFUSE:SN_OP_APPROVAL_REQUIRED, got: $r"
    r="$(snop_dry_approve_ok run gated alpha --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E24n: a tier B row with a passing approval -> accepted" || fail "E24n: expected ACCEPT, got: $r"
    r="$(snop_dry run ok alpha 42 --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E24o: --approval on a tier A row changes nothing" || fail "E24o: expected ACCEPT, got: $r"

    # A table somebody else can write is not a table.
    chmod 664 "$E24W/table.json"
    r="$(snop_dry run ok alpha 42)"
    [[ "$r" == REFUSE:SN_OP_RUN_TABLE_UNTRUSTED ]] && pass "E24p: a table writable by group -> refused ($r)" || fail "E24p: expected REFUSE:SN_OP_RUN_TABLE_UNTRUSTED, got: $r"
    chmod 644 "$E24W/table.json"
    chmod 770 "$E24W"
    r="$(snop_dry run ok alpha 42)"
    [[ "$r" == REFUSE:SN_OP_RUN_TABLE_UNTRUSTED ]] && pass "E24q: a table in a group-writable directory -> refused ($r)" || fail "E24q: expected REFUSE:SN_OP_RUN_TABLE_UNTRUSTED, got: $r"
    chmod 700 "$E24W"
    export SNOP_DRY_RUN_VERB_TABLE="$E24W/missing.json"
    r="$(snop_dry run ok alpha 42)"
    [[ "$r" == REFUSE:SN_OP_RUN_NO_TABLE ]] && pass "E24r: no table -> refused ($r)" || fail "E24r: expected REFUSE:SN_OP_RUN_NO_TABLE, got: $r"
    unset SNOP_DRY_RUN_VERB_TABLE
else
    fail "E24: this wrapper has no table verbs (run / verbs)"
fi

# ---- E25: dropin-install ----------------------------------------------------
# A drop-in changes how an existing unit runs, so what it may say is an
# allowlist. The cases below are the escapes a drop-in could be used for.

if (( E22_OK == 1 )) && grep -q 'verb_dropin_install' "$SNOP"; then
    printf '[Service]\nUser=agent-max\nExecStart=/usr/bin/true\n' > "$E22U/smart-node-e25.service"
    d25() { # d25 <file-name> <content>  -> prints the path of a fixture under /work
        printf "$2" > "$E22W/$1"; echo "$E22W/$1"
    }
    D="$(d25 e25-ok.conf '[Service]\nReadWritePaths=/var/lib/smart-node/hitl-caller\n')"
    r="$(snop_dry_approve_ok dropin-install smart-node-e25.service 10-hitl-caller-rw.conf "$D" "$(sha "$D")" --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E25a: a ReadWritePaths= drop-in under /var/lib/smart-node -> accepted" || fail "E25a: expected ACCEPT, got: $r"
    r="$(snop_dry dropin-install smart-node-e25.service 10-hitl-caller-rw.conf "$D" "$(sha "$D")")"
    [[ "$r" == REFUSE:SN_OP_APPROVAL_REQUIRED ]] && pass "E25b: without --approval -> refused ($r)" || fail "E25b: expected REFUSE:SN_OP_APPROVAL_REQUIRED, got: $r"
    r="$(snop_dry_approve_ok dropin-install smart-node-e25.service 10-hitl-caller-rw.conf "$D" "$(printf '0%.0s' {1..64})" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_SHA_MISMATCH ]] && pass "E25c: a file that is not the one approved -> refused ($r)" || fail "E25c: expected REFUSE:SN_OP_UNIT_SHA_MISMATCH, got: $r"

    chk() { # chk <label> <expected-name> <content> [conf-name]
        local f; f="$(d25 "e25-$1.conf" "$3")"
        local got; got="$(snop_dry_approve_ok dropin-install smart-node-e25.service "${4:-10-t.conf}" "$f" "$(sha "$f")" --approval 77)"
        [[ "$got" == "REFUSE:$2" ]] && pass "E25$1: refused ($got)" || fail "E25$1: expected REFUSE:$2, got: $got"
    }
    chk d  SN_OP_DROPIN_PATH   '[Service]\nReadWritePaths=/etc\n'
    chk e  SN_OP_DROPIN_PATH   '[Service]\nReadWritePaths=/var/lib/smart-node/../../etc\n'
    chk f  SN_OP_DROPIN_PATH   '[Service]\nReadWritePaths=/var/lib/smart-node/x /etc/ssh\n'
    chk g  SN_OP_DROPIN_PATH   '[Service]\nReadWritePaths=+/var/lib/smart-node/x\n'
    chk h  SN_OP_DROPIN_PATH   '[Service]\nReadWritePaths=/var/lib/smart-nodex\n'
    chk i  SN_OP_DROPIN_KEY    '[Service]\nExecStart=/bin/sh\n'
    chk j  SN_OP_DROPIN_KEY    '[Service]\nUser=root\n'
    chk k  SN_OP_DROPIN_KEY    '[Service]\nAmbientCapabilities=CAP_SYS_ADMIN\n'
    chk l  SN_OP_DROPIN_KEY    '[Service]\nEnvironment=LD_PRELOAD=/tmp/x.so\n'
    chk m  SN_OP_DROPIN_KEY    '[Service]\nReadWritePaths=\n'
    chk n  SN_OP_DROPIN_SECTION '[Service]\nReadWritePaths=/var/lib/smart-node/x\n[Install]\nWantedBy=multi-user.target\n'
    chk o  SN_OP_DROPIN_SECTION '[Unit]\nDescription=x\n'
    chk p  SN_OP_DROPIN_SECTION 'ReadWritePaths=/var/lib/smart-node/x\n'
    chk q  SN_OP_DROPIN_CONTINUATION '[Service]\nReadWritePaths=/var/lib/smart-node/x \\\n/etc\n'
    chk r  SN_OP_ARG_DROPIN_NAME '[Service]\nRestart=always\n' 'evil.conf'
    chk s  SN_OP_DROPIN_KEY    '[Service]\nRestart=sometimes\n'
    f="$(d25 e25-t.conf '[Service]\nReadOnlyPaths=/etc\nRestart=on-failure\nRestartSec=5s\n# a comment\n')"
    r="$(snop_dry_approve_ok dropin-install smart-node-e25.service 20-tighten.conf "$f" "$(sha "$f")" --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E25t: ReadOnlyPaths, Restart, RestartSec and a comment -> accepted" || fail "E25t: expected ACCEPT, got: $r"
    r="$(snop_dry_approve_ok dropin-install smart-node-nosuch.service 10-t.conf "$D" "$(sha "$D")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_DROPIN_NO_UNIT ]] && pass "E25u: a unit that does not exist -> refused ($r)" || fail "E25u: expected REFUSE:SN_OP_DROPIN_NO_UNIT, got: $r"
    r="$(snop_dry_approve_ok dropin-install ssh.service 10-t.conf "$D" "$(sha "$D")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_ARG_UNIT_SHAPE ]] && pass "E25v: a unit outside smart-node-* -> refused ($r)" || fail "E25v: expected REFUSE:SN_OP_ARG_UNIT_SHAPE, got: $r"
    mkdir -p "$E22U/smart-node-e25.service.d"
    printf '[Service]\nRestart=always\n' > "$E22U/smart-node-e25.service.d/10-shipped.conf"
    r="$(snop_dry_approve_ok dropin-install smart-node-e25.service 10-shipped.conf "$D" "$(sha "$D")" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_DROPIN_EXISTS ]] && pass "E25w: a drop-in somebody else wrote is not replaced -> refused ($r)" || fail "E25w: expected REFUSE:SN_OP_DROPIN_EXISTS, got: $r"
    printf '# installed-by: sn-op dropin-install\n[Service]\nRestart=always\n' > "$E22U/smart-node-e25.service.d/10-ours.conf"
    r="$(snop_dry_approve_ok dropin-install smart-node-e25.service 10-ours.conf "$D" "$(sha "$D")" --approval 77)"
    [[ "$r" == ACCEPT ]] && pass "E25x: a drop-in this verb installed may be replaced -> accepted" || fail "E25x: expected ACCEPT, got: $r"
elif (( E22_OK == 0 )); then
    cannot "E25: dropin-install fixtures cannot be staged outside /work"
else
    fail "E25: dropin-install is absent from sn-op"
fi

# ---- E26: route-install -----------------------------------------------------
# A route changes what the node's front door answers, so what a file may say is an
# allowlist and the domain it may name is the node's own, from a root-owned file.

if (( E22_OK == 1 )) && grep -q 'verb_route_install' "$SNOP" && python3 -c 'import yaml' 2>/dev/null; then
    E26D="$E22W/e26-dyn"; mkdir -p "$E26D"
    printf 'http:\n  routers:\n    smart-node-taken:\n      rule: "Host(`taken.test.internal`)"\n' > "$E26D/71-x.yml"
    printf 'test.internal\n' > "$E22W/e26-domain"
    chmod 644 "$E22W/e26-domain"
    export SNOP_DRY_RUN_TRAEFIK_DIR="$E26D" SNOP_DRY_RUN_ROUTE_DOMAIN_FILE="$E22W/e26-domain"
    mk26() { # mk26 <name> <host> <url> <middlewares> [extra router line]
        printf 'http:\n  routers:\n    smart-node-%s:\n      rule: "Host(`%s`)"\n      entryPoints: [websecure]\n      service: smart-node-%s\n      middlewares: [%s]\n      tls: {}\n%s  services:\n    smart-node-%s:\n      loadBalancer:\n        servers:\n          - url: "%s"\n' \
            "$1" "$2" "$1" "$4" "${5:+      $5\n}" "$1" "$3" > "$E22W/e26-$1.yml"; echo "$E22W/e26-$1.yml"
    }
    r26() { # r26 <label> <expected> <file> [route-name]
        local got; got="$(snop_dry_approve_ok route-install "${4:-80-t.yml}" "$3" "$(sha "$3")" --approval 77)"
        [[ "$got" == "$2" ]] && pass "E26$1: $2" || fail "E26$1: expected $2, got: $got"
    }
    F="$(mk26 ok good.test.internal http://host.docker.internal:8130/ vpn-only@file)"
    r26 a ACCEPT "$F"
    r="$(snop_dry route-install 80-t.yml "$F" "$(sha "$F")")"
    [[ "$r" == REFUSE:SN_OP_APPROVAL_REQUIRED ]] && pass "E26b: without --approval -> refused ($r)" || fail "E26b: expected REFUSE:SN_OP_APPROVAL_REQUIRED, got: $r"
    r="$(snop_dry_approve_ok route-install 80-t.yml "$F" "$(printf '0%.0s' {1..64})" --approval 77)"
    [[ "$r" == REFUSE:SN_OP_UNIT_SHA_MISMATCH ]] && pass "E26c: a file that is not the one approved -> refused ($r)" || fail "E26c: expected REFUSE:SN_OP_UNIT_SHA_MISMATCH, got: $r"
    r26 d REFUSE:SN_OP_ROUTE_CONTENT "$(mk26 pub good.example.com http://host.docker.internal:8130/ vpn-only@file)"
    r26 e REFUSE:SN_OP_ROUTE_CONTENT "$(mk26 taken taken.test.internal http://127.0.0.1:8130/ vpn-only@file)"
    r26 f REFUSE:SN_OP_ROUTE_CONTENT "$(mk26 noauth good.test.internal http://127.0.0.1:8130/ '')"
    r26 g REFUSE:SN_OP_ROUTE_CONTENT "$(mk26 anyauth good.test.internal http://127.0.0.1:8130/ 'my-own-mw@file')"
    r26 h REFUSE:SN_OP_ROUTE_CONTENT "$(mk26 lowport good.test.internal http://127.0.0.1:80/ vpn-only@file)"
    r26 i REFUSE:SN_OP_ROUTE_CONTENT "$(mk26 remote good.test.internal http://10.0.0.5:8130/ vpn-only@file)"
    r26 j REFUSE:SN_OP_ROUTE_CONTENT "$(mk26 extra good.test.internal http://127.0.0.1:8130/ vpn-only@file 'priority: 1000000')"
    r26 k REFUSE:SN_OP_ARG_ROUTE_NAME "$F" 70-t.yml
    printf '# installed-by: sn-op route-install\nhttp: {}\n' > "$E26D/81-ours.yml"
    printf 'http: {}\n' > "$E26D/82-shipped.yml"
    r26 l ACCEPT "$F" 81-ours.yml
    r26 m REFUSE:SN_OP_ROUTE_EXISTS "$F" 82-shipped.yml
    rm -f "$E22W/e26-domain"
    r26 n REFUSE:SN_OP_ROUTE_DOMAIN "$F"
    printf 'not a domain!\n' > "$E22W/e26-domain"
    chmod 644 "$E22W/e26-domain"
    r26 o REFUSE:SN_OP_ROUTE_DOMAIN "$F"
    unset SNOP_DRY_RUN_TRAEFIK_DIR SNOP_DRY_RUN_ROUTE_DOMAIN_FILE
elif (( E22_OK == 0 )); then
    cannot "E26: route-install fixtures cannot be staged outside /work"
else
    fail "E26: route-install is absent from sn-op, or PyYAML is missing"
fi

# ---- E27: ufw-allow-docker --------------------------------------------------
# One rule: the node's Docker subnet to one TCP port inside a range the node set.

if (( E22_OK == 1 )) && grep -q 'verb_ufw_allow_docker' "$SNOP"; then
    printf 'SMART_NODE_DOCKER_SUBNET=172.19.0.0/16\n' > "$E22W/e27-routing.env"; chmod 644 "$E22W/e27-routing.env"
    printf '8100-8199\n' > "$E22W/e27-ports"; chmod 644 "$E22W/e27-ports"
    export SNOP_DRY_RUN_ROUTING_ENV="$E22W/e27-routing.env" SNOP_DRY_RUN_APP_PORTS_FILE="$E22W/e27-ports"
    u27() { # u27 <label> <expected> <args...>
        local lab="$1" want="$2"; shift 2
        local got; got="$(snop_dry_approve_ok ufw-allow-docker "$@" --approval 77)"
        [[ "$got" == "$want" ]] && pass "E27$lab: $want" || fail "E27$lab: expected $want, got: $got"
    }
    u27 a ACCEPT 8130
    u27 b ACCEPT 8100
    u27 c ACCEPT 8199
    u27 d REFUSE:SN_OP_UFW_PORT 8200
    u27 e REFUSE:SN_OP_UFW_PORT 8099
    u27 f REFUSE:SN_OP_UFW_PORT 5432
    u27 g REFUSE:SN_OP_ARG_PORT 22
    u27 h REFUSE:SN_OP_ARG_PORT 81x0
    u27 i REFUSE:SN_OP_ARG_COUNT 8130 8131
    r="$(snop_dry ufw-allow-docker 8130)"
    [[ "$r" == REFUSE:SN_OP_APPROVAL_REQUIRED ]] && pass "E27j: without --approval -> refused ($r)" || fail "E27j: expected REFUSE:SN_OP_APPROVAL_REQUIRED, got: $r"
    printf 'SMART_NODE_DOCKER_SUBNET=8.8.8.0/24\n' > "$E22W/e27-routing.env"
    u27 k REFUSE:SN_OP_UFW_CONFIG 8130
    printf 'SMART_NODE_DOCKER_SUBNET=172.19.0.0/16\n' > "$E22W/e27-routing.env"
    printf '1-65535\n' > "$E22W/e27-ports"
    u27 l REFUSE:SN_OP_UFW_CONFIG 8130
    rm -f "$E22W/e27-ports"
    u27 m REFUSE:SN_OP_UFW_CONFIG 8130
    unset SNOP_DRY_RUN_ROUTING_ENV SNOP_DRY_RUN_APP_PORTS_FILE
elif (( E22_OK == 0 )); then
    cannot "E27: ufw-allow-docker fixtures cannot be staged outside /work"
else
    fail "E27: ufw-allow-docker is absent from sn-op"
fi

# ---- summary ---------------------------------------------------------------

echo
TOTAL=$((PASS + FAIL))
if [[ $FAIL -eq 0 && $CANNOT -gt 0 ]]; then
    echo "CANNOT RUN  $CANNOT case(s) need a /work checkout; $PASS/$TOTAL check(s) passed  [permissions-sn-op @ $WHAT]"
    exit 2
fi
if [[ $FAIL -eq 0 ]]; then
    echo "PASS  $PASS/$TOTAL check(s) passed  [permissions-sn-op @ $WHAT]"
    exit 0
fi
echo "FAIL  $FAIL/$TOTAL check(s) failed  [permissions-sn-op @ $WHAT]"
exit 1
