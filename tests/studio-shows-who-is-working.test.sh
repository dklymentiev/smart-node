#!/usr/bin/env bash
#
# The Studio shows which agents are working.
#
# WHY THIS EXISTS. The owner of a node in the field asked "how do I see who is busy"
# and nothing in the Studio answered: the sidebar dot said only enabled or
# disabled. core-api has answered the question at GET
# /api/agents/state. The Studio now reads it and marks the avatar.
#
# THE PROPERTIES:
#   1. the Studio proxies /api/agents/state, declared before /api/agents/:slug
#   2. the page reads it again on the live stream's events (no polling) and
#      paints the sidebar rows and the agent cards
#   3. it repaints after either list is rendered, so a re-render does not wipe it
#   4. busy turns a ring; stale and broken are still marks
#
#   tests/studio-shows-who-is-working.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SERVER="$REPO/companions/studio/src/server.js"
APP="$REPO/companions/studio/public/next2/js/app.js"
CSS="$REPO/companions/studio/public/next2/css/app.css"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
check() { if eval "$1"; then ok "$2"; else bad "$2"; fi; }

echo "Studio: who is working"

state_line=$(grep -n "app.get('/api/agents/state'" "$SERVER" | head -1 | cut -d: -f1)
slug_line=$(grep -n "app.get('/api/agents/:slug'," "$SERVER" | head -1 | cut -d: -f1)
check '[[ -n "$state_line" && -n "$slug_line" && "$state_line" -lt "$slug_line" ]]' \
      "the Studio proxies /api/agents/state before /api/agents/:slug"

has() { grep -qF -- "$1" "$2"; }
Q="'"
if has "fetch(${Q}/api/agents/state${Q}" "$APP"; then ok "the page reads /api/agents/state"; else bad "the page reads /api/agents/state"; fi
if has "soon('state', loadAgentState);" "$APP" && has "loadDesks(); loadAgentState();" "$APP"; then ok "it reads again on the live stream's events and on a resync"; else bad "it reads again on the live stream's events and on a resync"; fi
if has "querySelectorAll(${Q}.agent[data-slug], .acard[data-slug]${Q})" "$APP"; then ok "it paints the sidebar rows and the agent cards"; else bad "it paints the sidebar rows and the agent cards"; fi
n_paint=$(grep -c "^ *paintAgentState();$" "$APP")
check '[[ "$n_paint" -ge 2 ]]' "it repaints after the sidebar and the grid are rendered ($n_paint)"
if has "s.stale ? ${Q}stale${Q} : ${Q}busy${Q}" "$APP"; then ok "a busy row with an expired lease reads as stale, not busy"; else bad "a busy row with an expired lease reads as stale, not busy"; fi

if has "data-state-slug=" "$APP" && has "data-now-row=" "$APP" && has "data-now-time" "$APP" && has ".ovd__tl--now .ovd__tlt.is-live { color: var(--sn-ink); animation: sn-pulse" "$CSS"; then ok "the Live timeline opens with the turn, its start time pulsing"; else bad "the Live timeline opens with the turn, its start time pulsing"; fi
if ! has "kv(${Q}Now${Q}" "$APP"; then ok "there is no separate Now field"; else bad "there is no separate Now field"; fi

if has "el.classList.toggle(${Q}is-busy${Q}, st === ${Q}busy${Q});" "$APP"; then ok "busy, stale and broken are exposed as classes for the design system to style"; else bad "busy, stale and broken are exposed as classes for the design system to style"; fi
if has ".agent.is-broken .sn-av__st" "$CSS"; then ok "broken keeps a red dot"; else bad "broken keeps a red dot"; fi

tl_line=$(grep -n "app.get('/api/agents/:slug/timeline'" "$SERVER" | head -1 | cut -d: -f1)
if [[ -n "$tl_line" ]]; then ok "the Studio serves an agent timeline"; else bad "the Studio serves an agent timeline"; fi
if has "actor=\${encodeURIComponent(${Q}agent:${Q} + slug)}" "$SERVER" && has "entity_type=agent&entity_id=" "$SERVER"; then ok "the timeline asks core-api for this agent's events, not a window of the node"; else bad "the timeline asks core-api for this agent's events, not a window of the node"; fi
if has "${Q}planner.comment${Q}: ${Q}Commented on task${Q} + on" "$APP" && has "Woken by the runner" "$APP"; then ok "tool calls and wakes are put in words"; else bad "tool calls and wakes are put in words"; fi
if has "function tlCollapse(" "$APP"; then ok "repeats in a row collapse into one line with a count"; else bad "repeats in a row collapse into one line with a count"; fi
if has "const lastSrc = own && own.length ? own : events;" "$APP"; then ok "last action counts everything the agent did, not only messages"; else bad "last action counts everything the agent did, not only messages"; fi
if has "function nowTask(" "$APP" && has "${Q}task #${Q} + t.id" "$APP"; then ok "Now names the task the agent is on"; else bad "Now names the task the agent is on"; fi

ls_line=$(grep -n "app.get('/api/agents/last-seen'" "$SERVER" | head -1 | cut -d: -f1)
if [[ -n "$ls_line" && -n "$slug_line" && "$ls_line" -lt "$(grep -n "app.get('/api/agents/:slug'," "$SERVER" | head -1 | cut -d: -f1)" ]]; then ok "last-seen is declared before /api/agents/:slug"; else bad "last-seen is declared before /api/agents/:slug"; fi
if has "data-seen-slug=" "$APP" && has "function paintLastSeen(" "$APP"; then ok "the sidebar shows when each agent last acted"; else bad "the sidebar shows when each agent last acted"; fi

if has "el.textContent = ${Q}working now${Q};" "$APP" && has ".agent .agent__seen.is-live { color: var(--sn-ink); animation: sn-pulse" "$CSS"; then ok "the sidebar says working now and pulses while the agent works"; else bad "the sidebar says working now and pulses while the agent works"; fi

if has "function tlLinkTasks(" "$APP" && has "target=\"_blank\" rel=\"noopener\">#\$1</a>" "$APP" && has "return esc(text).replace(" "$APP"; then ok "task numbers in the timeline open the task in a new tab, escaped first"; else bad "task numbers in the timeline open the task in a new tab, escaped first"; fi

if command -v node >/dev/null 2>&1; then
    check 'node --check "$APP" && node --check "$SERVER"' "app.js and server.js parse"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
