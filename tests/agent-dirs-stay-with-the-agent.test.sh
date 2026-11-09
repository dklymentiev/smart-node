#!/usr/bin/env bash
# An install or update must leave each agent's logs/, state/ and work/ owned by the agent. The core-api stage chowns the
# whole agents root to the service user; the agents' own folders are handed back right after, to the agent whose
# folder it is (an agent that has an OS user), and nobody else's.
set -u
cd "$(dirname "$0")/.."
S=companions/core-api.sh; fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
chown_line=$(grep -n 'chown -R "$SERVICE_USER":"$SMART_NODE_GROUP" "$SN_AGENTS_ROOT"' $S | head -1 | cut -d: -f1)
back_line=$(grep -n 'chown -R "agent-$agent_slug":"$SMART_NODE_AGENT_GROUP" "$agent_dir$agent_sub"' $S | head -1 | cut -d: -f1)
chk "the agents' folders are handed back" "[ -n \"$back_line\" ]"
chk "and after the chown of the whole root, not before it" "[ -n \"$chown_line\" ] && [ -n \"$back_line\" ] && [ \"$back_line\" -gt \"$chown_line\" ]"
chk "only logs, state and work" "grep -q 'for agent_sub in logs state work; do' $S"
chk "only for an agent that has an OS user" "grep -q 'id -u \"agent-\$agent_slug\"' $S"
chk "the stage still parses" "bash -n $S"

# the same loop, run for real on a throw-away tree, as a user that exists, to show it does what it says
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/root/real/logs" "$T/root/real/work" "$T/root/_template/logs"
ME=$(id -un); MYGRP=$(id -gn)
# a fake `id` and `chown` that record, so the loop runs without root and without a real agent user
cat > "$T/id" <<EOF
#!/usr/bin/env bash
[ "\$2" = "agent-real" ] && exit 0 || exit 1
EOF
cat > "$T/chown" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "$T/log"
EOF
chmod +x "$T/id" "$T/chown"
body=$(sed -n '/local agent_dir agent_slug agent_sub/,/^    done$/p' $S)
SN_AGENTS_ROOT="$T/root" SMART_NODE_AGENT_GROUP=smart-node-agents PATH="$T:$PATH" bash -c "f() {
$body
}; f" 2>/dev/null
got=$(sort "$T/log" 2>/dev/null | tr '\n' ';')
chk "the loop hands back real/logs and real/work, and nothing of _template" "[ \"$got\" = \"-R agent-real:smart-node-agents $T/root/real/logs;-R agent-real:smart-node-agents $T/root/real/work;\" ]"
[ $fail = 0 ] && echo "PASS: agent dirs stay with the agent" || { echo FAIL; exit 1; }
