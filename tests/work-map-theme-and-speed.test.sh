#!/usr/bin/env bash
# Work Map follows the shared theme (no hard-coded surface or ink colours) and does not decode the whole event log.
set -u
cd "$(dirname "$0")/.."
C=companions/studio/public/next2/css/work-map.css; W=companions/core-api/routes/work.py; fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
rules=$(sed -n '/^\.workmapv \*{box-sizing/,$p' "$C")
chk "no hex colour is left in the view's rules" "! printf '%s' \"\$rules\" | grep -Eq '#[0-9a-fA-F]{3,8}\\b'"
chk "surface and ink come from the kit" "grep -q -- '--bg:var(--sn-bg); --panel:var(--sn-surface)' $C"
chk "the dark theme has its own data hues" "grep -q '^\\[data-theme=\"dark\"\\] \\.workmapv' $C"
chk "the event read leaves out non-agent noise" "grep -q \"actor NOT LIKE 'agent:%%' AND event_type IN\" $W"
chk "the total, last time and raw tail still describe the whole log" "grep -q 'raw_rows' $W && grep -q 'max(ts) AS last' $W"
chk "planner log reads go out together" "grep -q 'ThreadPoolExecutor(max_workers=_LOG_WORKERS)' $W"
chk "the event reads start before, and run beside, the planner reads" "grep -q 'rows_f = io.submit(read_agent_events)' $W"
chk "the planner task list is walked in batches of pages" "grep -q 'batch = 500, 6' $W || grep -q 'page_size, batch = 500, 6' $W"
python3 -m py_compile "$W" && echo "  ok   work.py compiles" || fail=1
[ $fail = 0 ] && echo "PASS: work map theme and speed" || { echo FAIL; exit 1; }
