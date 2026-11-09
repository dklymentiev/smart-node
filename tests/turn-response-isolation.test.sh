#!/usr/bin/env bash
# Replay the actual /api/message handler and executor without external services.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/companions/brin/tests/turn_response_regression.py" "$@"
