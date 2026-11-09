#!/bin/bash
#
# shipping-hygiene.sh -- refuse to ship what does not belong on a customer node.
#
# The companion trees are copied to a node DIRECTORY AT A TIME. Anything sitting
# in them travels, whether or not git tracks it -- and .gitignore makes that
# WORSE, not better: an ignored file is invisible to every routine check while
# still being copied. That is not hypothetical. Until 2026-08-03 the installer
# shipped companions/communication/.env.bak.* to every node, carrying a Telegram
# bot token, a brin API key and two mailbox passwords, because the backups were
# gitignored and no sweep ever looked at them.
#
# So this walks the FILESYSTEM, exactly like permissions.sh walks it rather than
# trusting the install manifest, and it REFUSES rather than warns. A warning in
# a build log is how the last one survived for two months.
#
# Usage:
#   shipping-hygiene.sh [--root DIR]   # exit 0 clean, 2 on findings
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "${1:-}" == "--root" ]] && ROOT="$2"

# Trees the installer copies wholesale. Anything here reaches a node.
SHIPPED=("companions" "bootstrap")

# Upstream projects vendored with their own history and their own hygiene.
# We do not police someone else's tree; we only refuse to add to it.
EXEMPT_RE='/companions/(doci|mesh|rein|planner)/|/node_modules/|/__pycache__/|/\.git/'

findings=0
report() { printf '  %-9s %s\n' "$1" "${2#$ROOT/}"; findings=$((findings + 1)); }

echo "[hygiene] scanning what ships, under $ROOT"

for tree in "${SHIPPED[@]}"; do
    [[ -d "$ROOT/$tree" ]] || continue

    # 1. Backups, editor leftovers, crash droppings. A node has no use for any
    #    of them, and a .bak of a config is a credential file by another name.
    while IFS= read -r f; do
        [[ "$f" =~ $EXEMPT_RE ]] && continue
        report "BACKUP" "$f"
    done < <(find "$ROOT/$tree" -type f \( \
                -name '*.bak' -o -name '*.bak.*' -o -name '*.bak-*' \
                -o -name '*~' -o -name '*.orig' -o -name '*.rej' \
                -o -name '*.save' -o -name '*.old' -o -name '*.swp' \
                -o -name '*.tmp' -o -name 'core.[0-9]*' -o -name 'nohup.out' \
                -o -name '.DS_Store' \) 2>/dev/null)

    # 2. Real .env files. `.env.example` is documentation and stays; a populated
    #    .env is somebody's live credentials.
    while IFS= read -r f; do
        [[ "$f" =~ $EXEMPT_RE ]] && continue
        [[ "$f" == *.example || "$f" == *.template || "$f" == *.sample ]] && continue
        report "ENVFILE" "$f"
    done < <(find "$ROOT/$tree" -type f \( -name '.env' -o -name '.env.*' -o -name '*.env' \) 2>/dev/null)

    # 3. Private key material, by CONTENT rather than by name -- the name is the
    #    part an author controls, and gets wrong.
    while IFS= read -r f; do
        [[ "$f" =~ $EXEMPT_RE ]] && continue
        report "KEY" "$f"
    done < <(command grep -rlI --exclude-dir=.git -e '-----BEGIN .*PRIVATE KEY-----' \
                "$ROOT/$tree" 2>/dev/null)

    # 4. An .ssh directory has no business in a tree that ships.
    while IFS= read -r d; do
        [[ "$d" =~ $EXEMPT_RE ]] && continue
        report "SSHDIR" "$d"
    done < <(find "$ROOT/$tree" -type d -name '.ssh' 2>/dev/null)
done

# 5. Superseded image tarballs. Each companion loads exactly the version its
#    STAGE_VERSION names; every other tarball beside it is dead weight that gets
#    carried around forever (2.1 GB of it on 2026-08-03).
for imgdir in "$ROOT"/companions/*-image; do
    [[ -d "$imgdir" ]] || continue
    while IFS= read -r tarball; do
        base=$(basename "$tarball")
        ver="${base##*-}"; ver="${ver%.tar.gz}"
        # Live if some companion script names this exact version.
        if ! command grep -rqE "STAGE_VERSION=\"?${ver//./\\.}\"?" "$ROOT"/companions/*.sh 2>/dev/null; then
            report "STALEIMG" "$tarball"
        fi
    done < <(find "$imgdir" -name '*.tar.gz' 2>/dev/null)
done

if (( findings )); then
    echo
    echo "[hygiene] REFUSED: $findings item(s) would ship to a customer node."
    echo "          Move them out of the tree; do not add them to .gitignore --"
    echo "          ignoring a file hides it from review while it still ships."
    exit 2
fi

echo "[hygiene] clean -- nothing in the shipped trees but what belongs there"
exit 0
