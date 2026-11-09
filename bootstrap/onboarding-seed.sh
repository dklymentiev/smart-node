#!/usr/bin/env bash
# onboarding-seed.sh -- make a fresh node configure itself.
#
# Runs INSIDE the target node as root, at the tail of Phase 2 (after the
# core-api, agent-runtimes and tracking companions are up). Idempotent and
# fail-open: onboarding is a convenience trigger, never a reason to fail an
# install (every step logs and continues; the script always exits 0).
#
# It wires the node's own autonomous task-loop to run onboarding once:
#
#   1. PROVISION the `sysadmin` agent (owner-only system agent) via core-api,
#      from the vendored sysadmin specialist. Skips if it already exists.
#   2. (retired) The per-agent wake cron is gone: the Runner is the only
#      waker; the per-agent run.sh is gone. The Runner wakes the agent
#      from the task seeded in step 3.
#   3. SEED one Planner task "Configure this business" assigned to sysadmin.
#      Skips if such a task already exists.
#
# The loop then closes on its own: seeding the task fires the planner's
# assignee hook -> core-api raises the agent's wake flag -> the Runner launches
# it -> claude reads the Planner -> finds the open task -> runs the onboarding flow
# (greet -> complete auth if deferred [W1] -> interview via human_ask ->
# author tenant_profile [W2 provision API] -> projector T0-T7 -> health-check).
#
# Env (all have install-time defaults):
#   SN_CORE_API_SOCKET  core-api Unix socket (default /run/smart-node/api/core-api.sock)
#   SN_ONBOARD_FORCE_RESEED  1 to replace baseline grants intentionally; ordinary
#                            runs merge and never narrow an established agent
#   SN_AGENTS_ROOT     agent home root (default /srv/agents)
#   SN_SPECIALIST_MD   path to sysadmin specialist (default vendored skeleton)
set -uo pipefail

CORE_SOCKET="${SN_CORE_API_SOCKET:-/run/smart-node/api/core-api.sock}"
CORE="http://localhost"
FORCE_RESEED="${SN_ONBOARD_FORCE_RESEED:-0}"
AGENTS_ROOT="${SN_AGENTS_ROOT:-/srv/agents}"
SPEC_MD="${SN_SPECIALIST_MD:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/srv-skeleton/agents/_specialists/configurator.md}"

# Agents are known by their names, not their roles (slug = name; a node may
# have renamed foreman to john). The role is the word under the name.
AGENT_SLUG="john"
# Set by provision_agent when this node already has a setup agent, to the slug
# it actually carries. Empty means the seed created one, or could not look.
SETUP_AGENT_FOUND=""
TASK_TITLE="Configure this business"

# A TCP Remote-User header only asserts the owner. The installer runs this
# script as root, which core-api resolves to its configured operator over the
# Unix socket using SO_PEERCRED. No request may fall back to TCP.
if [[ -z "$CORE_SOCKET" || ! -S "$CORE_SOCKET" ]]; then
    printf '[onboarding-seed] ERROR: core-api Unix socket is unavailable: %s\n' "$CORE_SOCKET"
    exit 1
fi

say() { printf '[onboarding-seed] %s\n' "$*"; }

# Per-run scratch for the curl response bodies below. They used to be four
# fixed paths in a world-writable directory -- onboard_agent.out and friends,
# straight in /tmp -- written by a script that runs as root. Anything on the node that
# created one of those first would make this step fail on a path it could not
# overwrite, which is the state overwrite this guard prevents; the installer's own staging
# directory is per-run for the same reason.
SEED_TMP=$(mktemp -d /tmp/onboard-seed.XXXXXXXX 2>/dev/null) || SEED_TMP=""
[[ -n "$SEED_TMP" ]] || { say "WARN: could not make a scratch dir -- falling back to \$PWD"; SEED_TMP="."; }
trap '[[ "$SEED_TMP" == /tmp/onboard-seed.* ]] && rm -rf -- "$SEED_TMP"' EXIT

