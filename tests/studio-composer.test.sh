#!/usr/bin/env bash
#
# The Studio chat composer: a field that grows, long pastes as chips, and files
# that are stored before a message names them.
#
# WHAT IT REPLACED. A single-line <input>: no line breaks, a long paste became a
# wall of text in a field one line high, and nothing to attach with. The owner
# approved the UI on 2026-09-10; files were initially a preview and are now
# stored in the node's attachment store with the desk as the parent.
#
# THE PROPERTIES:
#   1. the field is a textarea, with a chip tray, a "+" and one file picker that
#      "+" opens directly; no image-only twin (the owner found the file
#      picker takes images, so the Image entry was one click too many)
#   2. Enter sends; Shift+Enter and Ctrl/Cmd+Enter are a new line; an IME that
#      is mid-word keeps its Enter
#   3. a paste over 200 characters or 6 lines becomes a chip
#   4. what reaches the agent is the typed text, each pasted chip as a labelled
#      block, and one line per STORED file; a file that is not stored (still
#      uploading, refused) is never named; the upload goes through Studio with
#      the real type in its own header
#   5. the thread shows a pasted block collapsed and escaped, and the trailing
#      file lines as chips that open the stored file, live and after a reload
#   6. a word with no spaces wraps inside the bubble
#   7. new messages rise into place; reduced motion turns it off; history does
#      not animate
#   8. app.js parses
#
#   tests/studio-composer.test.sh                  # tree, PASS
#   tests/studio-composer.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "Studio composer (git:$BASELINE)"
else echo "Studio composer (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/studio/public/next2/index.html  > "$D/index.html"
read_src companions/studio/public/next2/js/app.js   > "$D/app.js"
read_src companions/studio/public/next2/css/app.css > "$D/app.css"
read_src companions/studio/src/server.js      > "$D/server.js"
for f in index.html app.js app.css server.js; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# Body of a top-level function, flag-based (an awk range closes on its own
# first line).
body_of() {
    awk -v n="$1" 'f && /^(async )?function /{exit} $0 ~ "^(async )?function "n"\\("{f=1} f' "$D/app.js"
}

# ------------------------------------------------------------------ 1. markup
if grep -q '<textarea id="orch"' "$D/index.html" && ! grep -q '<input id="orch"' "$D/index.html"; then
    ok "the field is a textarea"
else
    bad "the field is a textarea -- still a one-line input"
fi
missing=""
for id in cmpTray cmpPeek cmpPlus cmpFile; do
    grep -q "id=\"$id\"" "$D/index.html" || missing="$missing $id"
done
if [[ -z "$missing" ]]; then ok "tray, peek, + and the file picker are in the page"
else bad "tray, peek, + and the file picker are in the page (missing:$missing)"; fi
if grep -q 'accept="image/' "$D/index.html"; then
    bad "one picker, not filtered to images -- an image-only picker is still in the page"
elif grep -A3 "\$('#cmpPlus').addEventListener" "$D/app.js" | grep -qF "\$('#cmpFile').click()"; then
    ok "one picker, not filtered to images, and + opens it directly"
else
    bad "one picker, not filtered to images, and + opens it directly -- + does not open #cmpFile"
fi

# ------------------------------------------------------------------ 2. keys
if grep -q "if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;" "$D/app.js" \
   && grep -q "if (e.ctrlKey || e.metaKey) {" "$D/app.js"; then
    ok "Enter sends; Shift, Ctrl or Cmd with Enter is a line break; an IME keeps its Enter"
else
    bad "Enter sends; Shift, Ctrl or Cmd with Enter is a line break; an IME keeps its Enter"
fi

# ------------------------------------------------------------------ 3. paste
if grep -q '^const CMP_PASTE_CHARS = 200;' "$D/app.js" && grep -q '^const CMP_PASTE_LINES = 6;' "$D/app.js"; then
    ok "a paste over 200 chars or 6 lines becomes a chip"
else
    bad "a paste over 200 chars or 6 lines becomes a chip"
fi

# ------------------------------------------------ 4. what reaches the agent
cc=$(body_of cmpCompose)
if grep -q "for (const p of _cmpItems.filter((it) => it.kind === 'paste'))" <<<"$cc" \
   && grep -q "const files = _cmpItems.filter((it) => it.kind !== 'paste' && it.aid);" <<<"$cc"; then
    ok "the message carries pasted blocks and only files that are stored"
else
    bad "the message carries pasted blocks and only files that are stored"
fi
if grep -q "cmpUpload(it, f)" <<<"$(body_of cmpAddFiles)" \
   && grep -q "'X-Content-Type': file.type" <<<"$(body_of cmpUpload)"; then
    ok "a file is uploaded when added, its real type in its own header"
