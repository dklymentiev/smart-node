#!/usr/bin/env bash
#
# The "agent is working" indicator is one shimmering line, built once.
#
# WHAT IT REPLACED. Three bouncing dots and "working 12s", rebuilt through
# innerHTML every second. The owner (2026-09-10): the dots and the word read as
# dated; he asked for a running gradient, the way AI chat apps show a turn in
# flight. A CSS sweep on an element that is rebuilt every second restarts every
# second, so the rebuild had to go with the dots.
#
# THE PROPERTIES:
#   1. the element is built once; a tick or a new label changes text only
#   2. the words: Thinking / Using <tool> / Reconnecting
#   3. seconds appear only once a turn has run 10 s
#   4. the line shimmers (background-clip: text sweep), and reduced motion
#      turns the sweep off
#
# thinkingIndicator() is run for real in node, against a fake element and a
# fake clock.
#
#   tests/studio-thinking-indicator.test.sh                  # tree, PASS
#   tests/studio-thinking-indicator.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "Studio: thinking indicator (git:$BASELINE)"
else echo "Studio: thinking indicator (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/studio/public/next2/js/app.js   > "$D/app.js"
read_src companions/studio/public/ui/components.css > "$D/components.css"
for f in app.js components.css; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# ------------------------------------------------------------ 4. the styles
# The awk output is taken first: under pipefail, awk | grep -q fails whenever
# grep exits on its first match and awk then dies of SIGPIPE, which made this
# check fail at random inside run-source.sh (seen 2026-09-24, 1.12.46).
REDUCED="$(grep 'prefers-reduced-motion: reduce' "$D/components.css")"
if grep -q '@keyframes sn-think-sweep' "$D/components.css" \
   && grep -q 'background-clip:text' "$D/components.css" \
   && grep -q '\.sn-think__label{ animation:none;' <<<"$REDUCED"; then
    ok "the line shimmers, and reduced motion turns the sweep off"
else
    bad "the line shimmers, and reduced motion turns the sweep off"
fi

# ------------------------------------------------------- 1-3. run it for real
if ! command -v node >/dev/null 2>&1; then
    bad "node is available to run thinkingIndicator"
else
    while IFS= read -r line; do
        case "$line" in
            ok*)   ok "${line#ok }";;
            FAIL*) bad "${line#FAIL }";;
        esac
    done < <(node - "$D/app.js" <<'JS'
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const at = src.search(new RegExp('^function ' + name + '\\(', 'm'));
  if (at < 0) return null;
  let i = src.indexOf('{', at), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) return src.slice(at, i + 1);
  }
  return null;
}
const say = (good, name) => console.log((good ? 'ok ' : 'FAIL ') + name);
const escLine = (src.match(/^const esc = .*$/m) || [''])[0];
const fn = grab('thinkingIndicator');
if (!fn) { say(false, 'thinkingIndicator exists'); process.exit(0); }

let now = 1000000;
const FakeDate = { now: () => now };
const nodes = { '.sn-think__label': { textContent: '' }, '.sn-think__time': { textContent: '' } };
let writes = 0, html = '';
const el = {
  set innerHTML(v) { writes++; html = v; },
  get innerHTML() { return html; },
  querySelector: (s) => nodes[s] || null,
};
let tick = null;
const make = new Function('setInterval', 'clearInterval', 'Date',
  escLine + '\n' + fn + '\nreturn thinkingIndicator;');
const ti = make((f) => { tick = f; return 1; }, () => {}, FakeDate);
const t = ti(el);
const label = () => nodes['.sn-think__label'].textContent;
const time = () => nodes['.sn-think__time'].textContent;

say(label() === 'Thinking' && time() === '', 'a fresh turn reads "Thinking", with no seconds');
now += 4000; if (tick) tick();
t.setLabel('working: Read');
say(label() === 'Using Read', 'a tool in flight reads "Using <tool>"');
t.setLabel('reconnecting');
say(label() === 'Reconnecting', 'a dropped connection reads "Reconnecting"');
now += 8000; if (tick) tick();
// The seconds carry their own no-break space: without it the label and
// the number read as one word, "Thinking19s".
say(time() === '\u00a012s', 'seconds appear once the turn has run 10 s, after a no-break space');
say(writes === 1, 'the element is built once; ticks and labels change text only (built ' + writes + 'x)');
JS
)
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
