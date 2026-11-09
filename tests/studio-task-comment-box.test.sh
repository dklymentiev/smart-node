#!/usr/bin/env bash
#
# The comment box on the Studio task card.
#
# WHAT IT REPLACED. The planner tells an actor about a comment that says @name,
# and the Studio already relays POST /api/tasks/:id/comments with the viewer's
# identity, but the card had nowhere to type: a stuck task could only be asked
# about from a shell.
#
# THE PROPERTIES:
#   1. the card has a box, a Send button and a status line, right under the
#      Comments heading and above the comments
#   2. Send posts {content} to Studio's /api/tasks/:id/comments, which
#      relays it to the planner; Ctrl/Cmd+Enter sends too
#   3. the name after @ is lower-cased before posting, with the planner's own
#      mention pattern, so "@Dana" reaches the actor dana and an e-mail address
#      is left alone (run for real)
#   4. comments are newest first, and while the card is open new ones are
#      fetched every 4 s with the tab in view, deduplicated by id, and the poll
#      stops when the card is gone
#   5. each comment has Reply, which quotes it and mentions its author, but not
#      the viewer or a system actor
#   6. app.js parses
#
#   tests/studio-task-comment-box.test.sh                  # tree, PASS
#   tests/studio-task-comment-box.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "Studio task comment box (git:$BASELINE)"
else echo "Studio task comment box (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/studio/public/next2/js/app.js         > "$D/app.js"
read_src companions/studio/src/server.js            > "$D/server.js"
read_src companions/planner/src/planner/mentions.py > "$D/mentions.py"
for f in app.js server.js mentions.py; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# Body of a top-level function, flag-based (an awk range closes on its own
# first line).
body_of() {
    awk -v n="$1" 'f && /^(async )?function /{exit} $0 ~ "^(async )?function "n"\\("{f=1} f' "$D/app.js"
}
card=$(body_of loadDetail)
[[ -n "$card" ]] || { bad "loadDetail exists"; echo; echo "FAIL"; exit 1; }
has() { grep -qF -- "$1" <<<"$card"; }

# ------------------------------------------------------------------ 1. markup
if awk '/<div class="td__sec">Comments /{s=1} s && /<textarea id="tdCmt"/{t=1} t && /id="tdCmtMsg"/{m=1} m && /id="tdCmtSend"/{b=1} b && /<div class="td__cmts">/{print "yes"; exit}' <<<"$card" | grep -q yes; then
    ok "box, status and Send sit under the Comments heading, above the comments"
else
    bad "box, status and Send sit under the Comments heading, above the comments"
fi

# ------------------------------------------------------------------ 2. sending
if has 'fetch(`/api/tasks/${encodeURIComponent(id)}/comments`, {' \
   && has "method: 'POST'," \
   && has 'body: JSON.stringify({ content }),'; then
    ok "Send posts {content} to /api/tasks/:id/comments"
else
    bad "Send posts {content} to /api/tasks/:id/comments"
fi
if grep -qF "app.post('/api/tasks/:id/comments'" "$D/server.js"; then
    ok "Studio relays the post to the planner"
else
    bad "Studio relays the post to the planner"
fi
if has "if (send) send.addEventListener('click', post);" \
   && has "ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)"; then
    ok "the button and Ctrl/Cmd+Enter send"
else
    bad "the button and Ctrl/Cmd+Enter send"
fi

# ------------------------------------------------- 3. mentions, run for real
# The planner: (?<![CLASS])@(NAME). The box: (^|[^CLASS])@(NAME). Same CLASS
# and NAME, or a name the box lower-cases is not one the planner reads.
pyc=$(sed -nE 's/^_MENTION_RE = re\.compile\(r"\(\?<!\[([^]]*)\]\)@(.*)"\)$/\1 \2/p' "$D/mentions.py")
jsc=$(sed -nE 's/.*const content = text\.replace\(\/\(\^\|\[\^([^]]*)\]\)@(.*)\/g, .*/\1 \2/p' <<<"$card")
if [[ -n "$pyc" && "$pyc" == "$jsc" ]]; then
    ok "the box finds a mention with the planner's own pattern"
else
    bad "the box finds a mention with the planner's own pattern (planner: ${pyc:-none}; Studio: ${jsc:-none})"
fi
if command -v node >/dev/null 2>&1; then
    grep -m1 -F "const content = text.replace(" <<<"$card" > "$D/content.js"
    if node - "$D/content.js" <<'JS'
const fs = require('fs');
const line = fs.readFileSync(process.argv[2], 'utf8').trim();
if (!line) { console.log('  FAIL the box lower-cases a mention (no such line)'); process.exit(1); }
const lower = new Function('text', line + '\nreturn content;');
let bad = 0;
const say = (good, name, got) => { console.log((good ? '  ok   ' : '  FAIL ') + name + (good ? '' : ' -> ' + got)); if (!good) bad++; };
let g;
g = lower('@Dana what is left here?'); say(g === '@dana what is left here?', '"@Dana" is posted as "@dana"', g);
g = lower('ask (@Ops.Lead) and @QA-bot'); say(g === 'ask (@ops.lead) and @qa-bot', 'every mention is lower-cased, punctuation kept', g);
g = lower('write to Someone@Example.com'); say(g === 'write to Someone@Example.com', 'an e-mail address is left alone', g);
g = lower('Plain Text Stays'); say(g === 'Plain Text Stays', 'text without a mention is unchanged', g);
process.exit(bad ? 1 : 0);
JS
    then :; else FAILED=$((FAILED + 1)); fi
fi

# ------------------------------------------------------------- 4. live list
if has "els.reverse().forEach((el) => list.appendChild(el));" \
   && has "list.insertAdjacentHTML('afterbegin', one(c))"; then
    ok "comments are newest first, and a new one goes on top"
else
    bad "comments are newest first, and a new one goes on top"
fi
if has "}, 4000);" \
   && has "if (document.hidden || host.offsetParent === null) return;" \
   && has "if (!host.isConnected || !list || !list.isConnected) { clearInterval(host._tdPoll); return; }" \
   && has "if (host._tdPoll) clearInterval(host._tdPoll);" \
   && has "!known.has(c.id)"; then
    ok "new comments every 4 s in view, by id, one poll per card, stopped when it is gone"
else
    bad "new comments every 4 s in view, by id, one poll per card, stopped when it is gone"
fi

# ------------------------------------------------------------------ 5. Reply
if has "b.className = 'td__cmt-re'; b.textContent = 'Reply';" \
   && has "const quiet = new Set(['admin', 'owner', 'system', 'runner', 'voice', String(meIdentity.user || '').split('@')[0]]);" \
   && has "const who = c.author && !quiet.has(c.author) ? '@' + c.author + ' ' : '';"; then
    ok "Reply quotes the comment and mentions its author, never the viewer or a system actor"
else
    bad "Reply quotes the comment and mentions its author, never the viewer or a system actor"
fi

# ------------------------------------------------------------------ 6. parses
if command -v node >/dev/null 2>&1; then
    if node --check "$D/app.js" 2>/dev/null; then ok "app.js parses"; else bad "app.js parses"; fi
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