# curl to core-api using the kernel-proved installer identity.
capi() { # METHOD PATH [JSON]
    local method="$1" path="$2" data="${3:-}"
    if [[ -n "$data" ]]; then
        curl -sS -m 15 --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
            -X "$method" "$CORE$path" -d "$data"
    else
        curl -sS -m 15 --unix-socket "$CORE_SOCKET" -X "$method" "$CORE$path"
    fi
}

# ---------------------------------------------------------------- 1. agent
# The first agent thinks with the runtime this node was installed with (Claude
# Code, Flint, or both; companions/*.manifest.json "runtime"). Claude Code comes
# with its catalog rows; Flint has a model only once a provider is set on the
# first-run page, which then moves the agent onto it. Until then "none": an agent
# that waits for its model, not one on a runtime the node does not have.
STATE_DIR="${SMART_NODE_STATE_DIR:-/var/lib/smart-node/stages}"
stage_done() { grep -qx 'status=done' "$STATE_DIR/$1.state" 2>/dev/null; }
seed_model() {
    if stage_done agent-runtimes; then echo "claude-cli/sonnet"; return; fi
    capi GET /api/models 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    rows = d if isinstance(d, list) else d.get("models", [])
    rows = [m for m in rows if m.get("modality") == "chat" and str(m.get("id", "")).startswith("flint-cli/")]
    rows.sort(key=lambda m: not m.get("is_default"))
    print(rows[0]["id"] if rows else "none")
except Exception:
    print("none")
'
}

