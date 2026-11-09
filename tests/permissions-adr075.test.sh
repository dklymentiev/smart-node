#!/usr/bin/env bash
#
# permissions-adr075.test.sh -- source-level filesystem permission gates.
#
# Runs against the working tree (default) or against a git ref (--baseline).
# The baseline mode can show whether an older tree catches permission drift.
#
# Sixteen checks:
#
#   1. classes.json declares filesystem_homes and every entry either carries
#      a writer_group naming grp-role-*, grp-prj-* or grp-agents, or explicitly
#      declares itself root-owned (writer_group=null with owner=root, group=root).
#   2. Every reader_groups entry follows the same shape (grp-role-*, grp-prj-*
#      or grp-agents).
#   3. sn_config.projects schema carries a `restricted` boolean column with
#      DEFAULT false, in both CREATE TABLE and an ADD COLUMN IF NOT EXISTS
#      line so the update path also delivers it.
#   4. provision/sudoers/agent-max.sudoers passes `visudo -cf` (syntactic sudo
#      spec, refuses to leave a broken file in the tree).
#   5. The sudoers file names ONLY the validated wrapper
#      (/usr/local/sbin/sn-op). The old SN_WORK_PERM /
#      SN_WORK_GROUP aliases are superseded by the SN_OP grant; see
#      permissions-sn-op.test.sh for the wrapper-argument gates. No shell,
#      editor, mover, or write outside /work.
#   6. B01-core.sh installs the sudoers file with a visudo -cf gate and an
#      atomic mv, and B01 --verify asserts /etc/sudoers.d/agent-max is present
#      + mode 0440 + passes visudo -c.
#   7. cron-runner.sh sets umask 002 before running the agent wake, so files
#      the agent writes into /work land group-writable per the ACL model.
#   8. tools/classes-check.sh refuses a planted-defect classes.json (a
#      /work/... entry with writer_group=null AND owner!=root) with the named
#      error 'HOME NO WRITER GROUP' (named error, not silent
#      skip).
#   9. B01-core.sh carries a removal routine for the superseded broad
#      grant at /etc/sudoers.d/smart-node-sysadmin, called from the
#      run path in the same pass as the agent-max install, with a post-
#      removal visudo -c gate. Leaving both files in /etc/sudoers.d
#      unions to the broader grant.
#  10. B01 --verify asserts /etc/sudoers.d/smart-node-sysadmin is absent.
#  11. B01 --verify runs check 22 through sysadmin_sudo_scope_ok, which walks
#      every agent-* member of grp-role-sysadmin that is also in
#      smart-node-agents (admin skipped) and compares that member's
#      `sudo -ll -U <user>` against the SN_OP alias of the shipped sidecar.
#      Subject moved from single-user agent-sysadmin to %grp-role-sysadmin
#      because a single-user grant stops applying after a rename. Source-level
#      only; live sudo behaviour is
#      tests/sysadmin-sudo-scope.test.sh.
#  12. bootstrap/groups.sh does NOT hard-code the wrong DB literal
#      `-U postgres -d smart_node` and DOES source the persistence
#      contract (SMART_NODE_PERSISTENCE_ENV / SMART_NODE_PG_CORE_DB).
#      The old literal silently skipped role-group membership on a node.
#  13. bootstrap/acls.sh does NOT hard-code the wrong DB literal and DOES
#      use the persistence contract. Same shape as check 12; per-project
#      ACLs were the other silent skip.
#  14. bootstrap/groups.sh AND bootstrap/acls.sh carry a named
#      error 'HOME NO FILESYSTEM_HOMES' so a classes.json from a release
#      without filesystem_homes refuses with a name the operator can find,
#      instead of a jq "Cannot iterate over null" trace.
#  15. Every writer_group and reader_group in classes.json filesystem_homes
#      is one of: 'grp-agents' (the collective), or 'grp-role-<cap>' where
#      <cap> appears in the B02 sn-core-schema capability-backfill CASE
#      statement (or is one of the capability values used by fresh installs
#      via onboarding-seed.sh). A writer_group that no capability produces
#      would be a group groups.sh never creates: exactly the shape of D2
#      (grp-role-foreman while capability='configurator').
#  16. Meta check: --baseline REF reports whether at least one of checks
#      1-15 fails on that older tree, demonstrating that they catch drift.
#      Only runs when --baseline is given.
#
# Usage:
#   tests/permissions-adr075.test.sh                  # working tree
#   tests/permissions-adr075.test.sh --baseline REF   # inspect an older ref
#
# Exit: 0 all pass, 1 any fail, 2 setup error.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---- resolve source-of-truth files (working tree or baseline ref) -----------

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

