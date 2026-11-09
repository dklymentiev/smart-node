#!/usr/bin/env bash
#
# Gate: an Authum session outlives a container restart and ends on the node's
# own limits, not on PHP's defaults.
#
# WHY THIS EXISTS. Authum kept PHP sessions in /tmp inside the container with
# the 24-minute default garbage collector, while the cookie claimed 12 hours.
# A page left alone for half an hour, or any restart of the container, signed
# the owner out, and every page behind forward auth then failed silently.
#
# WHAT IS CHECKED, against a throwaway container of the installed Authum image
# running the index.php under test, with its own auth/ and data/ directories:
#   1. password sign-in, then forward auth (?action=verify) answers 200;
#   2. after the container is recreated (what an update does) the same cookie
#      still answers 200, and so it does after 30 minutes idle and a run of
#      PHP's session collector;
#   3. a session idle longer than AUTHUM_SESSION_LIFETIME answers 302;
#   4. a session older than AUTHUM_SESSION_MAX_AGE answers 302 even if used;
#   5. the cookie is issued for the absolute limit (Max-Age 2592000).
# Needs root (docker) and the image smart-node-authum:<VERSION>; without them
# it prints SKIP, never PASS.
#
# USAGE:
#   sudo tests/authum-session-lifetime.test.sh                 # working tree
#   sudo tests/authum-session-lifetime.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SRC_REL="bootstrap/authum/src/index.php"
IMAGE="smart-node-authum:$(cat "$ROOT/bootstrap/authum/VERSION")"
NAME="t4849-authum-$$"
PORT=$(( 20000 + $$ % 20000 ))

