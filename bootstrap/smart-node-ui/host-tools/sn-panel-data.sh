#!/bin/bash
# /usr/local/sbin/sn-panel-data.sh
# Renders panel JSON snapshots:
#   /srv/smart-node-hq/data/overview.json
#   /srv/smart-node-hq/data/events.json
#   /srv/smart-node-hq/data/services.json
# Run by sn-panel-data.service (systemd timer, every 30s).
# Reads Postgres via peer auth (unix root -> pg smartnode via pg_ident map).

set -euo pipefail

OUTDIR="/srv/smart-node-hq/data"
mkdir -p "$OUTDIR"
chmod 755 "$OUTDIR"

write_atomic() {
  mv "${1}.tmp" "${1}"
  chmod 644 "${1}"
}

GEN=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ---------------------------------------------------------------- overview
UPTIME=$(awk '{printf "%d", $1}' /proc/uptime)
read -r L1 L5 L15 _ < /proc/loadavg
MT=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)
MA=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
MU=$((MT - MA))

read -r DUSED DTOT DPCT < <(df -BG --output=used,size,pcent / | tail -1)
DUSED=${DUSED%G}
DTOT=${DTOT%G}
DPCT=${DPCT%\%}

NAMES_JSON=$(docker ps --format '{{.Names}}' | jq -R . | jq -s .)
N_RUN=$(echo "$NAMES_JSON" | jq 'length')

E24=$(psql -U smartnode -d smartnode -tAc \
  "SELECT count(*) FROM sn_core.events WHERE ts > now() - interval '24 hours'")

jq -n \
  --argjson up "$UPTIME" \
  --arg l1 "$L1" --arg l5 "$L5" --arg l15 "$L15" \
  --argjson mu "$MU" --argjson mt "$MT" \
  --argjson du "$DUSED" --argjson dt "$DTOT" --argjson dp "$DPCT" \
  --argjson nrun "$N_RUN" --argjson names "$NAMES_JSON" \
  --argjson e24 "$E24" \
  --arg gen "$GEN" \
  '{
    uptime_seconds: $up,
    load: { "1m": ($l1|tonumber), "5m": ($l5|tonumber), "15m": ($l15|tonumber) },
    mem:  { used_mb: $mu, total_mb: $mt },
    disk: { used_gb: $du, total_gb: $dt, pct: $dp },
    containers: { running: $nrun, names: $names },
    events_24h: $e24,
    generated_at: $gen
  }' > "$OUTDIR/overview.json.tmp"
write_atomic "$OUTDIR/overview.json"

# ---------------------------------------------------------------- events
psql -U smartnode -d smartnode -tAc \
  "SELECT row_to_json(t)::text FROM (
     SELECT ts, actor, event_type, entity_type, entity_id, payload
     FROM sn_core.events ORDER BY ts DESC LIMIT 200
   ) t" \
  | jq -s --arg gen "$GEN" '{ events: ., generated_at: $gen }' \
  > "$OUTDIR/events.json.tmp"
write_atomic "$OUTDIR/events.json"

# ---------------------------------------------------------------- messages
# Unified communication bus (email, telegram, future channels). Voice is
# not part of this stream.
psql -U smartnode -d smartnode -tAc \
  "SELECT row_to_json(t)::text FROM (
     SELECT id, channel_type, channel_id, direction, sender, recipient,
            substring(content, 1, 4000) AS content,
            received_at, thread_id, ai_session_id,
            metadata->>'subject'      AS subject,
            metadata->>'in_reply_to'  AS in_reply_to,
            metadata->>'from_name'    AS from_name
     FROM sn_core.messages ORDER BY id DESC LIMIT 500
   ) t" \
  | jq -s --arg gen "$GEN" '{ messages: ., generated_at: $gen }' \
  > "$OUTDIR/messages.json.tmp"
write_atomic "$OUTDIR/messages.json"

