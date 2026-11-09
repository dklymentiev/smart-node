#!/usr/bin/env bash
# Shared logging helpers. Source, do not execute.
#   log_open <dir>   open a timestamped log in <dir> and point <dir>/latest.log at it
#   log_info <msg>   info line
#   log_error <msg>  error line (stderr)

_SN_LOG_FILE=""

_sn_ts()   { date "+%Y-%m-%d-%H%M%S"; }
_sn_now()  { date "+%Y-%m-%d %H:%M:%S"; }

log_open() {
  local dir="${1:-logs}"
  mkdir -p "$dir"
  _SN_LOG_FILE="${dir}/$(_sn_ts).log"
  : > "$_SN_LOG_FILE"
  # Update latest.log symlink to the most recent run.
  ln -sfn "$(basename "$_SN_LOG_FILE")" "${dir}/latest.log"
}

_sn_emit() {
  local level="$1"; shift
  local line; line="$(_sn_now) [${level}] $*"
  if [ -n "$_SN_LOG_FILE" ]; then
    printf '%s\n' "$line" | tee -a "$_SN_LOG_FILE" >&2
  else
    printf '%s\n' "$line" >&2
  fi
}

log_info()  { _sn_emit "INFO"  "$@"; }
log_error() { _sn_emit "ERROR" "$@"; }
