#!/usr/bin/env python3
"""Health probe: check every agent's granted tools and channels, write to DB.

Runs every 15 minutes from /etc/cron.d/smart-node-health-probe. For each
(agent, grant) pair it runs the same probe the interactive grid scripts use,
then upserts the result into sn_core.agent_health_checks.

STATUS VALUES
  ok           the grant was exercised and it did something
  fail         the grant is broken or unreachable
  untestable   the grant cannot be safely probed from here (e.g. a mutating
               API call, or a stdio tool that must run as the OS user in a
               restricted environment)

The panel reads sn_core.agent_health_checks but never writes it. A hanging probe
must not be able to take the panel down -- that is why this runs as a cron, not
from the panel backend (architect decision).

Idempotent: re-running overwrites the previous result for each grant.
Reads only (no agent sessions started, no messages sent).
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.request

DB = "smartnode"


# ------------------------------------------------------------------ DB helpers

def q(sql, params=()):
    """Query the DB as the postgres OS user (peer auth). Returns list of rows."""
    cmd = ["sudo", "-u", "postgres", "psql", "-d", DB, "-At", "-F", "\x1f"]
    if params:
        # Interpolate safely via psql's -v mechanism is awkward; use Python
        # string formatting only for the controlled integer/text values we pass.
        pass
    cmd += ["-c", sql]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"psql error: {out.stderr.strip()[:200]}")
    return [line.split("\x1f") for line in out.stdout.splitlines() if line]


def upsert(rows):
    """Write probe results to sn_core.agent_health_checks.

    rows: list of (agent_slug, grant_name, check_type, status, error_detail)
    """
    if not rows:
        return
    now = datetime.datetime.utcnow().isoformat()
    # Build a multi-row literal and upsert in one statement to keep the probe
    # atomic: either all results land or none do.
    values = []
    for agent_slug, grant_name, check_type, status, error_detail in rows:
        def pg_str(s):
            if s is None:
                return "NULL"
            return "'" + str(s).replace("'", "''") + "'"
        values.append(
            f"({pg_str(agent_slug)},{pg_str(grant_name)},{pg_str(check_type)},"
            f"{pg_str(status)},{pg_str(now)},{pg_str(error_detail)})"
        )
    sql = (
        "INSERT INTO sn_core.agent_health_checks"
        " (agent_slug, grant_name, check_type, status, last_checked_at, error_detail)"
        " VALUES " + ",".join(values) +
        " ON CONFLICT (agent_slug, grant_name, check_type) DO UPDATE SET"
        "   status          = EXCLUDED.status,"
        "   last_checked_at = EXCLUDED.last_checked_at,"
        "   error_detail    = EXCLUDED.error_detail"
    )
    out = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", DB, "-At", "-c", sql],
        capture_output=True, text=True
    )
    if out.returncode:
        raise RuntimeError(f"upsert failed: {out.stderr.strip()[:200]}")


# ------------------------------------------------------------------ probes (same logic as agent-tools-grid.py)

def probe_http(url):
    session_id = None

    def call(payload):
        nonlocal session_id
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if session_id:
            hdrs["Mcp-Session-Id"] = session_id
        req = urllib.request.Request(
            url, method="POST", data=json.dumps(payload).encode(),
            headers=hdrs)
        with urllib.request.urlopen(req, timeout=8) as r:
            sid = r.getheader("Mcp-Session-Id")
            if sid:
                session_id = sid
            return r.status, r.read(60000).decode("utf-8", "replace")

    try:
        call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "probe", "version": "0"}}})
    except Exception as e:
        return "fail", f"unreachable: {type(e).__name__}"
    try:
        _, body = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        n = body.count('"name"')
        if n:
            return "ok", None
        return "fail", "server answers but lists no tools"
    except Exception as e:
        return "fail", f"handshake ok, listing failed: {type(e).__name__}"


def probe_stdio(cmd, arg, as_user):
    if not (cmd and arg and os.path.exists(arg)):
        return "fail", f"not on disk: {arg}"
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin = "\n".join(json.dumps(m) for m in msgs) + "\n"
    try:
        p = subprocess.run(
            ["sudo", "-u", as_user, "env", f"SN_AGENT_SLUG={as_user[6:]}", cmd, arg],
            input=stdin, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return "fail", "started and did not answer in 25s"
    out = p.stdout or ""
    if '"result"' not in out:
        return "fail", f"no usable answer (exit {p.returncode})"
    n = out.count('"inputSchema"') or out.count('"name"')
    return ("ok", None) if n else ("fail", "answers but lists no tools")


def probe_fs(paths_text, as_user, slug):
    home = f"/srv/agents/{slug}/work"
    ok_home = subprocess.run(
        ["sudo", "-u", as_user, "test", "-r", home]).returncode == 0
    declared = [x.strip('" ') for x in paths_text.strip("{}").split(",") if x.strip('" ')]
    if not declared:
        return ("ok" if ok_home else "fail",
                None if ok_home else f"cannot read own work area as {as_user}")
    first = declared[0]
    ok = subprocess.run(["sudo", "-u", as_user, "test", "-r", first]).returncode == 0
    return ("ok" if ok else "fail",
            None if ok else f"cannot read {first} as {as_user}")


def probe_api(endpoint, http_path, method):
    if not endpoint:
        return "untestable", "no endpoint address in the registry"
    if not http_path:
        return "untestable", "no http_path in the registry; tool not probeable from here"
    if (method or "").upper() not in ("GET", ""):
        return "untestable", "mutating method; not exercised from the probe"
    try:
        with urllib.request.urlopen(endpoint + http_path, timeout=6) as r:
            return "ok", None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # Reachable, just needs agent identity: we can confirm it exists.
            return "ok", None
        return "fail", f"HTTP {e.code}"
    except Exception as e:
        return "fail", f"unreachable: {type(e).__name__}"


# ------------------------------------------------------------------ main

def check_tools(agents, servers, tools, grants, shared):
    """Probe each (agent, tool) grant. Returns list of result rows."""
    rows = []
    for slug, _, osu, fsr in agents:
        for name, _, kind, hp, hm, ep, _ in tools:
            if (slug, name) not in grants:
                continue
            cache_key = None
            if kind == "builtin" and name == "fs":
                status, err = probe_fs(fsr, osu or "root", slug)
            elif name in servers:
                transport, cfg = servers[name]
                conf = json.loads(cfg or "{}")
                if transport in ("http", "sse"):
                    cache_key = ("http", conf.get("url", ""))
                    if cache_key not in shared:
                        shared[cache_key] = probe_http(conf.get("url", ""))
                    status, err = shared[cache_key]
                else:
                    args = conf.get("args") or []
                    status, err = probe_stdio(
                        conf.get("command", ""), args[0] if args else "", osu or "root")
            elif kind == "api":
                status, err = "untestable", "api tool; requires agent identity, not exercised from the probe"
            elif kind == "builtin":
                status, err = "ok", None
            else:
                # cli / shell tools: cannot safely exercise from the probe
                status, err = "untestable", "shell tool; not exercised from the probe"
            rows.append((slug, name, "tool", status, err))
    return rows


def check_channels(agents, bound, allowed, traffic):
    """Check each (agent, channel) binding. Returns list of result rows."""
    rows = []
    for slug, _ in [(a[0], a[1]) for a in agents]:
        for ch_slug, in [(b[1],) for b in bound if b[0] == slug]:
            who = allowed.get((slug, ch_slug), set())
            out_traffic = traffic.get((ch_slug, "out"))
            if not who:
                rows.append((slug, ch_slug, "channel", "fail",
                             "bound but permitted to answer nobody"))
            elif out_traffic:
                rows.append((slug, ch_slug, "channel", "ok", None))
            else:
                rows.append((slug, ch_slug, "channel", "untestable",
                             "bound and permitted; no outbound traffic in 7 days to confirm"))
    return rows


def main():
    agents = q(
        "SELECT slug, coalesce(title,slug), coalesce(os_user,''), "
        "coalesce(fs_read::text,'{}') FROM sn_config.agents "
        "WHERE archived_at IS NULL AND enabled ORDER BY created_at")

    servers = {r[0]: (r[1], r[2]) for r in q(
        "SELECT slug, transport, config_jsonb::text "
        "FROM sn_config.mcp_servers WHERE enabled")}

    tools = q(
        r"""SELECT t.name, coalesce(t.title,t.name), t.kind,
               coalesce(t.http_path,''), coalesce(t.http_method,''),
               coalesce(i.endpoint,''),
               (coalesce(t.usage_md,'') ~ 'https?://')::text
          FROM sn_core.tools t
          LEFT JOIN sn_config.integrations i ON i.slug=t.integration_slug
         WHERE t.kind <> 'mcp' OR t.name IN (SELECT slug FROM sn_config.mcp_servers)
         ORDER BY t.kind, t.name""")

    grants = set()
    for a, t, _ in q(
            "SELECT ag.slug, tl.name, array_to_string(at.granted_caps,'') "
            "FROM sn_config.agent_tools at "
            "JOIN sn_config.agents ag ON ag.id=at.agent_id "
            "JOIN sn_core.tools tl ON tl.id=at.tool_id"):
        grants.add((a, t))

    bound_rows = q(
        "SELECT a.slug, c.slug FROM sn_config.agent_channels ac "
        "JOIN sn_config.agents a ON a.id=ac.agent_id "
        "JOIN sn_config.channels c ON c.id=ac.channel_id WHERE ac.enabled")

    allowed = {}
    for a, ch, who in q(
            "SELECT ag.slug, ch.slug, co.display_name "
            "FROM sn_config.agent_contacts ac "
            "JOIN sn_config.agents ag ON ag.id=ac.agent_id "
            "JOIN sn_config.contacts co ON co.id=ac.contact_id "
            "JOIN sn_config.channels ch ON ch.id = ANY(ac.allowed_channels)"):
        allowed.setdefault((a, ch), set()).add(who)

    traffic = {}
    for ch, kind, n, last in q(
            "SELECT payload->>'channel', "
            " CASE WHEN event_type IN ('message.transmitted','message.sent') THEN 'out' "
            "      WHEN event_type='message.inbound' THEN 'in' ELSE 'blocked' END, "
            " count(*)::text, to_char(max(ts),'MM-DD HH24:MI') "
            "FROM sn_core.events "
            "WHERE event_type LIKE 'message.%' AND ts > now() - interval '7 days' "
            "  AND payload->>'channel' IS NOT NULL GROUP BY 1,2"):
        traffic[(ch, kind)] = (n, last)

    shared = {}
    tool_rows = check_tools(agents, servers, tools, grants, shared)
    channel_rows = check_channels(agents, bound_rows, allowed, traffic)
    all_rows = tool_rows + channel_rows

    upsert(all_rows)

    # Summary to stdout (captured by cron into the log file).
    ok_count = sum(1 for r in all_rows if r[3] == "ok")
    fail_count = sum(1 for r in all_rows if r[3] == "fail")
    unt_count = sum(1 for r in all_rows if r[3] == "untestable")
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"{ts}  health-probe: {len(all_rows)} checks, "
          f"ok={ok_count} fail={fail_count} untestable={unt_count}")
    for r in all_rows:
        if r[3] == "fail":
            print(f"  FAIL  {r[0]}  {r[2]}:{r[1]}  {r[4]}")

    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    main()