# The image is only the PHP runtime: index.php under test is mounted over the
# one baked in. Before an update builds this version's tag, the running node's
# own Authum image serves the same purpose; say which one ran.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    IMAGE="$(docker inspect smart-node-authum --format '{{.Config.Image}}' 2>/dev/null || true)"
fi
if [[ -z "$IMAGE" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "SKIP  no docker or no Authum image: nothing was run"
    exit 0
fi
echo "runtime image: $IMAGE"

WORK="$(mktemp -d)"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT

if [[ "${1:-}" == "--baseline" ]]; then
    git -C "$ROOT" show "${2:?}:$SRC_REL" > "$WORK/index.php" || { echo "cannot read $SRC_REL at $2"; exit 2; }
    echo "$SRC_REL (${2})"
else
    cp "$ROOT/$SRC_REL" "$WORK/index.php"
    echo "$SRC_REL (working tree)"
fi
chmod 644 "$WORK/index.php"
mkdir -p "$WORK/auth" "$WORK/data/ratelimit"
# www-data in php:8.3-fpm-alpine is uid 82, as on a node.
chown -R 82:82 "$WORK/auth" "$WORK/data"

# A node update RECREATES the container (a plain restart keeps its /tmp), so
# that is what step 2 does.
start() {
    docker rm -f "$NAME" >/dev/null 2>&1
    docker run -d --name "$NAME" -p "127.0.0.1:$PORT:80" \
        -v "$WORK/auth:/app/auth" -v "$WORK/data:/app/data" \
        -v "$WORK/index.php:/app/index.php:ro" \
        -e AUTHUM_DOMAIN=.t4849.local -e AUTHUM_RP_ID=t4849.local \
        -e AUTHUM_AUTH_URL=https://auth.t4849.local \
        -e AUTHUM_SESSION_LIFETIME=43200 -e AUTHUM_SESSION_MAX_AGE=2592000 \
        "$IMAGE" >/dev/null || return 1
    for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$PORT/?action=health" && return 0; sleep 0.25; done
    return 1
}
start || { echo "FAIL: container never answered"; exit 1; }

HASH=$(docker exec "$NAME" php -r 'echo password_hash("t4849-pass", PASSWORD_ARGON2ID);')
printf '{"tester":{"password":"%s"}}' "$HASH" > "$WORK/auth/users.json"
chown 82:82 "$WORK/auth/users.json"

FAILS=0
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
ok() { echo "ok    $*"; }

HDRS=$(curl -s -D - -o /dev/null -H 'Content-Type: application/json' \
    -d '{"username":"tester","password":"t4849-pass"}' "http://127.0.0.1:$PORT/?action=password")
SID=$(printf '%s\n' "$HDRS" | grep -i '^set-cookie: authum_session=' | tail -1 | sed 's/.*authum_session=\([^;]*\).*/\1/' | tr -d '\r')
[[ -n "$SID" ]] || { echo "FAIL: no session cookie from password sign-in"; exit 1; }

verify() { curl -s -o /dev/null -w '%{http_code}' -H "Cookie: authum_session=$SID" "http://127.0.0.1:$PORT/?action=verify"; }
# Rewrite one field of the session the way PHP itself stores it.
set_field() {
    docker exec -u www-data "$NAME" php -r '
        session_save_path(is_dir("/app/data/sessions") ? "/app/data/sessions" : sys_get_temp_dir());
        session_id($argv[1]); session_start();
        $_SESSION[$argv[2]] = time() - (int)$argv[3]; session_write_close();' "$SID" "$1" "$2"
}

# 1
[[ "$(verify)" == "200" ]] && ok "signed in: forward auth 200" || fail "forward auth refused a fresh sign-in"

# 5
MAXAGE=$(printf '%s\n' "$HDRS" | grep -i '^set-cookie: authum_session=' | tail -1 | grep -io 'max-age=[0-9]*' | cut -d= -f2)
[[ "$MAXAGE" == "2592000" ]] && ok "cookie Max-Age 2592000" || fail "cookie Max-Age is '${MAXAGE:-none}', not the 30-day limit"

# 2b first, so each half fails on its own: PHP's collector runs on 1 in 100 session starts. Age the session file
# by 30 minutes, then make 500 requests (the chance that none collects is
# below 1%). The 24-minute default removed it; the node's 12 h must not.
docker exec "$NAME" sh -c "f=\$(ls /app/data/sessions/sess_$SID /tmp/sess_$SID 2>/dev/null | head -1); [ -n \"\$f\" ] && touch -d @\$(( \$(date +%s) - 1800 )) \"\$f\""
for _ in $(seq 1 500); do curl -s -o /dev/null "http://127.0.0.1:$PORT/?action=health"; done
[[ "$(verify)" == "200" ]] && ok "30 min idle then collector: still signed in" || fail "the session collector removed a session idle 30 min"

# 2 (a session the collector removed would pass for "lost in the recreate",
# so sign in afresh when 2b failed)
if [[ "$(verify)" != "200" ]]; then
    SID=$(curl -s -D - -o /dev/null -H 'Content-Type: application/json' \
        -d '{"username":"tester","password":"t4849-pass"}' "http://127.0.0.1:$PORT/?action=password" \
        | grep -i '^set-cookie: authum_session=' | tail -1 | sed 's/.*authum_session=\([^;]*\).*/\1/' | tr -d '\r')
fi
start
[[ "$(verify)" == "200" ]] && ok "session survives recreating the container" || fail "recreating the container signed the session out"

# 3 (only meaningful if the session survived; re-sign-in otherwise would hide it)
set_field last_seen $((13 * 3600))
[[ "$(verify)" == "302" ]] && ok "13 h idle: signed out" || fail "a session idle 13 h is still accepted"

# 4: fresh sign-in, used a second ago, but signed in 31 days ago
HDRS=$(curl -s -D - -o /dev/null -H 'Content-Type: application/json' \
    -d '{"username":"tester","password":"t4849-pass"}' "http://127.0.0.1:$PORT/?action=password")
SID=$(printf '%s\n' "$HDRS" | grep -i '^set-cookie: authum_session=' | tail -1 | sed 's/.*authum_session=\([^;]*\).*/\1/' | tr -d '\r')
set_field auth_time $((31 * 86400))
set_field last_seen 1
[[ "$(verify)" == "302" ]] && ok "31 days after sign-in: signed out" || fail "a session 31 days old is still accepted"

if [[ $FAILS -gt 0 ]]; then echo "RESULT: FAIL ($FAILS)"; exit 1; fi
echo "RESULT: PASS"
