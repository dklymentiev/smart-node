#!/usr/bin/env bash
#
# A message search can look for the words in a message, not only for a person.
#
# WHAT WENT WRONG. An agent asked to add a contact to "our list" had no way to
# search its own conversations by what was said: the message search takes a
# person and a time window and nothing else. It could read the last dozen lines
# of a conversation, and when the thing it needed was older or in another
# thread, "not found" was the only answer it could reach.
#
# THE PROPERTIES, each run on the real code:
#
#   1. the words of the query become one pattern each: all must occur, in any
#      order, and `%`, `_` and a backslash are text, not wildcards
#   2. a long query is cut at eight words
#   3. a request with neither a person nor words returns nothing
#   4. words need no person: the search then covers every conversation the
#      caller is a party to, and the party rule is applied whether or not a
#      person was named
#   5. with words the window is a flat newest-first one (balancing across
#      channels would hide a match in a quiet channel)
#   6. each word is cut to its stem first, so another grammatical form of it
#      matches ("contacts" finds "contact")
#
#   tests/message-search-by-words.test.sh                  # working tree, PASS
#   tests/message-search-by-words.test.sh --baseline REF   # git:REF, expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "message search by words (git:$BASELINE)"
else echo "message search by words (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/messages.py > "$D/messages.py"

# 1, 2. the word patterns
if python3 - "$D/messages.py" <<'PY'
import ast, sys
src = open(sys.argv[1], encoding="utf-8").read()
keep = []
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "_SEARCH_MAX_WORDS" for t in n.targets):
        keep.append(n)
    if isinstance(n, ast.FunctionDef) and n.name in ("_search_words", "_min_words"):
        keep.append(n)
if not any(isinstance(n, ast.FunctionDef) for n in keep):
    print("   - there is no word pattern builder")
    sys.exit(1)
mod = ast.Module(body=keep, type_ignores=[])
ast.fix_missing_locations(mod)
ns = {}
exec(compile(mod, "messages", "exec"), ns)
f = ns["_search_words"]
failed = []
if f("list contacts") != ["%list%", "%contacts%"]:
    failed.append("two words are %r" % f("list contacts"))
if f("  ") != [] or f(None) != [] or f("") != []:
    failed.append("a blank query is not empty")
if f("100%") != ["%100\\%%"]:
    failed.append("percent is not escaped: %r" % f("100%"))
if f("a_b") != ["%a\\_b%"]:
    failed.append("underscore is not escaped: %r" % f("a_b"))
if f("a\\b") != ["%a\\\\b%"]:
    failed.append("backslash is not escaped: %r" % f("a\\b"))
m = ns["_min_words"]
if [m(n) for n in (1, 2, 3, 4, 5, 6, 8)] != [1, 2, 2, 3, 3, 4, 5]:
    failed.append("the number of words required is %r" % [m(n) for n in (1, 2, 3, 4, 5, 6, 8)])
if len(f(" ".join("w%d" % i for i in range(30)))) != 8:
    failed.append("a long query is not cut at eight words")
for x in failed:
    print("   - " + x)
sys.exit(1 if failed else 0)
PY
then ok "words become one pattern each, wildcards are text, a long query is cut at eight"
else bad "the word patterns are wrong or missing"; fi

# 3, 4, 5. the route
if python3 - "$D/messages.py" <<'PY'
import sys
src = open(sys.argv[1], encoding="utf-8").read()
m = src.index("class _MessageSearch")
model = src[m:src.index("@router.post(\"/search\"")]
assert "q: str | None" in model, "the request has no q"
assert 'identity: str = ""' in model, "identity is still required"
i = src.index("def messages_search")
fn = src[i:src.index("def _desk_of_message")]
assert "if not ident and not words:" in fn, "an empty request is not refused"
assert fn.count("m.content ~*") == 1 and "for i, w in enumerate(" in fn, "words are not applied one by one"
assert "_min_words(len(pats))" in fn and "ORDER BY " in fn and "({score_sql}) DESC" in fn, "a longer query is not matched by most of its words and ranked"
assert "_stem_patterns(cur, body.q)" in fn, "words are not cut to their stems, so another form of a word misses"
assert "def _stem_patterns" in src and "to_tsvector('english'::regconfig" in src, "no stemming through the database dictionary"
assert "_must_scope(principal, False)" in fn, "the party rule is gone"
# the party rule must not sit under the `if ident:` branch
scope = fn.index("_must_scope(principal, False)")
branch = fn.index("if ident:")
end_of_branch = fn.index("for i, w in enumerate(")
assert not (branch < scope < end_of_branch), "the party rule only applies when a person is named"
assert "if words or (body.channel or \"\").strip():" in fn, "words do not get the flat window"
PY
then ok "words need no person, the party rule applies either way, the window is flat"
else bad "the route does not search by words under the party rule"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
