#!/usr/bin/env bash
# Real CLI stream parser, context gate and persistence boundary; no provider or DB.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/companions/gateway/tests/request_usage_regression.py" "$@"