else
    bad "a file is uploaded when added, its real type in its own header"
fi
if grep -q "app.post('/api/desks/:id/attachments', express.raw(" "$D/server.js" \
   && grep -q "app.get('/api/desks/:id/attachments/:aid/content'" "$D/server.js" \
   && grep -q "app.delete('/api/desks/:id/attachments/:aid'" "$D/server.js"; then
    ok "Studio proxies upload (raw bytes), content (bytes) and delete to core-api"
else
    bad "Studio proxies upload (raw bytes), content (bytes) and delete to core-api"
fi
if grep -q 'if (cmpUploading()) return;' <<<"$(body_of sendChat)"; then
    ok "a message is not sent while a file is still on its way"
else
    bad "a message is not sent while a file is still on its way"
fi

# ------------------------------------------------ 5. rendering, run for real
if grep -q 'isUser ? userHtml(m.content)' "$D/app.js"; then
    ok "history renders user turns with the same renderer as the live bubble"
else
    bad "history renders user turns with the same renderer as the live bubble"
fi
if command -v node >/dev/null 2>&1; then
    if node - "$D/app.js" <<'JS'
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
const line = (re) => (src.match(re) || [null])[0];
const parts = [line(/^const esc = .*$/m), line(/^const CMP_ATTACH_RE = .*$/m),
  grab('cmpFmtSize'), grab('cmpFilesHtml'), grab('userHtml')];
if (parts.includes(null)) { console.log('  FAIL the thread renderer and its file chips exist'); process.exit(1); }
const userHtml = new Function('activeId', parts.join('\n') + '\nreturn userHtml;')('desk-1');
let bad = 0;
const say = (good, name) => { console.log((good ? '  ok   ' : '  FAIL ') + name); if (!good) bad++; };
const aid = '11111111-2222-3333-4444-555555555555';
const out = userHtml('look at this\n\n--- pasted text (7 chars) ---\na<b>c\nd\n--- end of pasted text ---\n'
  + '[attachment: shot.png, image/png, 148 bytes, id ' + aid + ']');
say(out.startsWith('look at this')
  && out.includes('<details class="cmp-pasted"><summary>Pasted text, 7 chars</summary>')
  && out.includes('<pre>a&lt;b&gt;c\nd</pre>') && !out.includes('--- pasted text'),
  'a pasted block renders collapsed and escaped');
say(out.includes('href="/api/desks/desk-1/attachments/' + aid + '/content"')
  && out.includes('<img class="cmp-chip__img"') && out.includes('<b>shot.png</b>') && !out.includes('[attachment:'),
  'a stored image renders as a chip with its thumbnail and opens the file');
const inside = userHtml('--- pasted text (60 chars) ---\n[attachment: x, text/plain, 1 bytes, id ' + aid + ']\n--- end of pasted text ---\nand my words');
say(!inside.includes('cmp-sent'), 'a line inside a pasted block that looks like a file stays text');
const evil = userHtml('[attachment: <img src=x onerror=1>, text/plain, 3 bytes, id ' + aid + ']');
say(!evil.includes('<img src=x') && evil.includes('&lt;img'), 'a file name is escaped');
process.exit(bad ? 1 : 0);
JS
    then :; else FAILED=$((FAILED + 1)); fi
fi

# ------------------------------------------------------------ 6+7. the bubble
if grep -q '\.sn-msg--user { white-space: pre-wrap; overflow-wrap: anywhere; }' "$D/app.css"; then
    ok "a word with no spaces wraps inside the bubble"
else
    bad "a word with no spaces wraps inside the bubble"
fi
if grep -q '@keyframes cmp-msg-in' "$D/app.css" \
   && grep -q 'prefers-reduced-motion: reduce' "$D/app.css" \
   && grep -q "bubble('user', userHtml(text, deskId), { arrive: true });" "$D/app.js" \
   && ! grep -q 'arrive' <<<"$(body_of loadHistory)"; then
    ok "a new message rises into place; history and reduced motion do not animate"
else
    bad "a new message rises into place; history and reduced motion do not animate"
fi

# ------------------------------------------------------------------ 8. parses
if command -v node >/dev/null 2>&1; then
    if node --check "$D/app.js" 2>/dev/null; then ok "app.js parses"; else bad "app.js parses"; fi
    # The copy sits outside Studio's package.json ("type": "module"), so name it
    # for what it is: an ES module parsed as CommonJS fails on its first import.
    if grep -q '^import ' "$D/server.js"; then sx="$D/server.mjs"; else sx="$D/server.cjs"; fi
    cp "$D/server.js" "$sx"
    if node --check "$sx" 2>/dev/null; then ok "server.js parses"; else bad "server.js parses"; fi
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
