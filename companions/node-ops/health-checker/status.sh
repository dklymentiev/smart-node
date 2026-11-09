#!/usr/bin/env bash
# status.sh -- rebuild both reports about this node, from the node itself.
#
# WHAT IT DOES NOT DO: publish. Generating is reading and is safe to repeat;
# publishing replaces a document somebody may be reading, and it goes through the
# document MCP as a deliberate act, never from a script. This prints the guids to
# update and stops there.
#
# Everything below reads. Nothing here changes the node.
set -u

OUT="${1:-/tmp/status-$(date +%Y%m%d-%H%M)}"
mkdir -p "$OUT"
T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run() {  # run <output-file> <script...>
  local dest="$1"; shift
  if timeout 600 python3 "$@" > "$dest" 2>"$dest.err"; then
    printf '  ok    %-26s %7s bytes\n' "$(basename "$dest")" "$(wc -c <"$dest")"
  else
    printf '  FAIL  %-26s %s\n' "$(basename "$dest")" "$(head -c 160 "$dest.err")"
  fi
}

echo "Rebuilding into $OUT"
echo
echo "Agents: what they have, and whether it works"
run "$OUT/a1-tools.html"      "$T/agent-tools-grid.py"
run "$OUT/a2-channels.html"   "$T/agent-channels-grid.py"
run "$OUT/a3-rules-life.html" "$T/agent-state-grid.py" both
run "$OUT/a4-cost.html"       "$T/agent-state-grid.py" cost

# The four grids are one document, in this order: equipped, permitted, governed,
# alive and costing. Read separately they invite the wrong conclusion.
cat "$OUT"/a1-tools.html "$OUT"/a2-channels.html \
    "$OUT"/a3-rules-life.html "$OUT"/a4-cost.html > "$OUT/agents-and-tools.html" 2>/dev/null

echo
echo "Node status"
run "$OUT/node-status.html" "$T/node-status.py"

echo
echo "Done. Two documents are ready to publish:"
echo "  $OUT/agents-and-tools.html  -> update one persistent agents-and-tools report"
echo "  $OUT/node-status.html       -> create a dated node-status-$(date +%Y-%m-%d) report"
echo
echo "The agents report is one living document and is updated in place, so the"
echo "link stays good. The node status is a reading taken on a day and gets a new"
echo "document each time, so two days can be compared."
