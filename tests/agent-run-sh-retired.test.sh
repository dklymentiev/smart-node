#!/usr/bin/env bash
#
# run.sh is gone from the node: not shipped, not created, not left behind.
#
# WHAT WENT WRONG. run.sh was the per-agent cron entry point. the Runner became the
# Runner the only waker, but the file stayed: agent_fs copied it into every new
# agent, core-api.sh re-converged it into existing ones on every update, and the
# onboarding seed could still create a cron that ran it. Run by hand it spent a
# model call outside brin and the gateway -- no session, no per-request usage,
# no budget rule, no claim -- and anyone reading an agent folder concluded that
# was how agents wake. It also capped a hand wake at 300 s (`timeout 300`).
#
# WHAT IS CHECKED. Behaviour where it can run anywhere, source only where not:
#
#   no-template      the shipped _template has no run.sh
#   create           agent_fs.create_agent_dir, run for real against a temp
#                    root and the shipped template, makes a folder with no run.sh
#   retire-files     retire_agent_runners, extracted from core-api.sh and run
#                    with the system calls stubbed, moves every agent's run.sh,
#                    its run.sh.pre-*.bak copies and _template/run.sh out of the
#                    agent folders into .archive/run-sh-retired-*/, not executable
#   retire-crons     it deletes the crons whose command is an agent's run.sh,
#                    and records one event per removal. The cluster and database
#                    are read from the persistence contract file with lib.sh's
#                    own _envfile_get, as on a node; the psql stand-in refuses
#                    without them
#   retire-quiet     on a node that has nothing to retire it moves nothing and
#                    records nothing
#   no-converge      nothing puts run.sh back: converge_agent_runners is gone
#                    and the skeleton step calls the retire step
#   no-wake-cron     the onboarding seed cannot create a cron that runs run.sh
#   no-instructions  nothing shipped tells anyone to run an agent's run.sh
#
#   tests/agent-run-sh-retired.test.sh                 # working tree, expect PASS
#   tests/agent-run-sh-retired.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

TREE="$WORK/tree"
mkdir -p "$TREE"
if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" archive "$BASELINE" companions/srv-skeleton companions/core-api/lib \
        companions/core-api.sh bootstrap/onboarding-seed.sh bootstrap/lib.sh docs \
        | tar -x -C "$TREE" || { echo "cannot read tree at $BASELINE" >&2; exit 2; }
    echo "run.sh retired (git:$BASELINE)"
else
    cp -a "$ROOT/companions" "$TREE/"
    mkdir -p "$TREE/bootstrap"; cp -a "$ROOT/bootstrap/onboarding-seed.sh" "$ROOT/bootstrap/lib.sh" "$TREE/bootstrap/"
    cp -a "$ROOT/docs" "$TREE/"
    echo "run.sh retired (working tree)"
fi

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

SKEL="$TREE/companions/srv-skeleton"
CAPI="$TREE/companions/core-api.sh"
LIBSH="$TREE/bootstrap/lib.sh"

# ---------------------------------------------------------------- no-template
[[ ! -e "$SKEL/agents/_template/run.sh" ]] \
    && ok "no-template: the shipped agent template has no run.sh" \
    || bad "no-template: companions/srv-skeleton/agents/_template/run.sh is shipped"

# ---------------------------------------------------------------- create
mkdir -p "$WORK/agents"
cp -a "$SKEL/agents/_template" "$WORK/agents/_template"
out="$(SN_AGENTS_ROOT="$WORK/agents" SN_AGENT_TEMPLATE="$WORK/agents/_template" \
       PYTHONPATH="$TREE/companions/core-api" python3 - 2>&1 <<'PY'
from lib import agent_fs
d = agent_fs.provision_agent_dir(slug="probe", title="Probe", specialist_body="You are a probe.",
                                 model="sonnet", role="probe", description=None)
print("DIR", d)
PY
)"
if [[ "$out" != *"DIR "* ]]; then
    bad "create: agent_fs could not create an agent folder: ${out:0:200}"
elif [[ -e "$WORK/agents/probe/run.sh" ]]; then
    bad "create: a newly created agent folder gets a run.sh"
else
    ok "create: a newly created agent folder has no run.sh (created for real by agent_fs)"
fi

# ---------------------------------------------------------------- retire
# Extract the function; stub everything that touches the system.
fn="$(awk '/^retire_agent_runners\(\) \{/,/^\}/' "$CAPI")"
if [[ -z "$fn" ]]; then
    bad "retire-files: core-api.sh has no retire_agent_runners step"
    bad "retire-crons: (no retire step to run)"
    bad "retire-quiet: (no retire step to run)"
