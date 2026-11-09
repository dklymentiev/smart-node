#!/usr/bin/env bash
#
# Gate: signing out and changing the password, in Authum.
#
# WHY THIS EXISTS. Studio's Sign out posted to Authum without a CSRF token and
# was refused ("Invalid CSRF token") for everyone; there was no way to change the
# password; and the first-run password was a hidden token nobody could know.
#
# WHAT IS CHECKED, against a throwaway container of the Authum image running the
# index.php under test (the same harness as authum-session-lifetime.test.sh):
#   1. GET ?action=logout signed in: a "Sign out?" page carrying the CSRF token;
#      its form signs out (forward auth then 302). Signed out, GET goes to /.
#   2. change_password: a wrong current password, a short one and two that differ
#      are refused with their reason; a right one changes it: the old password no
#      longer signs in, the new one does.
#   3. Studio's Sign out is a GET to Authum, not a token-less POST.
#   4. the first-run password is readable and handed back once (core-api setup).
# Needs root (docker) and an Authum image; without them it prints SKIP, never PASS.
#
# USAGE: sudo tests/authum-signin.test.sh [--baseline REF]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC_REL="bootstrap/authum/src/index.php"
IMAGE="smart-node-authum:$(cat "$ROOT/bootstrap/authum/VERSION")"
NAME="t5937-authum-$$"
PORT=$(( 20000 + $$ % 20000 ))
FAILS=0
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
ok() { echo "ok    $*"; }

# 3 and 4 need no container
f="$ROOT/companions/studio/public/next2/index.html"
grep -q '<form id="logoutForm" method="GET"><input type="hidden" name="action" value="logout">' "$f" \
    && ok "Studio Sign out opens Authum's page" || fail "Studio Sign out still posts without a token"
grep -q "/?action=logout'" "$ROOT/companions/studio/public/next2/js/app.js" \
    && fail "Studio still posts to ?action=logout" || ok "no token-less POST left in the Studio"
pw=$(python3 - "$ROOT/companions/core-api/routes/setup.py" <<'EOF'
import ast, sys, secrets
src = open(sys.argv[1]).read()
fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "readable_password")
ns = {"secrets": secrets}
exec(compile(ast.Module([fn], []), "setup", "exec"), ns)
print(ns["readable_password"]())
EOF
)
[[ "$pw" =~ ^[2-9a-hj-km-np-z]{4}-[2-9a-hj-km-np-z]{4}-[2-9a-hj-km-np-z]{4}$ ]] \
    && ok "first-run password is readable ($pw)" || fail "first-run password '$pw' is not three groups of four"
grep -q '"username": username, "password": password' "$ROOT/companions/core-api/routes/setup.py" \
    && ok "setup hands the password back once" || fail "setup does not return the password it made"

grep -q "if (\$config\['password_login'\] && !str_starts_with" "$ROOT/$SRC_REL" \
    && fail "a local (http) node still hides the password form" || ok "the password form shows on every node"
# the panel: its Logout goes to /logout, which nginx sends to Authum's sign-out page
grep -rq 'api/auth.php?action=logout' "$ROOT/bootstrap/smart-node-ui/src" \
    && fail "a panel page still links the missing /api/auth.php" || ok "no panel page links /api/auth.php"
for conf in nginx.conf nginx.local.conf; do
  grep -q 'location = /logout' "$ROOT/bootstrap/smart-node-ui/$conf" \
    && ok "panel $conf sends /logout to Authum" || fail "panel $conf has no /logout"