# ---------------------------------------------------------------- services
# Three sources merged into a single JSON.
COMPANIONS=$(
  if compgen -G "/etc/smart-node/companions.d/*.json" > /dev/null; then
    # Path: the companion's install tree, /opt/smart-node/<name>, when it has one.
    for f in /etc/smart-node/companions.d/*.json; do
      n=$(jq -r '.name' "$f"); p=""
      [ -d "/opt/smart-node/$n" ] && p="/opt/smart-node/$n"
      jq --arg p "$p" '. + {path: $p}' "$f"
    done | jq -s 'map({
      kind:    "companion",
      name:    .name,
      path:    .path,
      version: .version,
      state:   .state,
      deps:    (.deps // []),
      ts:      .ts,
      health:  (if .state == "installed" then "ok" else "warn" end)
    })'
  else
    echo "[]"
  fi
)

# Path of a container: the compose directory it is brought up from, else the
# host directory it keeps its data in (first bind mount under the node's own
# trees). All bind mounts go along for the tooltip.
CPATHS=$(
  docker ps -aq --filter name=^smart-node | xargs -r docker inspect \
    | jq 'map(
        ([.Mounts[] | select(.Type == "bind") | .Source | select(. != "/var/run/docker.sock")]) as $m
        | (.Config.Labels["com.docker.compose.project.working_dir"] // "") as $wd
        | {key: (.Name | ltrimstr("/")),
           value: {path: (if $wd != "" then $wd
                          else ([$m[] | select(test("^/(var/lib|opt)/smart-node/"))][0] // $m[0] // "") end),
                   mounts: $m}})
      | from_entries'
)

# docker ps with json per row
CONTAINERS=$(
  docker ps -a --format '{{json .}}' \
    | jq -s --argjson cp "$CPATHS" 'map(select(.Names | startswith("smart-node"))) |
             map({
               kind:    "container",
               name:    .Names,
               path:    ($cp[.Names].path // ""),
               mounts:  ($cp[.Names].mounts // []),
               version: .Image,
               state:   .State,
               status:  .Status,
               health:  (
                 if (.Status // "" | test("healthy"))                   then "ok"
                 elif (.Status // "" | test("unhealthy"))               then "fail"
                 elif (.State  // "" | test("running"))                 then "ok"
                 elif (.State  // "" | test("exited|dead"))             then "fail"
                 else "warn" end
               )
             })'
)

# Per unit, from one `systemctl show`:
# - path: the program it runs (the script, not the interpreter; a relative one
#   resolved against WorkingDirectory). A timer runs nothing itself, so its path
#   is its unit file. The unit file goes along for the tooltip.
# - timer + result: a service started by a timer runs, exits and sits
#   inactive/dead until the next tick, by design; its health is the Result of
#   its last run, not its ActiveState. Taken for every such service
#   whatever its state now: one that fires every 2 s is often mid-run here and
#   idle a moment later in the list below.
UPATHS=$(
  systemctl list-units --type=service,timer --plain --no-legend --all \
    'smart-node-*' 'sn-panel-data*' 'postgresql@16-smartnode.service' 2>/dev/null \
    | awk '{print $1}' \
    | xargs -r systemctl show -p Id -p ExecStart -p WorkingDirectory -p FragmentPath -p Result -p TriggeredBy \
    | jq -R -s '
        split("\n\n") | map(select(length > 0)
          | split("\n") | map(select(length > 0) | capture("^(?<key>[^=]+)=(?<value>.*)$")) | from_entries)
        | map(
            (((.ExecStart // "") | capture("argv\\[\\]=(?<a>[^;]*) ;")? // {a: ""}) | .a
              | split(" ") | map(select(length > 0))) as $argv
            | ($argv[0] // "") as $a0
            | (($argv | length) > 1 and ($a0 | test("/(python[0-9.]*|bash|sh|node|perl)$"))) as $interp
            # `python -m pkg`, a venv entry point (uvicorn, gunicorn) or a system
            # tool (curl) name no file of ours: the unit lives in its WorkingDirectory.
            | (if $interp and $argv[1] == "-m" then ""
               elif $interp then ([$argv[1:][] | select(startswith("-") | not)][0] // "")
               elif ($a0 | test("/venv/bin/|^/usr/s?bin/|^/bin/")) then ""
               else $a0 end) as $prog
            | (.WorkingDirectory // "") as $wd
            | (if $prog != "" and ($prog | startswith("/") | not) and ($wd | startswith("/"))
               then $wd + "/" + $prog else $prog end) as $p
            | {key: .Id, value: {path: (if $p != "" then $p
                                        elif ($wd | startswith("/")) then $wd
                                        else (.FragmentPath // "") end),
                                 unit_file: (.FragmentPath // ""),
                                 timer: (.TriggeredBy // ""),
                                 result: (.Result // "")}})
        | from_entries'
)

# systemd: smart-node-*, sn-panel-data*, postgresql@16-smartnode
SYSTEMD=$(
  systemctl list-units --type=service,timer --plain --no-legend --all \
    'smart-node-*' 'sn-panel-data*' 'postgresql@16-smartnode.service' 2>/dev/null \
    | awk '{ unit=$1; load=$2; active=$3; sub_=$4; $1=$2=$3=$4=""; sub(/^ +/,""); printf "{\"unit\":\"%s\",\"load\":\"%s\",\"active\":\"%s\",\"sub\":\"%s\",\"desc\":\"%s\"}\n", unit, load, active, sub_, $0 }' \
    | jq -s --argjson up "$UPATHS" 'map(. as $u
        | (($up[$u.unit] // {}) | if (.timer // "") != "" and ($u.unit | endswith(".service")) then . else null end) as $run | {
        kind:    "systemd",
        name:    .unit,
        path:    ($up[$u.unit].path // ""),
        unit_file: ($up[$u.unit].unit_file // ""),
        timer:   (if $run then $run.timer else null end),
        last_result: (if $run then $run.result else null end),
        version: "",
        state:   .active,
        status:  (if $run and .active == "inactive" then "idle, last run " + $run.result + " (" + $run.timer + ")"
                  else .active + "/" + .sub end),
        desc:    .desc,
        health:  (
          if .active == "active"      then "ok"
          elif .active == "failed"    then "fail"
          elif .active == "activating" then "ok"
          elif $run                   then (if $run.result == "success" then "ok" else "fail" end)
          else "warn" end
        )
      })'
)

jq -n \
  --argjson companions "$COMPANIONS" \
  --argjson containers "$CONTAINERS" \
  --argjson systemd    "$SYSTEMD" \
  --arg gen "$GEN" \
  '{
    companions: $companions,
    containers: $containers,
    systemd:    $systemd,
    summary: {
      companions_total:   ($companions | length),
      containers_running: ($containers | map(select(.health == "ok")) | length),
      containers_total:   ($containers | length),
      systemd_active:     ($systemd    | map(select(.health == "ok")) | length),
      systemd_total:      ($systemd    | length),
      issues:             (($companions + $containers + $systemd) | map(select(.health != "ok")) | length)
    },
    generated_at: $gen
  }' > "$OUTDIR/services.json.tmp"
write_atomic "$OUTDIR/services.json"

# ---------------------------------------------------------------- crons
# Systemd timers: list-timers --output=json (timestamps are microseconds-since-epoch)
TIMERS=$(
  systemctl list-timers --all --output=json 2>/dev/null \
    | jq 'map({
        kind:      "timer",
        name:      .unit,
        schedule:  "",
        target:    .activates,
        next_iso:  (if (.next  // 0) > 0 then ((.next  / 1000000) | strftime("%Y-%m-%dT%H:%M:%SZ")) else null end),
        last_iso:  (if (.last  // 0) > 0 then ((.last  / 1000000) | strftime("%Y-%m-%dT%H:%M:%SZ")) else null end),
        source:    "systemd",
        scope:     (if (.unit | test("^(smart-node-|sn-panel-data)")) then "smart-node" else "system" end)
      })'
)

# Fill schedule + description by querying each timer unit
TIMERS=$(
  echo "$TIMERS" | jq -c ".[]" | while read -r t; do
    unit=$(echo "$t" | jq -r .name)
    activates=$(echo "$t" | jq -r .target)
    on_calendar=$(systemctl show -p TimersCalendar --value "$unit" 2>/dev/null | head -1)
    on_active=$(systemctl show -p TimersMonotonic --value "$unit" 2>/dev/null | head -1)
    sched=""
    [ -n "$on_calendar" ] && sched="$on_calendar"
    [ -z "$sched" ] && [ -n "$on_active" ] && sched="$on_active"
    # Description: prefer the activated service's Description, fall back to timer's own
    desc=""
    if [ -n "$activates" ] && [ "$activates" != "null" ]; then
      desc=$(systemctl show -p Description --value "$activates" 2>/dev/null)
    fi
    [ -z "$desc" ] && desc=$(systemctl show -p Description --value "$unit" 2>/dev/null)
    echo "$t" | jq --arg s "$sched" --arg d "$desc" ".schedule = \$s | .description = \$d"
  done | jq -s "."
)

# Crontab entries -- /etc/crontab + /etc/cron.d/* (format: MIN HOUR DOM MON DOW USER CMD)
CRONTABS_TMP=$(mktemp)
parse_crontab() {
  local file="$1"
  awk -v f="$file" '
    function jsesc(s,   r) { r = s; gsub(/\\/, "\\\\", r); gsub(/"/, "\\\"", r); gsub(/\t/, "\\t", r); gsub(/[\001-\037\177]/, " ", r); return r }
    /^[[:space:]]*#/ {
      line = $0
      sub(/^[[:space:]]*#[[:space:]]?/, "", line)
      desc_buf = (desc_buf == "" ? line : desc_buf " " line)
      next
    }
    /^[[:space:]]*$/ { desc_buf = ""; next }
    /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/ { desc_buf = ""; next }
    $1 ~ /^@/ {
      sched = $1; user = $2
      $1=$2=""; sub(/^ +/, "")
      cmd = $0
      scope = (f ~ /smart-node/ ? "smart-node" : "system")
      printf "{\"kind\":\"crontab\",\"name\":\"%s\",\"schedule\":\"%s\",\"target\":\"%s\",\"next_iso\":null,\"last_iso\":null,\"source\":\"%s\",\"scope\":\"%s\",\"description\":\"%s\"}\n",
             user, sched, jsesc(cmd), f, scope, jsesc(desc_buf)
      desc_buf = ""
      next
    }
    {
      sched = $1 " " $2 " " $3 " " $4 " " $5
      user = $6
      $1=$2=$3=$4=$5=$6=""; sub(/^ +/, "")
      cmd = $0
      scope = (f ~ /smart-node/ ? "smart-node" : "system")
      if (desc_buf ~ /Example of job definition/) desc_buf = ""
      if (length(desc_buf) > 160) desc_buf = ""
      printf "{\"kind\":\"crontab\",\"name\":\"%s\",\"schedule\":\"%s\",\"target\":\"%s\",\"next_iso\":null,\"last_iso\":null,\"source\":\"%s\",\"scope\":\"%s\",\"description\":\"%s\"}\n",
             user, sched, jsesc(cmd), f, scope, jsesc(desc_buf)
      desc_buf = ""
    }' "$file"
}
[ -f /etc/crontab ] && parse_crontab /etc/crontab >> "$CRONTABS_TMP"
if [ -d /etc/cron.d ]; then
  for f in /etc/cron.d/*; do
    [ -f "$f" ] && parse_crontab "$f" >> "$CRONTABS_TMP"
  done
fi
# User crontabs at /var/spool/cron/crontabs/<user>
if [ -d /var/spool/cron/crontabs ]; then
  for f in /var/spool/cron/crontabs/*; do
    [ -f "$f" ] || continue
    user=$(basename "$f")
    awk -v f="$f" -v user="$user" '
      function jsesc(s,   r) { r = s; gsub(/\\/, "\\\\", r); gsub(/"/, "\\\"", r); gsub(/\t/, "\\t", r); gsub(/[\001-\037\177]/, " ", r); return r }
      /^[[:space:]]*#/ {
        line = $0
        sub(/^[[:space:]]*#[[:space:]]?/, "", line)
        desc_buf = (desc_buf == "" ? line : desc_buf " " line)
        next
      }
      /^[[:space:]]*$/ { desc_buf = ""; next }
      /^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=/ { desc_buf = ""; next }
      {
        sched = $1 " " $2 " " $3 " " $4 " " $5
        $1=$2=$3=$4=$5=""; sub(/^ +/, "")
        cmd = $0
        scope = (f ~ /smart-node/ ? "smart-node" : "system")
        # filter out boilerplate / format-help headers
        if (desc_buf ~ /Example of job definition/) desc_buf = ""
        if (length(desc_buf) > 160) desc_buf = ""
        printf "{\"kind\":\"crontab\",\"name\":\"%s\",\"schedule\":\"%s\",\"target\":\"%s\",\"next_iso\":null,\"last_iso\":null,\"source\":\"%s\",\"scope\":\"%s\",\"description\":\"%s\"}\n",
               user, sched, jsesc(cmd), f, scope, jsesc(desc_buf)
        desc_buf = ""
      }' "$f" >> "$CRONTABS_TMP"
  done
fi
CRONTABS=$(jq -R 'fromjson? // empty' "$CRONTABS_TMP" 2>/dev/null | jq -s '.' 2>/dev/null || echo "[]")
rm -f "$CRONTABS_TMP"

# Next-run across the union
NEXT_RUN=$(echo "$TIMERS" | jq -r '[.[] | select(.next_iso) | .next_iso] | min // null')

jq -n \
  --argjson timers "$TIMERS" \
  --argjson crontabs "$CRONTABS" \
  --arg next "$NEXT_RUN" \
  --arg gen "$GEN" \
  '{
    timers:   $timers,
    crontabs: $crontabs,
    summary: {
      timers_total:    ($timers   | length),
      crontab_entries: ($crontabs | length),
      next_run:        (if $next == "" or $next == "null" then null else $next end),
      issues:          0
    },
    generated_at: $gen
  }' > "$OUTDIR/crons.json.tmp"
write_atomic "$OUTDIR/crons.json"

# ---------------------------------------------------------------- ai gateway
# Pulls from local ai-gateway (127.0.0.1:8000). Each curl is bounded; on
# failure we still emit a valid ai.json so the page renders "gateway unreachable".
GW="http://127.0.0.1:8000/api/v1"
HEALTH_R=$(curl -sS -m 3 "$GW/health"        2>/dev/null || echo "")
PROVS_R=$(curl  -sS -m 3 "$GW/providers"     2>/dev/null || echo "")
CACHE_R=$(curl  -sS -m 3 "$GW/cache/stats"   2>/dev/null || echo "")

# v2 sessions: session-first gateway sessions.
# DB-backed in sn_core.ai_sessions; v1 in-memory map deprecated and unused
# by panel from 2026-05-24. Shape matches /assets/shell.js renderer:
# session_id / service / model / created_at / last_activity / message_count.
SESSIONS=$(psql -U smartnode -d smartnode -tAc "
  SELECT COALESCE(json_agg(row_to_json(t) ORDER BY t.last_activity DESC), '[]'::json)::text
    FROM (
      SELECT
        s.id::text                                                  AS session_id,
        s.provider                                                  AS service,
        s.model                                                     AS model,
        s.status                                                    AS status,
        to_char(s.created_at        AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at,
        to_char(s.last_activity_at  AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS last_activity,
        COALESCE(m.cnt, 0)                                          AS message_count,
        s.usage                                                     AS usage,
        s.is_opaque                                                 AS is_opaque,
        -- Embed last 30 messages per session for inline detail view (no extra round-trips).
        -- Content truncated to 800 chars; assistant usage kept; rest stripped.
        COALESCE(mm.messages, '[]'::json)                           AS messages
      FROM sn_core.ai_sessions s
      LEFT JOIN (
        SELECT session_id, count(*) AS cnt
          FROM sn_core.ai_session_messages
        GROUP BY session_id
      ) m ON m.session_id = s.id
      LEFT JOIN LATERAL (
        SELECT json_agg(row_to_json(mr) ORDER BY mr.seq) AS messages
        FROM (
          SELECT
            seq,
            role,
            CASE WHEN length(content) > 800
                 THEN substring(content from 1 for 800) || '…'
                 ELSE content END                                   AS content,
            to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at,
            usage
          FROM sn_core.ai_session_messages
          WHERE session_id = s.id
          ORDER BY seq DESC
          LIMIT 30
        ) mr
      ) mm ON true
      ORDER BY s.last_activity_at DESC
      LIMIT 20
    ) t
" 2>/dev/null)
[ -z "$SESSIONS" ] && SESSIONS='[]'
# Validate; reset to [] on parse failure
SESSIONS=$(echo "$SESSIONS" | jq '.' 2>/dev/null || echo '[]')

# Sanitise / default
HEALTH=$(echo "$HEALTH_R" | jq '.' 2>/dev/null || echo '{}')
PROVS=$( echo "$PROVS_R"  | jq '.providers // []' 2>/dev/null || echo '[]')
CACHE=$( echo "$CACHE_R"  | jq '.' 2>/dev/null || echo '{}')

# Overall status: healthy from gateway, or "unreachable" if no body
OVERALL=$(echo "$HEALTH" | jq -r '.status // "unreachable"')

# Merge runtime status from /health into provider records (matched by id)
PROVS_MERGED=$(jq -n --argjson p "$PROVS" --argjson h "$HEALTH" '
  $p | map(
    . as $base
    | ($h.providers[$base.id] // {}) as $rt
    | $base + {
        runtime_status:  ($rt.status         // "unknown"),
        active_sessions: ($rt.active_sessions // null),
        error:           ($rt.error           // null)
      }
  )
')

# Counts. active_sessions summary card counts only status='active' (lifecycle
# 'active' or 'streaming' or 'idle_released') -- terminal sessions don't pile up.
ACTIVE_SESS=$(echo "$SESSIONS" | jq '[.[] | select(.status == "active" or .status == "streaming" or .status == "idle_released")] | length')
P_TOTAL=$(   echo "$PROVS_MERGED" | jq 'length')
P_OK=$(      echo "$PROVS_MERGED" | jq '[.[] | select(.runtime_status == "ok")] | length')

# Stage 6: budgets snapshot. Read all enabled budgets with derived pct.
# Gateway resets period_reset_at on traffic; renderer is read-only.
BUDGETS=$(psql -U smartnode -d smartnode -tAc "
  SELECT COALESCE(json_agg(row_to_json(b) ORDER BY b.scope_type, b.scope_id), '[]'::json)::text
    FROM (
      SELECT
        id,
        scope_type,
        scope_id,
        period,
        limit_usd::float8                                           AS limit_usd,
        current_spend_usd::float8                                   AS current_spend_usd,
        CASE WHEN limit_usd > 0
             THEN ROUND((current_spend_usd / limit_usd * 100.0)::numeric, 2)::float8
             ELSE 0::float8
        END                                                         AS pct,
        hard_limit                                                  AS hard,
        (current_spend_usd >= limit_usd)                            AS over,
        to_char(period_reset_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS period_reset_at,
        enabled
      FROM sn_config.budgets
      WHERE enabled = true
      ORDER BY scope_type, scope_id
    ) b
" 2>/dev/null)
[ -z "$BUDGETS" ] && BUDGETS='[]'
BUDGETS=$(echo "$BUDGETS" | jq '.' 2>/dev/null || echo '[]')

# Cache hit rate
C_HITS=$(  echo "$CACHE" | jq '.hits   // 0')
C_MISSES=$(echo "$CACHE" | jq '.misses // 0')
C_LOOK=$(( C_HITS + C_MISSES ))
if [ "$C_LOOK" -gt 0 ]; then
  C_PCT=$(( C_HITS * 100 / C_LOOK ))
else
  C_PCT=0
fi
C_ENTRIES=$(echo "$CACHE" | jq '.entries // 0')

# SESSIONS goes to jq from a file, not as --argjson: it carries the last 20
# sessions with up to 30 messages each, and Linux caps ONE argument at 128 KB
# (MAX_ARG_STRLEN). Past that jq never starts ("Argument list too long", exit
# 126), the unit fails and install validate fails with it (example-node from
# 2026-09-15). The other arguments here are counters and short lists.
SESSIONS_TMP=$(mktemp)
printf '%s' "$SESSIONS" > "$SESSIONS_TMP"
jq -n \
  --arg gen "$GEN" \
  --arg status "$OVERALL" \
  --argjson active "$ACTIVE_SESS" \
  --argjson ptot "$P_TOTAL" \
  --argjson pok "$P_OK" \
  --argjson chits "$C_HITS" \
  --argjson clook "$C_LOOK" \
  --argjson cpct "$C_PCT" \
  --argjson centries "$C_ENTRIES" \
  --argjson providers "$PROVS_MERGED" \
  --slurpfile sessions "$SESSIONS_TMP" \
  --argjson budgets "$BUDGETS" \
  '{
    generated_at: $gen,
    summary: {
      status:           $status,
      active_sessions:  $active,
      providers_total:  $ptot,
      providers_ok:     $pok,
      cache_hits:       $chits,
      cache_lookups:    $clook,
      cache_hit_pct:    $cpct,
      cache_entries:    $centries
    },
    providers: $providers,
    sessions:  $sessions[0],
    budgets:   $budgets
  }' > "$OUTDIR/ai.json.tmp"
rm -f "$SESSIONS_TMP"
write_atomic "$OUTDIR/ai.json"

# ---------------------------------------------------------------- agents
# Smart Node agent format v0.1.0
#   /srv/smart-node/agents/<name>/agent.yaml  -- structured descriptor (source of truth)
#   /srv/smart-node/agents/<name>/CLAUDE.md   -- LLM instructions (not parsed by panel)
#   /srv/smart-node/agents/<name>/DISABLED    -- presence pauses the agent
#   /srv/smart-node/agents/<name>/logs/latest.log -- mtime = last run
# Directories starting with `_` (e.g. _template) are skipped.
# Legacy fallback: dir with CLAUDE.md but no agent.yaml is rendered with minimal
# info + a schema_warning so it shows up while being migrated to the new schema.
AGENTS_DIR="/srv/smart-node/agents"
AGENTS_TMP=$(mktemp)

agent_yaml_get() {
  # Flat-scalar YAML reader for the v0.1.0 agent.yaml schema.
  # No python-yaml dep; handles "key: value", quoted strings, trailing # comments.
  # $1 = yaml file path, $2 = top-level scalar key.
  local line val
  line=$(grep -m1 "^${2}:" "$1" 2>/dev/null) || return 0
  val=$(printf '%s' "$line" | sed -E "s/^${2}:[[:space:]]*//; s/[[:space:]]*#.*\$//; s/^[\"'](.*)[\"']\$/\1/; s/[[:space:]]+\$//")
  [ "$val" = "null" ] && val=""
  printf '%s\n' "$val"
}