provision_agent() {
    local existing_json found_slug
    existing_json=$(capi GET /api/agents 2>/dev/null)
    # Look up by capability  when the column is present,
    # falling back to role for nodes that pre-date the capability column.
    # Never by slug: the owner may rename the setup agent in the panel, changing
    # its slug while capability="configurator" remains stable (older nodes: role
    # "Foreman", slug "foreman"). Checking by slug misses a renamed agent, then creates a second
    # one with the same actor identity. Repeated updates would keep adding
    # duplicate configurators to an established node.
    found_slug=$(python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    agents = d if isinstance(d, list) else d.get("agents", [])
    for a in agents:
        if a.get("capability") == "configurator" or a.get("role") == "Foreman":
            print(a.get("slug", ""))
            break
except Exception:
    pass
' 2>/dev/null <<<"$existing_json")
    if [[ -n "$found_slug" ]]; then
        SETUP_AGENT_FOUND="$found_slug"
        say "setup agent already provisioned as '$found_slug' (capability=configurator) -- skip."
        return 0
    fi
    if [[ ! -r "$SPEC_MD" ]]; then
        say "WARN: configurator specialist not found ($SPEC_MD) -- cannot provision agent; skipping."
        return 0
    fi
    local model; model=$(seed_model)
    say "provisioning agent '$AGENT_SLUG' (model $model, mesh workspace sysadmin)."
    # Reference the specialist by slug -- srv-skeleton's seed-specialists.py has
    # already seeded `configurator` from _specialists/configurator.md, so passing
    # specialist_body here would collide on the unique (tenant,slug) index.
    # The node's first agent is the CONFIGURATOR: it builds the business (interview
    # -> business profile -> creates the business's agents born narrow). `sysadmin` is a
    # separate narrow node-health specialist, not this agent.
    local payload
    # tenant_id is NOT optional: the gateway copies the agent's tenant onto the chat
    # session row, and ai_sessions.tenant_id is NOT NULL -- seed it without a tenant and
    # every agent-mode chat dies with a 500 (NotNullViolation) while the cron agent, which
    # never touches that table, keeps working.
    payload=$(python3 - "$AGENT_SLUG" "$model" <<'PY'
import json, sys
slug, model = sys.argv[1], sys.argv[2]
print(json.dumps({
  "tenant_id": 1,
  "slug": slug, "title": "John", "role": "Configurator",
  "capability": "configurator",   # immutable product-role marker
  "model": model, "enabled": True, "mesh_workspace": slug,  # <runtime>/<model>, or none
  "os_user": "agent-" + slug, "specialist_slug": "configurator",
}))
PY
)
    local code
    # The socket proves root, which core-api maps to the node's configured
    # owner for this call, so the name admin-runner's owner gate receives is
    # one it can resolve and OS user creation is authorised.
    code=$(curl -sS -m 20 -o "$SEED_TMP/onboard_agent.out" -w '%{http_code}' \
        --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
        -X POST "$CORE/api/agents" -d "$payload" 2>/dev/null)
    if [[ "$code" == 2* ]]; then
        say "agent '$AGENT_SLUG' provisioned (HTTP $code)."
    else
        say "WARN: agent provision returned HTTP $code -- $(head -c 200 "$SEED_TMP/onboard_agent.out" 2>/dev/null). Continuing."
    fi
    rm -f "$SEED_TMP/onboard_agent.out"
}

# ------------------------------------------------------------ 1b. grant tools
# Grant the sysadmin configurator the minimal tool set the onboarding loop needs.
# The grant is DECLARATIVE and self-activating: put_grants renders .mcp.json from
# the intersection of these grant keys and the node's ENABLED mcp_servers, and
# links agent_tools from the intersection with the tools registry. A grant for a
# server/tool not yet registered on the node is skipped cleanly (agent.yaml still
# lists it) and lights up automatically once that server is registered -- so a
# fresh node renders `doci` (MCP) and links `planner` (an api tool registered by
# the tracking companion, which installs before this seed -- so its usage_md
# contract is composed into CLAUDE.md here); `mesh`/`human` activate once
# their servers come online. Without this step the agent ships with tools:[] and
# NO .mcp.json, i.e. no memory / planner / interview.
grant_tools() {
    say "granting sysadmin its onboarding tool set (declarative; renders what is registered)."
    local payload
    payload=$(python3 <<'PY'
import json
# key = tool/mcp-server slug; value = capability mask "rwd" (read/write/delete).
print(json.dumps({"grants": {
    "bash":    "rwd",   # builtin: curl provision REST, run node commands
    "fetch":   "rw-",   # builtin: read URLs
    "fs":      "rwd",   # builtin: read/write agent state
    "doci":    "rwd",   # docs memory (registered on a base node today)
    "mesh":    "rwd",   # semantic memory / worklog (activates in T3)
    "planner": "rw-",   # read/advance the "Configure this business" task (T3)
    "human":   "rw-",   # owner interview: human_ask / human_notify (T3 + channel)
    "messaging": "rw-", # write to a colleague / work own inbox (MCP from dispatcher)
    "node-admin": "rw-",# create+grant+wake narrow business sub-agents (the contract)
    "comm":    "rw-",   # the outside world: read the messages addressed to this
                        # agent on any written channel, answer them in-thread.
                        # The communication companion installs AFTER this seed, so
                        # the grant lands now and activates when the tool registers
                        # -- the same way mesh and human do. Missing until
                        # 2026-08-03 only because the tool was never registered.
}}))
PY
)
    local code grants_url="$CORE/api/agents/$AGENT_SLUG/grants?merge=true"
    if [[ "$FORCE_RESEED" == "1" ]]; then
        grants_url="$CORE/api/agents/$AGENT_SLUG/grants"
        say "force-reseed requested: replacing the setup agent grant baseline."
    fi
    code=$(curl -sS -m 15 -o "$SEED_TMP/onboard_grants.out" -w '%{http_code}' \
        --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
        -X PUT "$grants_url" -d "$payload" 2>/dev/null)
    # core-api refuses the whole set when one name is not on this node (a module
    # not installed: planner, mesh, comm...). The agent then got nothing, not even
    # the inbox reply a reminder asks for, and a reminder fell silent (2026-10-30).
    # Grant what the node has and name the rest in the log.
    if [[ "$code" == 400 ]] && grep -q 'unknown tool grant' "$SEED_TMP/onboard_grants.out" 2>/dev/null; then
        local missing
        missing=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["detail"].split(":",1)[1].strip())' \
            "$SEED_TMP/onboard_grants.out" 2>/dev/null)
        say "not on this node yet, granted later: $missing"
        payload=$(python3 -c '
import json, sys
p = json.loads(sys.argv[1]); gone = {x.strip() for x in sys.argv[2].split(",")}
p["grants"] = {k: v for k, v in p["grants"].items() if k not in gone}
print(json.dumps(p))' "$payload" "$missing")
        code=$(curl -sS -m 15 -o "$SEED_TMP/onboard_grants.out" -w '%{http_code}' \
            --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
            -X PUT "$grants_url" -d "$payload" 2>/dev/null)
    fi
    if [[ "$code" == 2* ]]; then
        say "sysadmin grants applied (HTTP $code); .mcp.json rendered for registered servers."
    else
        say "WARN: grant tools returned HTTP $code -- $(head -c 200 "$SEED_TMP/onboard_grants.out" 2>/dev/null). Continuing."
    fi
    rm -f "$SEED_TMP/onboard_grants.out"
}

# ---------------------------------------------------------------- 3. seed task
# The planner requires a proved identity for mutations. This root-run seed uses
# core-api's local socket, which identifies the operator through SO_PEERCRED;
# an installer Remote-User header over TCP is only asserted and gets a 403.
# Idempotent; no container exec needed.
seed_task() {
    local base="$CORE/api/planner/api/v1"
    local desc
    desc=$(cat <<'DESC'
Set this node up for its owner. You are the configurator; work from the profile
(the document in Documents) as the source of truth.
1. Greet the owner on their channel. Say who you are and that you will ask a few
   short questions to set the node up. First ask what it is for: a business, the
   owner's personal life (a set of personal assistants), or both. Do not assume a
   business; for personal use, read "business" below as the owner's own life.
2. If Claude auth was deferred at install, ask the owner to complete it
   (Studio -> AI Gateway -> Connect).
3. Interview the owner in a LIVE chat, in business language, one question at a time.
   The owner drives it; a background wake never runs the interview. Ask about:
   who they are and what they sell; where they work; what repeats and brings the
   money; how a customer reaches them; what they promise and what proves it; what
   is never decided without them; who decides when something goes wrong. "We do
   not know yet" is a valid answer. Write what you learn into the business profile.
4. When you understand the business well enough, PROPOSE a fitting set of agents
   in business language -- for each agent: what it does, what it must not do, and
   why you propose it, tied to a fact the owner told you. Propose only what this
   version of the node can actually do; if something would need a capability the
   node does not have, say so plainly instead of promising it. Agents are born
   narrow. Do NOT create departments. Nothing is built during this conversation --
   you gather and propose; the owner approves, and building the approved agents is
   a separate step afterwards.
5. Report what you proposed and what is still open, and advance the task.
DESC
)
    say "seeding Planner task '$TASK_TITLE' (assignee $AGENT_SLUG) if absent."

    # Ensure the assignee exists as a planner actor -- tasks.assignee is a FK to
    # actors, and proxy-auth only auto-creates the REQUESTER, not the assignee.
    # Idempotent: a 409 on a duplicate is fine.
    local ac
    ac=$(curl -sS -m 15 -o "$SEED_TMP/pl_actor.out" -w '%{http_code}' \
        --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
        -X POST "$base/actors" \
        -d "{\"id\":\"$AGENT_SLUG\",\"display_name\":\"Configurator\",\"type\":\"agent\"}" \
        2>/dev/null)
    say "planner actor '$AGENT_SLUG': HTTP $ac"

    # Skip create if a task with this title exists in any status: one the owner
    # closed stays closed, or every update hands them the same task again.
    local task_list skip
    task_list=$(curl -sS -m 15 \
        --unix-socket "$CORE_SOCKET" \
        "$base/tasks?assignee=$AGENT_SLUG" 2>/dev/null)
    skip=$(python3 -c "
import json, sys
title = sys.argv[1]
try:
    d = json.loads(sys.stdin.read())
    rows = d if isinstance(d, list) else d.get('tasks', d.get('items', []))
    if any(t.get('title') == title for t in rows):
        print('skip')
except Exception:
    pass
" "$TASK_TITLE" <<<"$task_list")
    if [[ "$skip" == "skip" ]]; then
        say "Planner task already seeded -- skip."
        return 0
    fi

    # Build the POST body through python so newlines in desc are JSON-encoded.
    local body
    body=$(python3 -c "
import json, sys
title, assignee = sys.argv[1], sys.argv[2]
desc = sys.stdin.read()
print(json.dumps({'title': title, 'description': desc, 'assignee': assignee, 'priority': 'high'}))
" "$TASK_TITLE" "$AGENT_SLUG" <<<"$desc")

    local code
    code=$(curl -sS -m 15 -o "$SEED_TMP/pl_task.out" -w '%{http_code}' \
        --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
        -X POST "$base/tasks" -d "$body" 2>/dev/null)
    if [[ "$code" == 2* ]]; then
        local tid
        tid=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('id',''))" \
            <"$SEED_TMP/pl_task.out" 2>/dev/null)
        say "Planner task seeded (id $tid)."
        # Tag it 'manual': a background wake is meant to skip manual tasks (owner-driven,
        # done in a live chat). The retired run.sh did that with planner exclude_tags;
        # the Runner that replaced it does not apply the tag yet.
        if [[ -n "$tid" ]]; then
            local tc
            tc=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' \
                --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
                -X POST "$base/tasks/$tid/tags" \
                -d '{"tag":"manual"}' 2>/dev/null)
            say "tagged task $tid manual: HTTP $tc"
        fi
    else
        say "WARN: task create HTTP $code -- $(head -c 200 "$SEED_TMP/pl_task.out" 2>/dev/null). Seed later from the panel."
    fi
    rm -f "$SEED_TMP/pl_task.out" "$SEED_TMP/pl_actor.out"
}

# create_owner_desk -- bind the owner's chat to the configurator via a desk, so
# the Studio lands on it deterministically instead of leaning on the "first
# enabled agent" fallback. Created AS THE OWNER (super-admin) so owner_user_id is
# the owner, not an asserted installer name. Idempotent (skip if the owner already has a desk);
# fail-open like the rest of the seed.
create_owner_desk() {
    local existing
    existing=$(curl -sS -m 15 --unix-socket "$CORE_SOCKET" "$CORE/api/desks" 2>/dev/null)
    if grep -q '"id"' <<<"$existing"; then
        say "owner desk already exists -- skip."
        return 0
    fi
    local code
    code=$(curl -sS -m 15 -o "$SEED_TMP/onboard_desk.out" -w '%{http_code}' \
        --unix-socket "$CORE_SOCKET" -H "Content-Type: application/json" \
        -X POST "$CORE/api/desks" \
        -d "{\"title\":\"Main\",\"agent_slug\":\"$AGENT_SLUG\"}" 2>/dev/null)
    if [[ "$code" == 201 ]]; then
        say "owner desk 'Main' created, bound to '$AGENT_SLUG' (HTTP $code)."
    else
        say "WARN: owner desk create returned HTTP $code -- $(head -c 200 "$SEED_TMP/onboard_desk.out" 2>/dev/null). Continuing."
    fi
}

# ---------------------------------------------------------------- run
say "onboarding seed -- start (socket=$CORE_SOCKET, agents_root=$AGENTS_ROOT)."
provision_agent
# A setup agent under a slug other than the canonical one means the owner
# renamed it, which only happens on a node that has been running its business
# for a while. Stopping at provision_agent is not enough on such a node: every
# step below still addresses the agent as "foreman". On the 1.12.38 update of
# the node that reported defect 1, provision_agent correctly found the agent
# under its new slug and skipped, and then grant_tools asked core-api to write
# grants for "foreman" (404, agent directory not found) and seed_task created a
# second "Configure this business" task assigned to a planner actor with no
# agent behind it. The owner closes that task by hand after every update.
#
# So an established node is left alone entirely. A fresh node whose seed died
# part-way still resumes: its agent carries the canonical slug, the condition
# below is false, and the remaining steps run as they always have (each one is
# idempotent, which is what makes the resume safe).
if [[ -n "$SETUP_AGENT_FOUND" && "$SETUP_AGENT_FOUND" != "$AGENT_SLUG" ]]; then
    AGENT_SLUG="$SETUP_AGENT_FOUND"
    say "this node is already set up; applying the grant baseline to '$AGENT_SLUG'."
    grant_tools
    say "onboarding task and desk are left as the owner has them."
    exit 0
fi
grant_tools
seed_task
create_owner_desk
say "done -- the onboarding task and the owner desk are in place."
exit 0
