#!/usr/bin/env bash
#
# The installer's "was this companion here before" probe must ask as root.
#
# WHAT WENT WRONG. 1.12.38 stopped an update from silently skipping a
# communication companion that was configured on an earlier run: when no channel
# credentials are given, it looks for /etc/smart-node/companions.d/communication.json
# and re-runs the companion if it is there. The probe runs over ssh WITHOUT sudo.
# B04 creates that directory as drwxr-s--- root:smart-node, and the ssh user is
# an ordinary admin account that need not be in that group, so it cannot search
# the directory and `test -f` answers no for a file that exists. On the node the
# defect was reported from, the file was there, dated the day before, and the
# update logged "communication companion not installed" and skipped it exactly
# as 1.12.37 had, then exited 0 calling the run complete.
#
# THE PROPERTIES:
#   1. the probe asks as root
#   2. the probe still asks about the same marker file
#   3. an unprivileged test -f cannot see a file it may not search to
#      (the failure mode itself, demonstrated rather than asserted)
#   4. the closing report knows whether the companion ran
#   5. the agent count after the seed counts active agents only
#   6. install-on.sh parses
#
#   tests/companion-probe-asks-as-root.test.sh                  # tree, PASS
#   tests/companion-probe-asks-as-root.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()      { printf '  ok   %s\n' "$1"; }
bad()     { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
skipped() { printf '  --   %s\n' "$1"; }

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "installer: the companion probe asks as root (git:$BASELINE)"
else echo "installer: the companion probe asks as root (working tree)"; fi

D=$(mktemp -d); trap 'chmod -R u+rwx "$D" 2>/dev/null; rm -rf "$D"' EXIT
read_src orchestrator/install-on.sh > "$D/install-on.sh"
[[ -s "$D/install-on.sh" ]] || { bad "install-on.sh is readable"; echo; echo "FAIL"; exit 1; }

probe=$(grep -m1 'companions.d/communication.json' "$D/install-on.sh")

# 1 + 2. The probe, as written.
if [[ "$probe" == *"sudo -n test -f"* ]]; then
    ok "the probe asks as root"
else
    bad "the probe asks as root -- it reads: ${probe# }"
fi
if [[ "$probe" == *"/etc/smart-node/companions.d/communication.json"* ]]; then
    ok "the probe still asks about the companion record"
else
    bad "the probe still asks about the companion record"
fi

# 3. Why it matters, shown with the same primitive the installer uses: a file
#    that exists, under a directory the caller may not search, reports absent.
if [[ "$(id -u)" == "0" ]]; then
    skipped "unprivileged test -f (running as root; the bits do not apply)"
else
    mkdir -p "$D/closed" && : > "$D/closed/marker.json" && chmod 000 "$D/closed"
    if test -f "$D/closed/marker.json" 2>/dev/null; then
        bad "an unreadable directory hides the file from test -f"
    else
        ok "an unreadable directory hides the file from test -f, which is the defect"
    fi
    chmod 755 "$D/closed"
fi

# 4. The closing report must describe the run, not the command line. With the
#    probe fixed, a companion carried forward without credentials runs, and the
#    report still announced it as skipped, because that line asks only whether
#    --tg-token or --mail-host was given.
rerun=$(grep -c 'COMM_RAN=1' "$D/install-on.sh")
claim=$(grep -m1 'elif \[\[ -z "\$TG_TOKEN\$MAIL_HOST"' "$D/install-on.sh")
if [[ "$rerun" -gt 0 && "$claim" == *'COMM_RAN'* ]]; then
    ok "the closing report knows whether the companion actually ran"
else
    bad "the closing report calls the companion skipped whenever no credentials were given"
fi

# 5. The roster count the installer reports after the seed.
count=$(grep -m1 'FROM sn_config.agents' "$D/install-on.sh")
if [[ "$count" == *"archived_at IS NULL"* ]]; then
    ok "the seed's agent count counts active agents only"
else
    bad "the seed's agent count counts archived rows too, and overstates the roster"
fi

if bash -n "$D/install-on.sh" 2>/dev/null; then ok "install-on.sh parses"; else bad "install-on.sh parses"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
