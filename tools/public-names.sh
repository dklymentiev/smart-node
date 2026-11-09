#!/usr/bin/env bash
#
# public-names.sh -- what public hostnames does this node claim, and do they
# still answer?
#
# WHY THIS EXISTS. A node's own validation checks its internal `*.sn.vpn` names
# and passes while a public door is shut. That is not a hypothetical: on
# 2026-08-14 a node update recreated the authum container, its Traefik label
# came back carrying only the internal name, `auth.<public>` began answering
# 404, every other public name kept redirecting to it, and sign-in was down --
# with validate reporting 189/189 passed throughout.
#
# So this asks the node what it claims, rather than being told:
#
#   1. read every router rule Traefik knows -- the dynamic files AND the
#      container labels;
#   2. anything outside this node's own domain is a PUBLIC name;
#   3. ask each one whether it answers.
#
# AND IT SAYS WHERE EACH RULE LIVES, which is the part that matters for keeping
# them. A rule in a dynamic file survives a container recreation. A rule on a
# container label does not: the installer owns those containers and rewrites
# their labels on every update. A public name found on a label is reported as a
# WARNING even while it answers, because it is answering on borrowed time.
#
# Run it on the node. Exit 0 if every public name answers, 1 otherwise.
#
# Usage:
#   public-names.sh              check and report
#   public-names.sh --list       list what was found, ask nothing
#   public-names.sh --quiet      only problems
set -uo pipefail

DYNAMIC_DIR="${SN_TRAEFIK_DYNAMIC:-/etc/smart-node/traefik/dynamic}"
NODE_DOMAIN="${SMART_NODE_DOMAIN:-}"
MODE="check"
QUIET=0
for a in "$@"; do
    case "$a" in
        --list)  MODE="list" ;;
        --quiet) QUIET=1 ;;
        --help|-h) sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    esac
done

say() { [ "$QUIET" = 1 ] || echo "$*"; }

# The node's own domain, so we can tell "mine" from "public". Prefer the routing
# contract; fall back to the hostname. Getting this wrong only makes the report
# noisier, never quieter -- an unknown domain means everything reads as public.
if [[ -z "$NODE_DOMAIN" && -r /etc/smart-node/routing.env ]]; then
    NODE_DOMAIN=$(sed -n 's/^SMART_NODE_DOMAIN=//p' /etc/smart-node/routing.env | tr -d '"' | head -1)
fi
[[ -z "$NODE_DOMAIN" ]] && NODE_DOMAIN=$(hostname -f 2>/dev/null || hostname)

# host -> where its rule lives. Two sources, and the difference is the point.
declare -A ORIGIN=()

collect_files() {
    [[ -d "$DYNAMIC_DIR" ]] || return 0
    local f h
    for f in "$DYNAMIC_DIR"/*.yml "$DYNAMIC_DIR"/*.yaml; do
        [[ -e "$f" ]] || continue
        while read -r h; do
            [[ -n "$h" ]] && ORIGIN["$h"]="file:$(basename "$f")"
        done < <(grep -ohE 'Host\(`[^`]+`\)' "$f" 2>/dev/null | sed -E 's/Host\(`([^`]+)`\)/\1/')
    done
}

collect_labels() {
    command -v docker >/dev/null 2>&1 || return 0
    local c labels h
    # Every label of every running container, then every Host() in any of them.
    # Deliberately NOT guessing which label carries the rule from the container's
    # name: the router name is arbitrary, and an earlier version of this that
    # derived it found nothing at all while a rule sat right there.
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        labels=$(docker inspect "$c" --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}
{{end}}' 2>/dev/null | grep -E '\.rule=')
        [[ -z "$labels" ]] && continue
        while read -r h; do
            # A file wins the attribution: if the same host is in both, the file
            # is what keeps it and the label is redundant rather than fragile.
            [[ -n "$h" && -z "${ORIGIN[$h]:-}" ]] && ORIGIN["$h"]="label:$c"
        done < <(grep -ohE 'Host\(`[^`]+`\)' <<<"$labels" | sed -E 's/Host\(`([^`]+)`\)/\1/')
    done
}

collect_files
collect_labels

public=()
for h in "${!ORIGIN[@]}"; do
    [[ "$h" == *"$NODE_DOMAIN" ]] && continue     # the node's own name: not public
    # Any .vpn name is internal. NOT `.sn.vpn`: that was the first version and it
    # reported six of a node's own internal names as unreachable public ones,
    # because that node's internal estate uses a different second label.
    # A genuinely public name never ends in .vpn, so the broad match is both
    # safer and less wrong than enumerating estates.
    [[ "$h" == *.vpn ]] && continue
    public+=("$h")
done

echo "[public-names] node domain: $NODE_DOMAIN"
if [[ ${#public[@]} -eq 0 ]]; then
    echo "[public-names] this node claims no public hostnames -- nothing to check"
    exit 0
fi

if [[ "$MODE" == "list" ]]; then
    for h in $(printf '%s\n' "${public[@]}" | sort); do
        printf "  %-34s %s\n" "$h" "${ORIGIN[$h]}"
    done
    exit 0
fi

fail=0
warn=0
for h in $(printf '%s\n' "${public[@]}" | sort); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "https://$h" 2>/dev/null)
    origin="${ORIGIN[$h]}"
    # 2xx and 3xx both count: every name but the door redirects TO the door.
    if [[ "$code" =~ ^[23] ]]; then
        state="ok"
    else
        state="FAIL"; fail=$((fail + 1))
    fi
    note=""
    if [[ "$origin" == label:* ]]; then
        note="  <- ON A CONTAINER LABEL: the installer rewrites this on the next update"
        warn=$((warn + 1))
    fi
    [[ "$state" == "ok" && "$QUIET" == 1 ]] || printf "  %-6s %-34s %-4s %-28s%s\n" "$state" "$h" "$code" "$origin" "$note"
done

echo "[public-names] $((${#public[@]} - fail))/${#public[@]} answering, $warn on container labels"
if (( warn > 0 )); then
    echo "[public-names] move the label-based ones into $DYNAMIC_DIR -- see docs/public-domain.md."
    echo "[public-names] they answer today and will stop, silently, at the next node update."
fi
exit $(( fail > 0 ? 1 : 0 ))
