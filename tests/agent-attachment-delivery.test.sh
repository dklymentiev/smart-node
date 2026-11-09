#!/usr/bin/env bash
# The second half of a desk attachment reaches the model, not merely the
# textual transcript. Supports --baseline REF to demonstrate RED on main.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0
ok() { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/brin/core/attachment_delivery.py > "$D/delivery.py"
read_src companions/brin/core/executor.py > "$D/executor.py"
read_src companions/srv-skeleton/tools/attachments/attachment-read.py > "$D/reader.py"
read_src companions/core-api/routes/attachments.py > "$D/attachments.py"
read_src companions/core-api/lib/agent_fs.py > "$D/agent_fs.py"
read_src companions/core-api.sh > "$D/core-api.sh"
read_src companions/dispatcher.sh > "$D/dispatcher.sh"
read_src companions/brin/retire_attachment_stopgaps.py > "$D/retire.py"

if [[ -n "$BASELINE" ]]; then echo "agent attachment delivery (git:$BASELINE)"
else echo "agent attachment delivery (working tree)"; fi

if [[ -s "$D/delivery.py" ]] \
   && grep -q 'split_trailing_attachments' "$D/delivery.py" \
   && grep -q '/srv/tools/attachments/attachment-read.py' "$D/delivery.py"; then
    ok "brin replaces trailing handles with one exact retrieval command"
else
    bad "brin replaces trailing handles with one exact retrieval command"
fi

if [[ -s "$D/reader.py" ]] \
   && grep -q 'AF_UNIX' "$D/reader.py" \
   && grep -q 'core-api.sock' "$D/reader.py" \
   && ! grep -q '/srv/data/attachments' "$D/reader.py"; then
    ok "the helper is kernel-identified and never opens the store directly"
else
    bad "the helper is kernel-identified and never opens the store directly"
fi

if grep -q 'application/pdf.*\.pdf' "$D/reader.py" \
   && grep -q 'image/png.*\.png' "$D/reader.py" \
   && grep -q 'smart-node-attachments-%s' "$D/reader.py" \
   && grep -q 'os.chmod(root, 0o700)' "$D/reader.py" \
   && grep -q 'os.chmod(temp, 0o600)' "$D/reader.py"; then
    ok "materialized names carry type and each agent's PrivateTmp cache is closed"
else
    bad "materialized names carry type and each agent's PrivateTmp cache is closed"
fi

python3 - "$D/reader.py" <<'PY'
import sys
s = open(sys.argv[1], encoding="utf-8").read()
audio = s.find('content_type.startswith(("audio/", "video/"))')
fetch = s.find('request("/api/attachments/%s/content"')
honest = s.find('audio and video cannot be inspected')
raise SystemExit(0 if -1 not in (audio, fetch, honest) and audio < fetch else 1)
PY
if [[ $? -eq 0 ]]; then
    ok "audio/video is refused honestly before content is fetched"
else
    bad "audio/video is refused honestly before content is fetched"
fi

if grep -q 'message_to_send = await content_for_agent' "$D/executor.py" \
   && grep -q 'if _default_agent and _thread:' "$D/executor.py"; then
    ok "desk turns replace handles with instructions immediately before the model"
else
    bad "desk turns replace handles with instructions immediately before the model"
fi

if grep -q 'row\["status"\] not in ("approved", "processed")' "$D/attachments.py" \
   && grep -q "attachment is {row\['status'\]}, not approved" "$D/attachments.py"; then
    ok "the generic API door refuses received and unknown statuses"
else
    bad "the generic API door refuses received and unknown statuses"
fi

if grep -q 'attachment-read.py <id>' "$D/agent_fs.py" \
   && grep -q 'attachment reader installed' "$D/core-api.sh" \
   && grep -q 'test -x /srv/tools/attachments/attachment-read.py' "$D/core-api.sh" \
   && ! grep -q 'STAGE_VERSION="0.4.17"' "$D/core-api.sh" \
   && ! grep -q 'STAGE_VERSION="0.2.6"' "$D/dispatcher.sh" \
   && grep -q 'retire_attachment_stopgaps' "$D/dispatcher.sh" \
   && grep -q 'DATA_ROOT.*\/srv\/data' "$D/retire.py" \
   && grep -q 'os.walk(desks, followlinks=False)' "$D/retire.py" \
   && grep -q 'd:g:smart-node-agents' "$D/retire.py" \
   && grep -q 'Read(/srv/data/attachments/\*\*)' "$D/retire.py"; then
    ok "both versioned installers ship the code and the canonical prompt explains it"
else
    bad "both versioned installers ship the code and the canonical prompt explains it"
fi

echo
if (( FAILED )); then echo "FAIL ($FAILED)"; exit 1; fi
echo "PASS"
