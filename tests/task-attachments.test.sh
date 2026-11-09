#!/usr/bin/env bash
#
# A file can hang from a planner task, readable by whoever may read the task.
#
# WHAT WAS MISSING. A person answering a hitl question may attach files (a
# photo, a price list). The answer lands on the task, but the files had no
# place on the node: the attachment store keyed every file to a message or a
# desk, and nothing in core-api took a file for a task.
#
# THE PROPERTIES:
#   1. schema: task_id on sn_core.message_attachments (fresh and installed
#      nodes), an index by task, the one-parent CHECK counting three parents,
#      and an installed node's two-parent form dropped so the list re-adds it
#   2. routes: POST and GET /api/planner/api/v1/tasks/{id}/attachments, gated
#      on planner.proxy, refusing a name with no proof, checking the task
#      exists and that the caller may read it
#   3. main.py includes the router before the planner proxy
#   4. the attachment API decides a task file by the task, first
#   5. core-api.sh lays tasks/ for itself, 0750, recorded in the manifest
#
#   tests/task-attachments.test.sh                  # tree, PASS
#   tests/task-attachments.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "task attachments (git:$BASELINE)"
else echo "task attachments (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src bootstrap/B02-sn-core-schema.sql                > "$D/b02.sql"
read_src companions/core-api/routes/task_attachments.py  > "$D/task_attachments.py"
read_src companions/core-api/routes/attachments.py       > "$D/attachments.py"
read_src companions/core-api/main.py                     > "$D/main.py"
read_src companions/core-api.sh                          > "$D/core-api.sh"
for f in b02.sql attachments.py main.py core-api.sh; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# ----------------------------------------------------------------- 1. schema
TABLE=$(awk '/^CREATE TABLE IF NOT EXISTS sn_core.message_attachments /,/^\);/' "$D/b02.sql")
if grep -qE '^ +task_id +bigint,' <<<"$TABLE" \
   && grep -q 'ALTER TABLE sn_core.message_attachments ADD COLUMN IF NOT EXISTS task_id bigint;' "$D/b02.sql" \
   && grep -q 'CREATE INDEX IF NOT EXISTS message_attachments_task_id_idx ON sn_core.message_attachments USING btree (task_id) WHERE (task_id IS NOT NULL);' "$D/b02.sql" \
   && grep -q 'message_attachments_one_parent_chk CHECK (num_nonnulls(message_id, desk_id, task_id) = 1)' "$D/b02.sql" \
   && grep -q "pg_get_constraintdef(oid) NOT LIKE '%task_id%'" "$D/b02.sql"; then
    ok "a row may hang from a task, with exactly one parent of three, on fresh and installed nodes"
else
    bad "a row may hang from a task, with exactly one parent of three, on fresh and installed nodes"
fi

# ----------------------------------------------------------------- 2. routes
T="$D/task_attachments.py"
if [[ -s "$T" ]] \
   && grep -q '@router.post("/api/planner/api/v1/tasks/{task_id}/attachments", status_code=201,' "$T" \
   && grep -q '@router.get("/api/planner/api/v1/tasks/{task_id}/attachments",' "$T" \
   && [[ $(grep -c 'dependencies=\[Depends(requires("planner.proxy"))\]' "$T") -eq 2 ]] \
   && grep -q 'if principal.strength == "asserted":' "$T" \
   && grep -q 'if not await run_in_threadpool(_task_exists, task_id):' "$T" \
   && grep -q 'if not await run_in_threadpool(may_read_task, principal, task_id):' "$T" \
   && grep -q 'if not may_read_task(principal, task_id):' "$T"; then
    ok "store and list a task's files: proved callers who may read the task"
else
    bad "store and list a task's files: proved callers who may read the task"
fi

# ------------------------------------------------------------ 3. main order
mine=$(grep -n '^app.include_router(task_attachments.router)' "$D/main.py" | cut -d: -f1)
proxy=$(grep -n '^app.include_router(planner.router)' "$D/main.py" | cut -d: -f1)
if [[ -n "$mine" && -n "$proxy" && "$mine" -lt "$proxy" ]]; then
    ok "main.py answers the task paths before the planner proxy"
else
    bad "main.py answers the task paths before the planner proxy"
fi

# ------------------------------------------------------- 4. attachment API
if grep -q 'if row\["task_id"\] is not None:' "$D/attachments.py" \
   && grep -q 'if not may_read_task(principal, row\["task_id"\]):' "$D/attachments.py" \
   && grep -q 'elif row\["message_id"\] is None:' "$D/attachments.py" \
   && grep -q 'a.created_at, a.task_id' "$D/attachments.py"; then
    ok "a task file is decided by its task, before the desk and message rules"
else
    bad "a task file is decided by its task, before the desk and message rules"
fi

# ----------------------------------------------------------- 5. installer
if grep -q 'install -d -m 0750 -o "$SERVICE_USER" -g "$SMART_NODE_GROUP" /srv/data/attachments/tasks' "$D/core-api.sh" \
   && grep -q 'manifest_record dir /srv/data/attachments/tasks' "$D/core-api.sh"; then
    ok "core-api.sh lays tasks/ for itself, recorded in the manifest"
else
    bad "core-api.sh lays tasks/ for itself, recorded in the manifest"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