CLASSES="$WORK/classes.json"
B02="$WORK/B02.sql"
SUDOERS="$WORK/agent-max.sudoers"
B01="$WORK/B01-core.sh"
CRON="$WORK/cron-runner.sh"
CHECK="$WORK/classes-check.sh"
GROUPS_SH="$WORK/groups.sh"
ACLS_SH="$WORK/acls.sh"

fetch node-versioning/classes.json "$CLASSES" || exit 2
fetch bootstrap/B02-sn-core-schema.sql "$B02" || exit 2
fetch bootstrap/B01-core.sh "$B01" || exit 2
fetch bootstrap/cron-runner.sh "$CRON" || exit 2
fetch tools/classes-check.sh "$CHECK" || exit 2

# groups.sh and acls.sh may be absent on an older baseline.
# Fetch best-effort; the checks that depend on them will fail loudly if the
# file is missing (a legitimate failure, not a setup error).
GROUPS_PRESENT=1
ACLS_PRESENT=1
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" show "$BASELINE:bootstrap/groups.sh" > "$GROUPS_SH" 2>/dev/null \
        || GROUPS_PRESENT=0
    git -C "$ROOT" show "$BASELINE:bootstrap/acls.sh"   > "$ACLS_SH"   2>/dev/null \
        || ACLS_PRESENT=0
else
    [[ -r "$ROOT/bootstrap/groups.sh" ]] \
        && cp "$ROOT/bootstrap/groups.sh" "$GROUPS_SH" \
        || GROUPS_PRESENT=0
    [[ -r "$ROOT/bootstrap/acls.sh" ]] \
        && cp "$ROOT/bootstrap/acls.sh"   "$ACLS_SH" \
        || ACLS_PRESENT=0
fi

# Sudoers file may be absent on the baseline -- that is a legitimate check-4
# failure, not a setup error. Fetch best-effort and let the check report.
SUDOERS_PRESENT=1
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" show "$BASELINE:provision/sudoers/agent-max.sudoers" > "$SUDOERS" 2>/dev/null \
        || SUDOERS_PRESENT=0
else
    [[ -r "$ROOT/provision/sudoers/agent-max.sudoers" ]] \
        && cp "$ROOT/provision/sudoers/agent-max.sudoers" "$SUDOERS" \
        || SUDOERS_PRESENT=0
fi

WHAT="${BASELINE:-working tree}"
echo "permissions-adr075 ($WHAT)"

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo "  ok   $*"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }

# ---- check 1: filesystem_homes writer_group presence + shape ---------------

if ! command -v jq >/dev/null; then
    fail "check 1: jq is required and is not installed"
