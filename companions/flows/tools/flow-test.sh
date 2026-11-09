#!/usr/bin/env bash
#
# flow-test.sh -- lint every flow, then run the tests each flow carries.
#
# A flow is a directory, and everything that describes it lives inside:
#
#     flows/<name>/
#       <name>.yaml          the machine, with its version in metadata
#       README.md            the contract: edges, verdicts, effects, gaps
#       CHANGELOG.md         one entry per metadata.version
#       logic/               block scripts, if it has any
#       tests/*.test.sh      every edge and the negative cases, without a model
#
# So a flow can be copied to another node and still be checkable there. A flow
# is treated like an application: a flow without its README, CHANGELOG,
# metadata.version or tests is not done, and this FAILS it. This runs the lot:
#
#     flow-test.sh                 every flow in the tree beside these tools
#     flow-test.sh --flow NAME     one flow
#     flow-test.sh --root DIR      a tree somewhere else
#     flow-test.sh --quiet         one line per flow
#
# Exit code is 0 when every flow carries its contract, lints clean and every
# test passes.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${SN_FLOWS_AGENTS:-$(cd "$HERE/.." && pwd)/agents}"
ONE=""
QUIET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)  ROOT="$2"; shift 2 ;;
        --flow)  ONE="$2"; shift 2 ;;
        --quiet) QUIET=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# The interpreter the tools and the tests should use: the engine's venv on a
# node (it has PyYAML), whatever is on PATH in a checkout.
if [[ -z "${SN_FLOW_PY:-}" ]]; then
    if [[ -x /opt/smart-node/workflows/venv/bin/python ]]; then
        SN_FLOW_PY=/opt/smart-node/workflows/venv/bin/python
    else
        SN_FLOW_PY=python3
    fi
fi
export SN_FLOW_PY

FLOWS="$ROOT/flows"
[[ -d "$FLOWS" ]] || { echo "no flows under $FLOWS" >&2; exit 2; }

FAILED=0
TESTED=0
UNTESTED=()
INCOMPLETE=()

for dir in "$FLOWS"/*/; do
    name="$(basename "$dir")"
    [[ -n "$ONE" && "$name" != "$ONE" ]] && continue
    [[ -f "$dir/$name.yaml" ]] || continue

    lint_out="$("$SN_FLOW_PY" "$HERE/flow-lint.py" --root "$ROOT" --flow "$name" ${QUIET:+--quiet} 2>&1)"
    if [[ $? -eq 0 ]]; then
        (( QUIET )) || { echo "lint  $name  ok"; [[ -n "$lint_out" ]] && echo "$lint_out"; }
        (( QUIET )) && echo "lint  $name  ok"
    else
        echo "lint  $name  FAILED"
        echo "$lint_out"
        FAILED=$((FAILED + 1))
    fi

    # The contract. Each missing piece is a failure, not a remark.
    missing=()
    [[ -s "$dir/README.md" ]]    || missing+=("README.md")
    [[ -s "$dir/CHANGELOG.md" ]] || missing+=("CHANGELOG.md")
    "$SN_FLOW_PY" -c "import sys, yaml; wf = yaml.safe_load(open(sys.argv[1])) or {}; sys.exit(0 if ((wf.get('metadata') or {}).get('version')) else 1)" \
        "$dir/$name.yaml" 2>/dev/null || missing+=("metadata.version")

    shopt -s nullglob
    tests=("$dir"tests/*.test.sh)
    shopt -u nullglob
    [[ ${#tests[@]} -eq 0 ]] && missing+=("tests/")

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "contract  $name  FAILED: missing ${missing[*]}"
        INCOMPLETE+=("$name")
        FAILED=$((FAILED + 1))
    else
        (( QUIET )) || echo "contract  $name  ok"
    fi
    if [[ ${#tests[@]} -eq 0 ]]; then
        UNTESTED+=("$name")
        continue
    fi
    for t in "${tests[@]}"; do
        TESTED=$((TESTED + 1))
        if (( QUIET )); then
            if SN_FLOW_TOOLS="$HERE" "$t" >/dev/null 2>&1; then
                echo "test  $name/$(basename "$t")  ok"
            else
                echo "test  $name/$(basename "$t")  FAILED"
                FAILED=$((FAILED + 1))
            fi
        else
            echo
            SN_FLOW_TOOLS="$HERE" "$t" || FAILED=$((FAILED + 1))
        fi
    done
done

echo
if [[ ${#INCOMPLETE[@]} -gt 0 ]]; then
    # An error: a flow without its contract or its tests is a process nobody
    # can reason about or has walked.
    echo "incomplete contract: ${INCOMPLETE[*]}"
fi
if [[ ${#UNTESTED[@]} -gt 0 ]]; then
    echo "no tests: ${UNTESTED[*]}"
fi
echo "$TESTED test file(s) run | $FAILED failure(s)"
exit $(( FAILED > 0 ))