else
    run_retire() {  # run_retire <agents_root> <cron rows to return>
        local ar="$1" rows="$2"
        env -i PATH="$PATH" HOME="$WORK" bash -c '
            SN_AGENTS_ROOT="$1"; ROWS="$2"; EV="$3"; LOGF="$4"
            SERVICE_USER=x; SMART_NODE_GROUP=x
            # NOT set here: the installer shell does not carry the cluster or the
            # database. They come from the B02 contract file, read with the real
            # _envfile_get from lib.sh -- a fixture that presets them is how the
            # first version of this test passed a step that failed on the stand.
            SMART_NODE_PERSISTENCE_ENV="$5"
            '"$envfile_get"'
            log() { printf "%s\n" "$*" >> "$LOGF"; }
            log_event() { printf "%s|%s|%s|%s|%s\n" "$@" >> "$EV"; }
            install() { local a; for a in "$@"; do :; done; mkdir -p "$a"; }
            # psql stand-in: refuses, as psql does, without a cluster and a database.
            runuser() {
                local c="" d="" prev=""
                for a in "$@"; do
                    [[ "$prev" == --cluster ]] && c="$a"; [[ "$prev" == -d ]] && d="$a"; prev="$a"
                done
                [[ -n "$c" && -n "$d" ]] || { echo "Error: No cluster specified with --cluster"; return 1; }
                printf "%s" "$ROWS"
            }
            '"$fn"'
            retire_agent_runners
        ' _ "$ar" "$rows" "$WORK/events" "$WORK/log" "$WORK/persistence.env"
    }
    envfile_get="$(awk '/^_envfile_get\(\) \{/,/^\}/' "$LIBSH")"
    printf 'SMART_NODE_PG_CLUSTER=16/smartnode\nSMART_NODE_PG_CORE_DB=smartnode\n' > "$WORK/persistence.env"
    A="$WORK/node/agents"
    mkdir -p "$A/_template" "$A/alex" "$A/sysadmin" "$A/.archive"
    for f in "$A/_template/run.sh" "$A/alex/run.sh" "$A/alex/run.sh.pre-20260904174439.bak" "$A/sysadmin/run.sh"; do
        printf '#!/bin/sh\necho wake\n' > "$f"; chmod 0755 "$f"
    done
    echo "keep" > "$A/alex/CLAUDE.md"
    : > "$WORK/events"; : > "$WORK/log"
    run_retire "$A" $'alex-wake\narchitect-wake\nsysadmin-wake'

    left="$(find "$A" -path "$A/.archive" -prune -o -name 'run.sh*' -print)"
    arch="$(find "$A/.archive" -name 'run.sh*' -type f 2>/dev/null | sort)"
    execs="$(find "$A/.archive" -name 'run.sh*' -type f -perm /111 2>/dev/null)"
    if [[ -z "$left" && "$(wc -l <<<"$arch")" -eq 4 && -z "$execs" && -f "$A/alex/CLAUDE.md" ]]; then
        ok "retire-files: 4 run.sh files (template, agents, .bak) archived, none executable, nothing else touched"
    else
        bad "retire-files: left in agent folders=[${left//$'\n'/ }] archived=$(wc -l <<<"$arch") executable=[${execs//$'\n'/ }]"
    fi

    n_cron="$(grep -c '|cron.deleted|cron|' "$WORK/events")"
    n_file="$(grep -c '|agent.runner_retired|' "$WORK/events")"
    if [[ "$n_cron" -eq 3 && "$n_file" -eq 1 ]] && grep -q 'architect-wake' "$WORK/log"; then
        ok "retire-crons: 3 run.sh crons removed, one event each, plus one event for the files"
    else
        bad "retire-crons: cron events=$n_cron file events=$n_file"
    fi

    B="$WORK/clean/agents"; mkdir -p "$B/_template" "$B/alex"
    : > "$WORK/events"; : > "$WORK/log"
    run_retire "$B" ""
    [[ ! -s "$WORK/events" && ! -d "$B/.archive" ]] \
        && ok "retire-quiet: nothing to retire -> nothing moved, no event" \
        || bad "retire-quiet: a clean node still got an archive or events"
fi

# ---------------------------------------------------------------- no-converge
if grep -q 'converge_agent_runners' "$CAPI"; then
    bad "no-converge: core-api.sh still converges run.sh into agent folders"
elif ! awk '/^lay_srv_skeleton\(\) \{/,/^\}/' "$CAPI" | grep -q '^ *retire_agent_runners$'; then
    bad "no-converge: the skeleton step does not call retire_agent_runners"
else
    ok "no-converge: nothing re-creates run.sh; the skeleton step retires it"
fi

# ---------------------------------------------------------------- no-wake-cron
if grep -Eq 'run\.sh"|ensure_wake_cron|WAKE_ENABLED' "$TREE/bootstrap/onboarding-seed.sh"; then
    bad "no-wake-cron: the onboarding seed can still create a cron that runs run.sh"
else
    ok "no-wake-cron: the onboarding seed has no wake cron"
fi

# ---------------------------------------------------------------- no-instructions
hits="$(grep -rnE '(sudo -u [^ ]+ -H )?/srv/agents/[^ ]*/run\.sh|agents/<[a-z]+>/run\.sh|Every `run\.sh` MUST|entry point, run by cron' \
        "$TREE/docs" "$SKEL" 2>/dev/null)"
[[ -z "$hits" ]] \
    && ok "no-instructions: no shipped doc or skeleton file tells anyone to run run.sh" \
    || bad "no-instructions: ${hits:0:200}"

echo
if ((FAILED)); then echo "FAILED: $FAILED"; exit 1; fi
echo "ALL PASS"