else
    out="$(jq -r '
        (.filesystem_homes // []) as $h
        | if ($h | length) == 0 then "EMPTY" else
            [ $h[] |
                . as $r
                | if $r.writer_group == null then
                    if ($r.owner // "") == "root" and ($r.group // "") == "root"
                        then empty
                        else "BAD_NULL " + $r.path
                    end
                  elif ($r.writer_group | test("^(grp-role-|grp-prj-|grp-agents$)")) then empty
                  else "BAD_SHAPE " + $r.path + " " + $r.writer_group
                  end
            ] | join("\n")
          end
    ' "$CLASSES" 2>/dev/null)"
    if [[ "$out" == "EMPTY" ]]; then
        fail "check 1: classes.json has no filesystem_homes"
    elif [[ -n "$out" ]]; then
        while IFS= read -r line; do
            fail "check 1: $line"
        done <<< "$out"
    else
        pass "check 1: every filesystem_homes entry has a valid writer_group or is root-owned"
    fi
fi

# ---- check 2: reader_groups entries follow grp-* shape ----------------------

out="$(jq -r '
    [ .filesystem_homes[]? as $r
      | ($r.reader_groups // [])[]?
      | select(test("^(grp-role-|grp-prj-|grp-agents$)") | not)
      | ($r.path + " " + .)
    ] | join("\n")
' "$CLASSES" 2>/dev/null)"
if [[ -n "$out" ]]; then
    while IFS= read -r line; do
        fail "check 2: reader_groups shape: $line"
    done <<< "$out"
else
    pass "check 2: every reader_groups entry matches grp-role-*, grp-prj-* or grp-agents"
fi

# ---- check 3: sn_config.projects has a `restricted` boolean column ----------

# Both the CREATE TABLE column and the ADD COLUMN IF NOT EXISTS migration line
# must be present -- CREATE TABLE covers clean installs, ADD COLUMN covers
# updates from a schema without the restricted column.
if grep -qE '^\s*restricted\s+boolean\s+DEFAULT\s+false\s+NOT\s+NULL' "$B02" \
    && grep -qE 'ALTER\s+TABLE\s+sn_config\.projects\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+restricted\s+boolean' "$B02"; then
    pass "check 3: sn_config.projects.restricted column present in CREATE TABLE + ADD COLUMN"
else
    fail "check 3: sn_config.projects.restricted column missing from B02 (CREATE TABLE and/or ADD COLUMN)"
fi

# ---- check 4: sudoers file passes visudo -cf --------------------------------

if [[ $SUDOERS_PRESENT -eq 0 ]]; then
    fail "check 4: provision/sudoers/agent-max.sudoers absent"
elif ! command -v visudo >/dev/null; then
    fail "check 4: visudo not available (required to validate sudoers)"
else
    if visudo -cf "$SUDOERS" >/dev/null 2>&1; then
        pass "check 4: provision/sudoers/agent-max.sudoers passes visudo -cf"
    else
        fail "check 4: provision/sudoers/agent-max.sudoers refused by visudo -cf"
    fi
fi

# ---- check 5: sudoers spec is the SN_OP wrapper form only -------------------
#
# The old SN_WORK_PERM and SN_WORK_GROUP aliases were unsafe under
# fnmatch(3) matching without FNM_PATHNAME and are superseded by SN_OP. The
# only in-scope command is now the validated wrapper /usr/local/sbin/sn-op.
# See tests/permissions-sn-op.test.sh for the E1-E18 argument gates.

if [[ $SUDOERS_PRESENT -eq 1 ]]; then
    body="$(grep -Ev '^\s*(#|$)' "$SUDOERS" || true)"
    # Refuse any shell/editor/mover/direct binary anywhere in the body.
    banned="$(grep -Eo '(/bin/(sh|bash|dash)|/usr/bin/(vim?|nano|emacs|less|more|env)|/bin/(mv|cp|rm|chgrp|chmod)|/usr/bin/(mv|cp|rm|gpasswd|setfacl)|/usr/sbin/(groupadd|groupmod|groupdel)|/bin/su|/usr/bin/sudo)' <<< "$body" || true)"
    if [[ -n "$banned" ]]; then
        fail "check 5: sudoers names direct binaries outside SN_OP scope: $banned"
    else
        # The only allowed absolute path is /usr/local/sbin/sn-op.
        bad_path="$(grep -oE '/[A-Za-z][A-Za-z0-9_./-]*' <<< "$body" \
                    | grep -v '^/usr/local/sbin/sn-op$' \
                    | grep -Ev '^/etc/sudoers\.d(/agent-max)?$' \
                    | sort -u || true)"
        # And the SN_WORK_PERM / SN_WORK_GROUP aliases must be gone.
        legacy="$(grep -oE 'SN_WORK_PERM|SN_WORK_GROUP' <<< "$body" | sort -u || true)"
        if [[ -n "$bad_path" ]]; then
            fail "check 5: sudoers names paths outside SN_OP scope: $(echo $bad_path)"
        elif [[ -n "$legacy" ]]; then
            fail "check 5: sudoers still carries superseded aliases: $legacy"
        else
            pass "check 5: sudoers spec grants only SN_OP"
        fi
    fi
fi

# ---- check 6: B01-core.sh installs sudoers with visudo gate + verify check --

if grep -q 'install_agent_max_sudoers' "$B01" \
    && grep -q 'visudo -c' "$B01" \
    && grep -q '/etc/sudoers.d/agent-max' "$B01"; then
    if grep -qE 'verify_check.*agent-max' "$B01"; then
        pass "check 6: B01 installs sudoers with visudo gate AND --verify asserts it"
    else
        fail "check 6: B01 installs sudoers but --verify has no agent-max assertion"
    fi
else
    fail "check 6: B01 does not install /etc/sudoers.d/agent-max via visudo-gated routine"
fi

# ---- check 7: cron-runner.sh sets umask 002 --------------------------------

if grep -qE '^\s*umask\s+002\b' "$CRON"; then
    pass "check 7: cron-runner.sh sets umask 002"
else
    fail "check 7: cron-runner.sh does not set umask 002 for group-writable files"
fi
if grep -qE 'chmod 0644 "\$LOG"' "$CRON"; then
    pass "check 7b: cron-runner keeps generated cron logs non-group-writable"
else
    fail "check 7b: cron-runner keeps generated cron logs non-group-writable"
fi

# ---- check 8: classes-check.sh names the HOME NO WRITER GROUP error ---------

if grep -q 'HOME NO WRITER GROUP' "$CHECK"; then
    # Prove the lint actually fires. classes-check.sh resolves its inputs from
    # $(dirname "$0")/.. so we rebuild a minimal tree in $WORK with a broken
    # classes.json (one /work entry: writer_group=null AND owner=admin, which
    # is neither root nor grp-*) and run the check from there. We need only
    # classes.json, docs/node/entities.md, and bootstrap/B02-sn-core-schema.sql
    # to satisfy classes-check's pre-flight (missing file exits 1 without ever
    # reaching the filesystem_homes lint).
    if command -v jq >/dev/null; then
        scratch="$WORK/scratch"
        mkdir -p "$scratch/tools" "$scratch/node-versioning" \
                 "$scratch/docs/node" "$scratch/bootstrap"
        # Break one entry that currently has a writer_group. This produces a
        # HOME NO WRITER GROUP hit; other checks may also fire (that is fine).
        jq '(.filesystem_homes[] | select(.path == "/work/flows")) |= (.writer_group = null | .owner = "admin" | .group = "admin")' \
            "$CLASSES" > "$scratch/node-versioning/classes.json"
        # Minimal siblings so the pre-flight passes. entities.md contents do
        # not matter for this check (the entities/classes cross-check may
        # produce noise, which we discard); the filesystem_homes lint runs
        # regardless.
        printf '# entities\n' > "$scratch/docs/node/entities.md"
        printf -- '-- schema stub\n' > "$scratch/bootstrap/B02-sn-core-schema.sql"
        cp "$CHECK" "$scratch/tools/classes-check.sh"
        chmod +x "$scratch/tools/classes-check.sh"
        # classes-check.sh exits 1 on any note(); under pipefail the pipeline
        # would carry that exit even when grep matched. Capture output first,
        # then grep, so the check-8 outcome depends on the presence of the
        # named error, not on the lint's aggregate exit.
        check_out="$(bash "$scratch/tools/classes-check.sh" 2>&1)"
        if grep -q 'HOME NO WRITER GROUP' <<< "$check_out"; then
            pass "check 8: classes-check.sh refuses a planted-defect classes.json with 'HOME NO WRITER GROUP'"
        else
            fail "check 8: classes-check.sh did not emit 'HOME NO WRITER GROUP' on planted defect"
        fi
    else
        fail "check 8: jq required for planted-defect fixture"
    fi
else
    fail "check 8: classes-check.sh does not carry a 'HOME NO WRITER GROUP' named error"
fi

# ---- check 9: B01 removes the superseded broad sudoers file --------------

# The routine must exist, be called from run_intent (or equivalent run path),
# and follow the rm-then-visudo-c pattern. The three grep patterns together
# prove the shape: named function, called at run time, gates the resulting
# sudoers state with visudo -c after removal.
if grep -q 'remove_superseded_sysadmin_sudoers' "$B01" \
    && grep -q '/etc/sudoers.d/smart-node-sysadmin' "$B01"; then
    # A remove-then-check pattern anywhere in the function is enough: the
    # exact wording will drift, the shape matters.
    if awk '
        /^remove_superseded_sysadmin_sudoers\(\)/ { inside=1; next }
        inside && /^}/                            { inside=0 }
        inside && /rm .*smart-node-sysadmin|rm[[:space:]].*\$old/   { saw_rm=1 }
        inside && /visudo -c/                     { saw_visudo=1 }
        END { exit !(saw_rm && saw_visudo) }
    ' "$B01"; then
        # And the function must actually be called from the run path.
        if grep -qE '^\s*remove_superseded_sysadmin_sudoers\s*$' "$B01"; then
            pass "check 9: B01 removes the superseded /etc/sudoers.d/smart-node-sysadmin with a visudo -c gate"
        else
            fail "check 9: B01 defines the removal routine but does not call it from the run path"
        fi
    else
        fail "check 9: B01 removal routine does not follow the rm + visudo-c shape"
    fi
else
    fail "check 9: B01 has no removal routine for the superseded /etc/sudoers.d/smart-node-sysadmin"
fi

# ---- check 10: B01 --verify asserts absence of the superseded sudoers file --

# Grep the file-level source for a verify_check whose body checks that
# /etc/sudoers.d/smart-node-sysadmin is absent. We match the file path and
# a negative existence test on the same nearby lines rather than one exact
# form, so a wording drift is not a false negative.
if awk '
    /verify_check.*smart-node-sysadmin/  { found=NR }
    found && NR <= found+5 && /\[\[ ! -e .*smart-node-sysadmin/ { hit=1 }
    END { exit !hit }
' "$B01"; then
    pass "check 10: B01 --verify asserts /etc/sudoers.d/smart-node-sysadmin is absent"
else
    fail "check 10: B01 --verify has no assertion that /etc/sudoers.d/smart-node-sysadmin is absent"
fi

# ---- check 11: B01 --verify compares agent-sysadmin's sudo -ll with the canon --

# Wiring only: the verify_check calls sysadmin_sudo_scope_ok, and that
# function walks grp-role-sysadmin members that are also in smart-node-agents
# (admin skipped), reads `sudo -ll -U <member>` and hands it to sudo_ll_within
# against the SN_OP alias of the sidecar. What the comparison accepts and
# refuses on recorded sudo output is tests/sysadmin-sudo-scope.test.sh; here
# we only assert the wiring.
if grep -q "verify_check \"grp-role-sysadmin members' effective sudo = SN_OP only\"" "$B01" \
        && grep -qE '^[[:space:]]+sysadmin_sudo_scope_ok[[:space:]]*$' "$B01" \
        && grep -qE 'getent group grp-role-sysadmin' "$B01" \
        && grep -qE 'smart-node-agents' "$B01" \
        && grep -qE 'sudoers_alias_commands "\$canon" SN_OP' "$B01" \
        && grep -qE 'sudo -n -ll -U "\$m"' "$B01" \
        && grep -q 'sudo_ll_within' "$B01" \
        && ! grep -qE 'sudoers_alias_commands[^\n]+SN_WORK_(PERM|GROUP)' "$B01"; then
    pass "check 11: B01 --verify walks grp-role-sysadmin members (smart-node-agents ∩, admin skipped) and compares sudo -ll against the SN_OP canon"
else
    fail "check 11: B01 --verify has no group-walk sudo -ll scope comparison against the SN_OP canon (or still names the SN_WORK_* aliases)"
fi

# ---- check 12: groups.sh uses the persistence contract, not a bad literal ---

# Two halves. Refuse the wrong literal (`-U postgres -d smart_node` is what
# silently skipped role-group membership on a node), AND require the
# canonical contract (source PERSISTENCE_ENV, use SMART_NODE_PG_CORE_DB).
if [[ $GROUPS_PRESENT -eq 0 ]]; then
    fail "check 12: bootstrap/groups.sh absent"
elif grep -qE 'psql[^|]*-U[[:space:]]+postgres[[:space:]]+-d[[:space:]]+smart_node' "$GROUPS_SH"; then
    fail "check 12: bootstrap/groups.sh still hard-codes 'psql -U postgres -d smart_node' (D1)"
elif ! grep -qE 'SMART_NODE_PG_CORE_DB|SMART_NODE_PERSISTENCE_ENV|PERSISTENCE_ENV' "$GROUPS_SH"; then
    fail "check 12: bootstrap/groups.sh does not reference the persistence contract (SMART_NODE_PG_CORE_DB / PERSISTENCE_ENV)"
else
    pass "check 12: bootstrap/groups.sh uses the persistence contract (no smart_node literal, sources PERSISTENCE_ENV)"
fi

# ---- check 13: acls.sh uses the persistence contract, same shape as 12 ------

if [[ $ACLS_PRESENT -eq 0 ]]; then
    fail "check 13: bootstrap/acls.sh absent"
elif grep -qE 'psql[^|]*-U[[:space:]]+postgres[[:space:]]+-d[[:space:]]+smart_node' "$ACLS_SH"; then
    fail "check 13: bootstrap/acls.sh still hard-codes 'psql -U postgres -d smart_node' (D1)"
elif ! grep -qE 'SMART_NODE_PG_CORE_DB|SMART_NODE_PERSISTENCE_ENV|PERSISTENCE_ENV' "$ACLS_SH"; then
    fail "check 13: bootstrap/acls.sh does not reference the persistence contract (SMART_NODE_PG_CORE_DB / PERSISTENCE_ENV)"
else
    pass "check 13: bootstrap/acls.sh uses the persistence contract (no smart_node literal, sources PERSISTENCE_ENV)"
fi

# ---- check 14: both scripts carry the HOME NO FILESYSTEM_HOMES named error --

check14_missing=""
[[ $GROUPS_PRESENT -eq 1 ]] || check14_missing="$check14_missing groups.sh"
[[ $ACLS_PRESENT   -eq 1 ]] || check14_missing="$check14_missing acls.sh"
if [[ -n "$check14_missing" ]]; then
    fail "check 14: source file(s) absent:$check14_missing"
elif grep -q 'HOME NO FILESYSTEM_HOMES' "$GROUPS_SH" \
    && grep -q 'HOME NO FILESYSTEM_HOMES' "$ACLS_SH"; then
    pass "check 14: groups.sh AND acls.sh carry the 'HOME NO FILESYSTEM_HOMES' named error"
else
    where=""
    grep -q 'HOME NO FILESYSTEM_HOMES' "$GROUPS_SH" || where="$where groups.sh"
    grep -q 'HOME NO FILESYSTEM_HOMES' "$ACLS_SH"   || where="$where acls.sh"
    fail "check 14: 'HOME NO FILESYSTEM_HOMES' missing in:$where (D3 named error)"
fi

# ---- check 15: writer/reader groups in classes.json map to real capabilities

# Union of capability values a real node can produce:
#   (a) values in the B02 sn-core-schema capability-backfill CASE statement,
#   (b) values that onboarding-seed.sh writes for a fresh install, and
#   (c) the ELSE branch of the backfill (agent's slug) -- but we cannot
#       enumerate slugs at test time; we only trust (a) + (b), which is what
#       every canonical role produces.
# Every writer_group and every reader_group in filesystem_homes must be
# either 'grp-agents' or 'grp-role-<cap>' with <cap> in that union, OR the
# row is root-owned (writer_group=null).
if ! command -v jq >/dev/null; then
    fail "check 15: jq is required and is not installed"
else
    # (a) capabilities from the B02 backfill CASE. Strip surrounding quotes
    # from the 'THEN' target. The CASE ends with 'ELSE slug', which we skip.
    caps_a="$(awk '
        /^UPDATE sn_config\.agents/                { inside=1 }
        inside && / WHERE capability = /           { inside=0 }
        inside && /THEN/ {
            n = split($0, f, "'\''")
            # THEN target is the second single-quoted string on the line
            for (i=1; i<=n; i++) if (f[i] ~ /THEN/) { print f[i+1]; break }
        }
    ' "$B02" | sort -u)"
    # (b) capability values written by onboarding-seed.sh. Extract every
    # 'capability': 'value' literal in the seed file (best effort).
    SEED_TMP="$WORK/onboarding-seed.sh"
    seed_caps=""
    if [[ -n "$BASELINE" ]]; then
        git -C "$ROOT" show "$BASELINE:bootstrap/onboarding-seed.sh" > "$SEED_TMP" 2>/dev/null \
            && seed_caps="$(grep -oE "\"capability\"[[:space:]]*:[[:space:]]*\"[^\"]+\"" "$SEED_TMP" \
                    | sed -E 's/.*"([^"]+)".*/\1/' | sort -u)" || true
    elif [[ -r "$ROOT/bootstrap/onboarding-seed.sh" ]]; then
        cp "$ROOT/bootstrap/onboarding-seed.sh" "$SEED_TMP"
        seed_caps="$(grep -oE "\"capability\"[[:space:]]*:[[:space:]]*\"[^\"]+\"" "$SEED_TMP" \
                | sed -E 's/.*"([^"]+)".*/\1/' | sort -u)"
    fi
    known_caps="$(printf '%s\n%s\n' "$caps_a" "$seed_caps" | sed '/^$/d' | sort -u)"

    # Every writer/reader group name in classes.json filesystem_homes.
    declared="$(jq -r '
        [ .filesystem_homes[]
          | ((.writer_group // empty),
             ((.reader_groups // [])[])) ]
        | unique | .[]
    ' "$CLASSES")"

    bad=""
    while IFS= read -r g; do
        [[ -z "$g" ]] && continue
        # grp-agents is a collective maintained by groups.sh apply_agents_group.
        [[ "$g" == "grp-agents" ]] && continue
        # grp-prj-<slug> is per-project and created from sn_config.projects;
        # classes.json never lists a per-slug row here (it lists /work/projects
        # only), but tolerate it if it ever does.
        [[ "$g" == grp-prj-* ]] && continue
        if [[ "$g" == grp-role-* ]]; then
            cap="${g#grp-role-}"
            if ! grep -qxF "$cap" <<< "$known_caps"; then
                bad="$bad $g"
            fi
        else
            bad="$bad $g"
        fi
    done <<< "$declared"

    if [[ -z "$bad" ]]; then
        pass "check 15: every filesystem_homes group name maps to a real capability (B02 backfill + onboarding-seed)"
    else
        fail "check 15: classes.json declares group(s) no capability produces:$bad (D2 shape: group never created)"
    fi
fi

# ---- summary ---------------------------------------------------------------

echo
TOTAL=$((PASS + FAIL))
if [[ $FAIL -eq 0 ]]; then
    echo "PASS  $PASS/$TOTAL check(s) passed  [permissions-adr075 @ $WHAT]"
    exit 0
fi
echo "FAIL  $FAIL/$TOTAL check(s) failed  [permissions-adr075 @ $WHAT]"
exit 1
