#!/usr/bin/env bash
#
# capabilities-check.sh -- the calls quoted to agents must exist in the source.
#
# Two files carry this design and they are read by different audiences:
#
#   docs/node/entities.md          the contract. One row per thing the node is
#                                  made of, with the exact call and the gate.
#                                  THE SOURCE.
#   node-versioning/capabilities.md the list in the owner's language, composed
#                                  into the CONFIGURATOR's prompt on every node
#                                  at install and at recompose. Only that
#                                  specialist: agent_fs.py gates it on the slug,
#                                  so this is not in every agent's prompt.
#
# capabilities.md quotes the calls so an agent knows how to do what it can name,
# instead of searching for it. Quoting means the two can drift, and a drifted
# call is worse than no call: the agent acts confidently on an endpoint that
# does not exist.
#
# This is deliberately a CHECK and not a generator. A capability is not a thing:
# "Read web pages" is a tool grant, "Agents" is an entity, "Email" is a channel
# binding plus a mailbox the node cannot create. Generating either file from the
# other would force a shape that fits neither, and would make a fourth home for
# a design that already lives in three places.
#
# Exit 0 = every call quoted to agents exists in the contract.
# Exit 1 = at least one does not. The offending line is printed.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAP="$ROOT/node-versioning/capabilities.md"
ENT="$ROOT/docs/node/entities.md"

fail=0

for f in "$CAP" "$ENT"; do
    if [[ ! -f "$f" ]]; then
        printf 'MISSING  %s\n' "$f"
        exit 1
    fi
done

# The contract, with backticks stripped so `POST /api/agents` matches the plain
# form quoted on the other side.
contract="$(tr -d '`' < "$ENT")"

# One capability line at a time, so a failure can name the capability rather
# than just the call.
while IFS= read -r line; do
    case "$line" in
        *'<!-- call:'*) ;;
        *) continue ;;
    esac

    # The owner-facing name: the first **bold** run on the line.
    name="$(printf '%s' "$line" | sed -n 's/^[^*]*\*\*\([^*]*\)\*\*.*/\1/p')"
    [[ -z "$name" ]] && name='(unnamed)'

    # Everything between "call:" and either "| gate:" or the end of the comment.
    calls="$(printf '%s' "$line" \
        | sed -n 's/.*<!-- call:\(.*\)-->.*/\1/p' \
        | sed 's/| *gate:.*//')"

    # Capabilities may quote several calls, comma separated.
    IFS=',' read -ra items <<< "$calls"
    for item in "${items[@]}"; do
        # Trim, then drop anything after the endpoint itself: a payload
        # ({grants:{...}}), or an English tail (" with instance_ref = ...",
        # " field mesh_workspace"). A path segment like {slug} is kept because
        # it never follows a space.
        call="$(printf '%s' "$item" \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
            | sed 's/ {.*//; s/ with .*//; s/ field .*//; s/ (also .*//' \
            | tr -d '`')"
        [[ -z "$call" ]] && continue

        # HERESTRING, NOT A PIPE, AND THAT IS THE WHOLE POINT.
        # `printf ... | grep -qF` is a race under `set -o pipefail`: grep -q
        # exits at the first match, printf dies of SIGPIPE, and the PIPELINE
        # status becomes 141 -- so a call that IS in the contract reads as
        # missing. The contract is small enough to fit a pipe buffer, so printf
        # normally finishes before grep leaves and nothing shows; under load it
        # loses the race and the gate refuses a good release, naming a different
        # innocent capability each time. Cost two refused publications of 1.12.9
        # before it was believed, because every standalone run passed.
        if ! grep -qF -- "$call" <<< "$contract"; then
            printf 'NOT IN THE CONTRACT  %-24s  %s\n' "$name" "$call"
            fail=1
        fi
    done
done < "$CAP"

if [[ "$fail" -eq 0 ]]; then
    n="$(grep -c '<!-- call:' "$CAP")"
    printf 'OK  %s capabilities quote a call, and every call is in %s\n' \
        "$n" "docs/node/entities.md"
else
    printf '\nA capability quotes a call that docs/node/entities.md does not carry.\n'
    printf 'Fix the CONTRACT first, then the quote -- never the quote alone.\n'
fi

exit "$fail"
