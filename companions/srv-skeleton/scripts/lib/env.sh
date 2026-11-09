#!/usr/bin/env bash
# Shared .env loading. Source, do not execute.
#   load_env <file>   export KEY=VALUE pairs from <file> (defaults to /srv/secrets dir)
# No fallbacks: a missing file is an error (fail loud, do not silently continue).

load_env() {
  local file="${1:?load_env: path to .env required}"
  if [ ! -f "$file" ]; then
    echo "load_env: env file not found: $file" >&2
    return 1
  fi
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
}
