#!/usr/bin/env bash
# The sn_core message trigger must seed state in the same schema as its message.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL="$ROOT/bootstrap/B02-sn-core-schema.sql"
grep -qE 'INSERT INTO sn_core\.message_state \(message_id, status\)' "$SQL"
grep -qE 'messages_seed_state_trg AFTER INSERT ON sn_core\.messages FOR EACH ROW EXECUTE FUNCTION sn_core\.messages_seed_state\(\)' "$SQL"
echo 'ok   sn_core message inserts seed sn_core state'
