#!/usr/bin/env bash
# Exercise the shipped create path with a transactional registry, actual
# filesystem provisioning and a controlled privileged operation. No live node.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
exec python3 "$ROOT/tests/agent_create_system_user.py" "$ROOT" "$@"
