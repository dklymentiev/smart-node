#!/bin/sh
# DOCI container entrypoint.
#
# Ensures /var/www/html/files is a git repository before Apache starts.
# Every save in DOCI shells out to `git add && git commit` from that
# directory, so it must be initialised on first run.
#
# Idempotent: if .git already exists, this is a no-op.

set -e

FILES_DIR=/var/www/html/files

if [ -z "${DOCI_DOMAIN:-}" ]; then
    echo "[doci-entrypoint] FATAL: DOCI_DOMAIN env var is required" >&2
    exit 1
fi
DOMAIN="$DOCI_DOMAIN"

if [ ! -d "$FILES_DIR" ]; then
    mkdir -p "$FILES_DIR"
    chown www-data:www-data "$FILES_DIR"
fi

# Bind-mounted files from the host arrive owned by the host user (often
# root inside the container), which leaves www-data unable to write.
# DOCI writes through Apache (thread wraps on live docs, api/save.php edits,
# git commits), so the whole tree has to belong to www-data.
chown -R www-data:www-data "$FILES_DIR" 2>/dev/null || true

if [ ! -d "$FILES_DIR/.git" ]; then
    echo "[doci-entrypoint] Initialising git repo at $FILES_DIR"
    cd "$FILES_DIR"
    git init -q -b main
    git config user.name "DOCI"
    git config user.email "doci@$DOMAIN"
    if [ -n "$(ls -A . 2>/dev/null | grep -v '^.git$' || true)" ]; then
        git add -A
        git -c user.name=DOCI -c user.email="doci@$DOMAIN" commit -q -m "Initial DOCI content" || true
    fi
    chown -R www-data:www-data .git
    cd /
else
    # Existing repo. Pick up any seed content added between starts (host
    # bind-mounted new files into files/ since last container run, etc.)
    # so 'History:' actually reflects the file state.
    cd "$FILES_DIR"
    if ! git diff --quiet HEAD 2>/dev/null || [ -n "$(git ls-files --others --exclude-standard 2>/dev/null)" ]; then
        git add -A 2>/dev/null || true
        git -c user.name=DOCI -c user.email="doci@$DOMAIN" \
            commit -q -m "Sync seed content on container start" 2>/dev/null || true
    fi
    cd /
fi

# Apache (and the PHP code) runs as www-data, but the user's HOME isn't
# set by default. Git config files live at /var/www/.config/git/config.
export HOME=/var/www

# ----------------------------------------------------------------------
# Schema migrations -- SYNCHRONOUS, before Apache starts.
#
# Previously this block lived inside a `( sleep 5; ... ) &` subshell
# that raced Apache: requests that landed before the subshell finished
# applying 001/002/003 hit a half-migrated DB and returned 500. Now
# migrations are a hard prerequisite to taking traffic.
# ----------------------------------------------------------------------

echo "[doci-entrypoint] Waiting for database to accept connections..."
DB_WAIT_TIMEOUT="${DOCI_DB_WAIT_TIMEOUT:-60}"
i=0
until php -r '
    require_once "/var/www/html/src/config.php";
    try { get_db()->query("SELECT 1"); exit(0); }
    catch (Throwable $e) { exit(1); }
' >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge "$DB_WAIT_TIMEOUT" ]; then
        echo "[doci-entrypoint] FATAL: database unreachable after ${DB_WAIT_TIMEOUT}s" >&2
        exit 1
    fi
    sleep 1
done
echo "[doci-entrypoint] Database reachable (${i}s)"

if [ -d /var/www/html/migrations ]; then
    echo "[doci-entrypoint] Applying migrations..."
    for m in /var/www/html/migrations/*.sql; do
        [ -f "$m" ] || continue
        echo "[doci-entrypoint]   -> $(basename "$m")"
        php -r '
            require_once "/var/www/html/src/config.php";
            $pdo = get_db();
            $sql = file_get_contents($argv[1]);
            try {
                $pdo->exec($sql);
            } catch (Throwable $e) {
                fwrite(STDERR, "migration ".basename($argv[1]).": ".$e->getMessage()."\n");
                exit(1);
            }
        ' "$m" || {
            echo "[doci-entrypoint] FATAL: migration $(basename "$m") failed; refusing to start Apache" >&2
            exit 1
        }
    done
    echo "[doci-entrypoint] Migrations OK."
fi

# ----------------------------------------------------------------------
# Seed tasks -- index, normalise, demo seed.
#
# These do NOT block Apache start. They populate / normalise content
# that's nice-to-have on first boot but is not required for the API to
# answer correctly. Kept backgrounded so a slow seed never delays
# readiness.
# ----------------------------------------------------------------------
(
    sleep 2
    if [ -f /var/www/html/scripts/index-documents.php ]; then
        php /var/www/html/scripts/index-documents.php 2>&1 | tail -20 || true
    fi
    if [ -f /var/www/html/scripts/normalize-links.php ]; then
        php /var/www/html/scripts/normalize-links.php 2>&1 | tail -10 || true
        cd /var/www/html/files
        if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
            git add -A 2>/dev/null
            git -c user.name=DOCI -c user.email="doci@$DOCI_DOMAIN" \
                commit -q -m "Normalise internal links to GUID form" 2>/dev/null || true
        fi
        cd /
    fi
    if [ -f /var/www/html/scripts/seed-demo.sh ]; then
        sh /var/www/html/scripts/seed-demo.sh 2>&1 | tail -5 || true
        cd /var/www/html/files
        if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
            git add -A 2>/dev/null
            git -c user.name=DOCI -c user.email="doci@$DOCI_DOMAIN" \
                commit -q -m "Seed demo thread + version" 2>/dev/null || true
        fi
        cd /
    fi
) &

exec docker-php-entrypoint "$@"