if [ -d "$AGENTS_DIR" ]; then
  for d in "$AGENTS_DIR"/*/; do
    [ -d "$d" ] || continue
    aname=$(basename "$d")
    # Skip internal/template dirs (any starting with _)
    [[ "$aname" == _* ]] && continue

    ayaml="${d}agent.yaml"
    aclaude="${d}CLAUDE.md"
    has_yaml=false
    has_claude=false
    [ -f "$ayaml" ] && has_yaml=true
    [ -f "$aclaude" ] && has_claude=true
    # need at least one to count as an agent
    if ! $has_yaml && ! $has_claude; then continue; fi

    # status (DISABLED flag wins, then yaml's enabled:false)
    astatus="active"
    if [ -f "${d}DISABLED" ]; then
      astatus="disabled"
    elif $has_yaml; then
      aenabled=$(agent_yaml_get "$ayaml" "enabled")
      [ "$aenabled" = "false" ] && astatus="disabled"
    fi

    # last run mtime
    alast=""
    if [ -f "${d}logs/latest.log" ]; then
      alast=$(date -u -d "@$(stat -c %Y "${d}logs/latest.log")" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || true)
    fi

    if $has_yaml; then
      atitle=$(agent_yaml_get "$ayaml" "title")
      adesc=$( agent_yaml_get "$ayaml" "description")
      aicon=$( agent_yaml_get "$ayaml" "icon")
      amodel=$(agent_yaml_get "$ayaml" "model")
      asched=$(agent_yaml_get "$ayaml" "schedule")
      adept=$( agent_yaml_get "$ayaml" "department")
      aver=$(  agent_yaml_get "$ayaml" "version")
      aschver=$(agent_yaml_get "$ayaml" "schema_version")
      ayname=$( agent_yaml_get "$ayaml" "name")
      [ -z "$ayname" ] && ayname="$aname"
      [ -z "$atitle" ] && atitle="$aname"
      [ -z "$aicon"  ] && aicon="cpu"
      awarn=""
      [ "$ayname" != "$aname" ] && awarn="agent.yaml name '${ayname}' does not match directory basename '${aname}'"
      jq -n \
        --arg name "$aname" \
        --arg path "${d%/}" \
        --arg title "$atitle" \
        --arg desc "$adesc" \
        --arg icon "$aicon" \
        --arg model "$amodel" \
        --arg sched "$asched" \
        --arg dept "$adept" \
        --arg version "$aver" \
        --arg schema "$aschver" \
        --arg last "$alast" \
        --arg status "$astatus" \
        --arg warn "$awarn" \
        '{
          name: $name,
          path: $path,
          title: $title,
          description: $desc,
          icon: $icon,
          model: $model,
          schedule: $sched,
          department: (if $dept == "" then null else $dept end),
          version: (if $version == "" then null else $version end),
          schema_version: (if $schema == "" then null else $schema end),
          last_run_iso: (if $last == "" then null else $last end),
          status: $status,
          source: "yaml",
          warning: (if $warn == "" then null else $warn end)
        }' >> "$AGENTS_TMP"
    else
      # Legacy fallback: no agent.yaml, only CLAUDE.md present
      adesc=$(awk 'NF && !/^[[:space:]]*#/ {print; exit}' "$aclaude" 2>/dev/null || true)
      adesc=$(printf '%s' "$adesc" | cut -c1-160)
      asched=""
      if [ -f "${d}cron" ]; then
        asched=$(grep -v '^[[:space:]]*$\|^[[:space:]]*#' "${d}cron" 2>/dev/null | head -1 || true)
      fi
      jq -n \
        --arg name "$aname" \
        --arg path "${d%/}" \
        --arg title "$aname" \
        --arg desc "$adesc" \
        --arg sched "$asched" \
        --arg last "$alast" \
        --arg status "$astatus" \
        '{
          name: $name,
          path: $path,
          title: $title,
          description: $desc,
          icon: "cpu",
          model: "",
          schedule: $sched,
          department: null,
          version: null,
          schema_version: null,
          last_run_iso: (if $last == "" then null else $last end),
          status: $status,
          source: "legacy",
          warning: "no agent.yaml -- migrate to format v0.1.0 (cp _template/agent.yaml here, then fill)"
        }' >> "$AGENTS_TMP"
    fi
  done
fi
AGENTS=$(jq -s '. | sort_by((if .status == "active" then 0 else 1 end), (.department // ""), .name)' "$AGENTS_TMP" 2>/dev/null || echo "[]")
rm -f "$AGENTS_TMP"

ATOTAL=$(   echo "$AGENTS" | jq 'length')
AACTIVE=$(  echo "$AGENTS" | jq '[.[] | select(.status == "active")]   | length')
ADISABLED=$(echo "$AGENTS" | jq '[.[] | select(.status == "disabled")] | length')

jq -n \
  --argjson agents "$AGENTS" \
  --argjson total "$ATOTAL" \
  --argjson active "$AACTIVE" \
  --argjson disabled "$ADISABLED" \
  --arg gen "$GEN" \
  '{
    generated_at: $gen,
    summary: {
      agents_total:    $total,
      agents_active:   $active,
      agents_disabled: $disabled
    },
    agents: $agents
  }' > "$OUTDIR/agents.json.tmp"
write_atomic "$OUTDIR/agents.json"
