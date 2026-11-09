#!/usr/bin/env bash
# Exercise actual gateway spawns and the wrapper with a local CLI stand-in.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/companions/gateway/tests/prompt_transport_regression.py" "$@"
