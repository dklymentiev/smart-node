#!/usr/bin/env bash
# Real CLI parser -> gateway completion -> brin -> external outbox boundary.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/companions/communication/tests/final_answer_regression.py" "$@"