done
# one sign-up form: an install with the Studio sends people to the first-run page
grep -q 'open the panel and finish /setup. Read' "$ROOT/orchestrator/install-on.sh" \
    && fail "a full install still sends people to the panel /setup" || ok "a full install points at the Studio"
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    IMAGE="$(docker inspect smart-node-authum --format '{{.Config.Image}}' 2>/dev/null || true)"
fi
if [[ -z "$IMAGE" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "SKIP  no docker or no Authum image: the Authum part was not run"
    [[ $FAILS -gt 0 ]] && { echo "RESULT: FAIL ($FAILS)"; exit 1; }
    exit 0
fi
echo "runtime image: $IMAGE"
WORK="$(mktemp -d)"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT
if [[ "${1:-}" == "--baseline" ]]; then
    git -C "$ROOT" show "${2:?}:$SRC_REL" > "$WORK/index.php" || { echo "cannot read $SRC_REL at $2"; exit 2; }
else
    cp "$ROOT/$SRC_REL" "$WORK/index.php"
fi
chmod 644 "$WORK/index.php"
mkdir -p "$WORK/auth" "$WORK/data/ratelimit"
chown -R 82:82 "$WORK/auth" "$WORK/data"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:80" \
    -v "$WORK/auth:/app/auth" -v "$WORK/data:/app/data" -v "$WORK/index.php:/app/index.php:ro" \
    -e AUTHUM_DOMAIN=.t5937.local -e AUTHUM_RP_ID=t5937.local -e AUTHUM_AUTH_URL=https://auth.t5937.local \
    "$IMAGE" >/dev/null || { echo "FAIL: container did not start"; exit 1; }
for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$PORT/?action=health" && break; sleep 0.25; done
HASH=$(docker exec "$NAME" php -r 'echo password_hash("old-pass-5937", PASSWORD_BCRYPT);')
printf '{"tester":{"password":"%s"}}' "$HASH" > "$WORK/auth/users.json"
chown 82:82 "$WORK/auth/users.json"
U="http://127.0.0.1:$PORT"
signin() {  # $1 password -> prints the session id, empty if refused (a refused try gets a cookie too)
    local out; out=$(curl -s -D - -H 'Content-Type: application/json' -d "{\"username\":\"tester\",\"password\":\"$1\"}" "$U/?action=password")
    [[ "$out" == *'"success":true'* ]] || return 0
    printf '%s\n' "$out" | grep -i '^set-cookie: authum_session=' | tail -1 | sed 's/.*authum_session=\([^;]*\).*/\1/' | tr -d '\r'
}
C() { curl -s -H "Cookie: authum_session=$SID" "$@"; }
verify() { C -o /dev/null -w '%{http_code}' "$U/?action=verify"; }
csrf() { C "$1" | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//; s/"$//'; }

# 1
SID=""
[[ "$(C -o /dev/null -w '%{redirect_url}' "$U/?action=logout")" == *"/" ]] && ok "signed out: the page sends you to sign in" || fail "GET logout while signed out does not redirect"
SID=$(signin old-pass-5937); [[ -n "$SID" ]] || { echo "FAIL: could not sign in"; exit 1; }
page=$(C "$U/?action=logout")
[[ "$page" == *"Sign out?"* ]] && ok "GET logout: a Sign out? page" || fail "GET logout shows no confirmation page"
T=$(csrf "$U/?action=logout")
[[ -n "$T" ]] && ok "the page carries the CSRF token" || fail "no CSRF token on the sign-out page"
C -o /dev/null --data-urlencode "csrf_token=$T" "$U/?action=logout"
[[ "$(verify)" == "302" ]] && ok "its button signs out" || fail "still signed in after Sign out"

# 2
SID=$(signin old-pass-5937)
T=$(csrf "$U/?page=settings")
change() { C -o /dev/null -w '%{redirect_url}' --data-urlencode "csrf_token=$T" --data-urlencode "current=$1" \
    --data-urlencode "new=$2" --data-urlencode "repeat=$3" "$U/?action=change_password"; }
[[ "$(change wrong-one new-pass-5937x new-pass-5937x)" == *"pw=current"* ]] && ok "a wrong current password is refused" || fail "a wrong current password was not refused"
[[ "$(change old-pass-5937 short short)" == *"pw=short"* ]] && ok "a short new password is refused" || fail "a short password was not refused"
[[ "$(change old-pass-5937 new-pass-5937x new-pass-5937y)" == *"pw=repeat"* ]] && ok "two different new passwords are refused" || fail "a mismatch was not refused"
[[ "$(change old-pass-5937 new-pass-5937x new-pass-5937x)" == *"pw=ok"* ]] && ok "the password is changed" || fail "a right change was not accepted"
[[ -z "$(signin old-pass-5937)" ]] && ok "the old password no longer signs in" || fail "the old password still signs in"
[[ -n "$(signin new-pass-5937x)" ]] && ok "the new password signs in" || fail "the new password does not sign in"
# read the page first: `curl | grep -q` under pipefail fails whenever grep stops reading early
settings=$(C "$U/?page=settings")
[[ "$settings" == *'action="?action=change_password"'* ]] && ok "the settings page has the form" || fail "no password form on the settings page"

if [[ $FAILS -gt 0 ]]; then echo "RESULT: FAIL ($FAILS)"; exit 1; fi
echo "RESULT: PASS"
