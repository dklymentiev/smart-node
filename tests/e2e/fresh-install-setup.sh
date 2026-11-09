#!/usr/bin/env bash
# Run on a disposable node immediately after a clean install. The setup agent
# must be usable before the installer reports success, including its host user.
set -euo pipefail

if [[ "${SN_E2E_DISPOSABLE:-}" != 1 ]]; then
    echo '[e2e] REFUSED: set SN_E2E_DISPOSABLE=1 on a disposable stand' >&2
    exit 2
fi
case "$(hostname -s)" in
    sn-*|stand-*) ;;
    *) echo "[e2e] REFUSED: not a stand: $(hostname -s)" >&2; exit 2 ;;
esac

fail=0
check() {
    if [[ "$1" == 1 ]]; then
        echo "  PASS: $2"
    else
        echo "  FAIL: $2"
        fail=1
    fi
}

row=$(runuser -u postgres -- psql -X -At -d smartnode -c \
    "SELECT slug || '|' || os_user FROM sn_config.agents WHERE capability='configurator' AND archived_at IS NULL" 2>/dev/null || true)
slug=${row%%|*}
os_user=${row#*|}
# The setup agent's slug is its name (john on a release that names it so, foreman on one that does not);
# what a fresh install guarantees is that there is exactly one, with a host user of the same name.
if [[ -n "$slug" && "$row" != *$'\n'* && "$os_user" == "agent-$slug" ]]; then
    check 1 'fresh install has one active setup agent with OS identity'
else
    check 0 "fresh install has one active setup agent with OS identity (got ${row:-none})"
fi

if [[ -n "$slug" && "$os_user" == "agent-$slug" ]] && id "$os_user" >/dev/null 2>&1; then
    check 1 'setup agent OS user exists'
else
    check 0 'setup agent OS user exists'
fi

if [[ -n "$slug" && "$os_user" == "agent-$slug" ]] && [[ -d "/srv/agents/$slug" ]]; then
    check 1 'setup agent directory exists'
else
    check 0 'setup agent directory exists'
fi

if [[ -n "${E2E_ONBOARDING_BASELINE:-}" ]]; then
    # The run has a pre-update baseline, so this node was updated, not freshly installed: the seed leaves an
    # established node's setup task as the owner has it (onboarding-seed.sh), so there is none to find.
    echo '  SKIP: setup task (this node was updated from a release that already had a setup agent)'
else
    task_count=$(runuser -u postgres -- psql -X -At -d planner -c \
        "SELECT count(*) FROM tasks WHERE title='Configure this business' AND assignee='$slug' AND status <> 'closed'" 2>/dev/null || true)
    if [[ "$task_count" == 1 ]]; then
        check 1 'setup task exists once in Planner'
    else
        check 0 "setup task exists once in Planner (got ${task_count:-none})"
    fi
fi

if (( fail )); then
    echo '[e2e] fresh-install-setup: FAIL'
    exit 1
fi
echo '[e2e] fresh-install-setup: PASS'
