#!/usr/bin/env bash
# An outbound send that fails for a reason that passes (provider 5xx or 429,
# a timeout, a dropped SMTP connection) is tried again later instead of being
# marked failed on the spot; a refusal still fails at once. On the trials
# stand a 60 s Telegram outage lost the agent's answer (502 -> failed).
# Supports --baseline REF to show RED on main.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0
ok() { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "outbound retry on transient failure (git:$BASELINE)"
else echo "outbound retry on transient failure (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/communication/send_outbound.py > "$D/send_outbound.py"
read_src bootstrap/B02-sn-core-schema.sql > "$D/schema.sql"
read_src companions/communication/common/db.py > "$D/db.py"

# 1. the classifier, run on real exception shapes
python3 - "$D/send_outbound.py" <<'PY'
import ast, smtplib, socket, sys
src = open(sys.argv[1]).read()
fn = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_transient"]
if not fn:
    print("  FAIL there is a _transient() classifier"); sys.exit(1)
ns = {}
exec(compile(ast.Module(body=fn, type_ignores=[]), "x", "exec"), ns)
t = ns["_transient"]

class Resp:
    def __init__(self, code): self.status_code = code
class HTTPError(Exception):
    def __init__(self, code): super().__init__("HTTP %d" % code); self.response = Resp(code)

cases = [
    ("telegram 502", HTTPError(502), True),
    ("telegram 429", HTTPError(429), True),
    ("telegram 400 (chat not found)", HTTPError(400), False),
    ("telegram 401 (bad token)", HTTPError(401), False),
    ("SMTP connection dropped", smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), True),
    ("SMTP refused to connect", ConnectionRefusedError(111, "refused"), True),
    ("SMTP 451 try later", smtplib.SMTPDataError(451, b"try later"), True),
    ("SMTP 550 no such user", smtplib.SMTPDataError(550, b"no such user"), False),
    ("timeout", socket.timeout("timed out"), True),
    ("egress refusal", RuntimeError("egress guard: refusing to email"), False),
]
bad = 0
for name, exc, want in cases:
    got = t(exc)
    print("  %s %s -> %s" % ("ok  " if got == want else "FAIL", name, "retry" if want else "fail now"))
    bad += got != want
sys.exit(1 if bad else 0)
PY
[[ $? -eq 0 ]] || FAILED=$((FAILED + 1))

# 2. a row waiting for its retry is not picked up early
if python3 - "$D/send_outbound.py" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
m = re.search(r'SCAN_SQL = """(.*?)"""', s, re.S)
sys.exit(0 if m and "send_next_at" in m.group(1) and "send_attempts" in m.group(1) else 1)
PY
then ok "the send pass skips rows whose retry is not due yet"
else bad "the send pass skips rows whose retry is not due yet"; fi

# 3. transient and attempts left -> retry; otherwise the old failure + tell the agent
if python3 - "$D/send_outbound.py" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
i = s.find("except Exception as exc:  # one bad row")
blk = s[i:i + 2000]
r, f, t = blk.find("_schedule_retry("), blk.find('_set_state(mid, "failed"'), blk.find("db.tell_sender(")
sys.exit(0 if i > 0 and -1 not in (r, f, t) and r < f < t and "_transient(exc)" in blk else 1)
PY
then ok "a transient failure is rescheduled; only the last try fails and tells the agent"
else bad "a transient failure is rescheduled; only the last try fails and tells the agent"; fi

# 4. existing nodes get the columns; the retry is journalled
grep -q "ADD COLUMN IF NOT EXISTS send_next_at" "$D/schema.sql" \
  && grep -q "ADD COLUMN IF NOT EXISTS send_attempts" "$D/schema.sql" \
  && ok "existing nodes get send_next_at and send_attempts" \
  || bad "existing nodes get send_next_at and send_attempts"
python3 - "$D/db.py" <<'PY2'
import re, sys
s = open(sys.argv[1]).read()
m = re.search(r"def mark_transmitted.*?UPDATE message_state(.*?)WHERE", s, re.S)
sys.exit(0 if m and "error = NULL" in m.group(1) else 1)
PY2
[[ $? -eq 0 ]] && ok "a send that went through on a retry drops the retrying text"                || bad "a send that went through on a retry drops the retrying text"
grep -q '"message.send_retry"' "$D/db.py" \
  && ok "each retry is an event (message.send_retry)" \
  || bad "each retry is an event (message.send_retry)"

echo
[[ $FAILED -eq 0 ]] && echo "PASS" || echo "FAIL ($FAILED)"
exit $FAILED
