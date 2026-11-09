#!/usr/bin/env bash
# Execute the binding/routing code and its SQL against conflicting registry rows.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$ROOT/tests/mailbox_receiver_guard.py" "$ROOT" "$@"
