#!/usr/bin/env bash
#
# install-from-release.sh -- put a node on a machine using nothing but a published
# release: the repository at a tag.
#
# WHY THIS EXISTS. A release clone carries the installer and every component's
# source, and that is the whole release: no component ships an image, so there
# is nothing to download beside the clone. Assets attached to an old release
# are ignored.
#
# It is also the answer to "can someone else install this". Everything here uses
# only what a person outside this project can reach: a clone and a release. If a
# step ever needs something from our build host, that is a defect in the release,
# not a missing instruction.
#
# THE REPOSITORY MAY BE PRIVATE. Pass a token and it works exactly the same; the
# token is used for the clone and the release lookup, and is never written to
# disk or into the git remote. When the repository is public, omit it.
#
# Usage:
#   tools/install-from-release.sh --workdir /tmp/sn --token TOKEN \
#       -- --provider lxd-local --name test-node --ip NODE_IP \
#          --ssh-key ~/.ssh/id.pub --setup-via-token --all
#
#   --tag vX.Y.Z     which release (default: the newest one published)
#   --repo O/N       source repository (default: the project's own)
#   --workdir DIR    where to put the clone (default: a temp directory)
#   --token TOKEN    GitHub token; or set GITHUB_TOKEN. Omit for a public repo.
#   --prepare-only   clone the release, print the install command, run nothing
#   --              everything after this is passed to orchestrator/install-on.sh
#
# Exit 0 installed (or prepared), 1 a check refused, 2 could not run.
set -uo pipefail

REPO="dklymentiev/smart-node"
TAG=""
WORKDIR=""
TOKEN="${GITHUB_TOKEN:-}"
PREPARE_ONLY=0
INSTALL_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)         REPO="${2:?}"; shift 2 ;;
        --tag)          TAG="${2:?}"; shift 2 ;;
        --workdir)      WORKDIR="${2:?}"; shift 2 ;;
        --token)        TOKEN="${2:?}"; shift 2 ;;
        --prepare-only) PREPARE_ONLY=1; shift ;;
        --help|-h)      sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --)             shift; INSTALL_ARGS=("$@"); break ;;
        *) echo "unknown argument: $1 (use --help)" >&2; exit 2 ;;
    esac
done

for tool in git curl tar sha256sum python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "[from-release] missing: $tool" >&2; exit 2; }
done

api() {
    if [[ -n "$TOKEN" ]]; then
        curl -sS --max-time 60 -H "Authorization: Bearer $TOKEN" "$@"
    else
        curl -sS --max-time 60 "$@"
    fi
}

# ------------------------------------------------------------------ the release

if [[ -z "$TAG" ]]; then
    TAG="$(api "https://api.github.com/repos/${REPO}/releases/latest" \
           | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name") or "")' 2>/dev/null)"
    [[ -n "$TAG" ]] || { echo "[from-release] could not read the newest release of $REPO" >&2; exit 2; }
    echo "[from-release] newest release: $TAG"
fi

release_json="$(api "https://api.github.com/repos/${REPO}/releases/tags/${TAG}")"
printf '%s' "$release_json" | python3 -c 'import json,sys; json.load(sys.stdin)["tag_name"]' >/dev/null 2>&1 \
    || { echo "[from-release] no published release $TAG in $REPO" >&2; exit 2; }

# -------------------------------------------------------------------- the clone

if [[ -z "$WORKDIR" ]]; then
    WORKDIR="$(mktemp -d)"
    echo "[from-release] workdir: $WORKDIR (temporary)"
fi
CLONE="$WORKDIR/smart-node"
[[ -e "$CLONE" ]] && { echo "[from-release] $CLONE already exists; choose an empty --workdir" >&2; exit 2; }
mkdir -p "$WORKDIR"

echo "[from-release] cloning $REPO at $TAG"
if [[ -n "$TOKEN" ]]; then
    git clone --quiet --branch "$TAG" --depth 1 \
        "https://${TOKEN}@github.com/${REPO}.git" "$CLONE" 2>/dev/null
else
    git clone --quiet --branch "$TAG" --depth 1 \
        "https://github.com/${REPO}.git" "$CLONE" 2>/dev/null
fi
[[ -d "$CLONE/.git" ]] || { echo "[from-release] clone failed (private repository without a token?)" >&2; exit 2; }
# The token must not survive in the remote: a working copy left on an operator's
# machine would otherwise carry a credential in plain text.
git -C "$CLONE" remote set-url origin "https://github.com/${REPO}.git"

echo "[from-release] release $(cat "$CLONE/RELEASE" 2>/dev/null), $(find "$CLONE" -type f -not -path '*/.git/*' | wc -l) files"

# ------------------------------------------------------------------- install it

if (( PREPARE_ONLY )) || [[ ${#INSTALL_ARGS[@]} -eq 0 ]]; then
    echo
    echo "[from-release] ready. Install with:"
    echo "  cd $CLONE && orchestrator/install-on.sh --provider lxd-local \\"
    echo "      --name <node> --ip <ip> --ssh-key <key.pub> --setup-via-token --all"
    exit 0
fi

echo "[from-release] running the installer"
cd "$CLONE" || exit 2
# Thread the optional token so companion scripts can read private registry assets
# during pre-publication testing. Public code-only installs leave it empty.
export GITHUB_TOKEN="${TOKEN}"
exec orchestrator/install-on.sh "${INSTALL_ARGS[@]}"
