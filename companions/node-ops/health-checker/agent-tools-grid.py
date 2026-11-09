#!/usr/bin/env python3
"""Section 3 as a grid: every agent against every tool.

Rows are agents, columns are tools. Agents grow faster than the catalogue and
rows scroll where columns do not, so this way round survives a larger roster.

Two passes per cell, because they answer different questions:
  reach  is the thing there at all, asked cheaply and from outside
  work   does it actually do something, asked as the agent wherever possible

A cell shows the worse of the two. Everything that is not plain `ok` is
explained underneath, so the grid stays readable and nothing is hidden in it.
"""
import html
import json
import os
import subprocess
import sys
import urllib.request

DB = "smartnode"
TD = 'style="border:1px solid #888;padding:5px 7px;vertical-align:top;font-size:12.5px"'
TH = 'style="border:1px solid #888;padding:5px 7px;text-align:left;font-size:12px"'


def q(sql):
    out = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", DB, "-At", "-F", "\x1f", "-c", sql],
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(out.stderr.strip()[:300])
    return [l.split("\x1f") for l in out.stdout.splitlines() if l]


def esc(s):
    return html.escape(str(s or ""))


# ------------------------------------------------------------------ probes
def probe_http(url):
    """Reach, then work: a server that completes a handshake but lists no tool
    is reachable and useless, and that state has happened here before."""
    def call(payload):
        req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read(60000).decode("utf-8", "replace")
    try:
        st, _ = call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "audit", "version": "0"}}})
        reach = f"HTTP {st}"
    except Exception as e:
        return ("", f"unreachable: {type(e).__name__}")
    try:
        st, body = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        n = body.count('"name"')
        return (reach, f"lists {n} tools" if n else "answers, lists NO tools")
    except Exception as e:
        return (reach, f"handshake only, listing failed: {type(e).__name__}")


def probe_stdio(cmd, arg, as_user):
    """Spawn it the way the runtime would, as the agent, and ask for its tools."""
    if not (cmd and arg and os.path.exists(arg)):
        return ("", "missing on disk")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "audit", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    stdin = "\n".join(json.dumps(m) for m in msgs) + "\n"
    try:
        p = subprocess.run(["sudo", "-u", as_user, "env", f"SN_AGENT_SLUG={as_user[6:]}", cmd, arg],
                           input=stdin, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return ("present", "started and did not answer in time")
    out = p.stdout or ""
    if '"result"' not in out:
        return ("present", f"started and answered nothing usable (exit {p.returncode})")
    n = out.count('"inputSchema"') or out.count('"name"')
    return ("present", f"lists {n} tools as {as_user}" if n else "answers, lists NO tools")


def probe_fs(paths, as_user, slug):
    """What the agent can actually open, decided by the operating system.

    The declared list is NOT a gate. core-api says so in its own words where it
    renders it: "ownership is the boundary, this list is an instruction, and
    docs/node/entities.md forbids describing it to an owner as protection." So an
    empty list means the agent was TOLD about no directory, not that it can read
    nothing. The only honest check is to try, as the agent, and report what the
    kernel allowed.
    """
    home = f"/srv/agents/{slug}/work"
    ok_home = subprocess.run(["sudo", "-u", as_user, "test", "-r", home]).returncode == 0
    declared = [x.strip('" ') for x in paths.strip("{}").split(",") if x.strip('" ')]
    if not declared:
        return ("granted",
                (f"reads its own work area as {as_user}; no directory is named in its "
                 f"instructions, so it is told about nothing beyond that")
                if ok_home else f"CANNOT read its own work area as {as_user}")
    first = declared[0]
    ok = subprocess.run(["sudo", "-u", as_user, "test", "-r", first]).returncode == 0
    return ("granted", f"reads {first} as {as_user}" if ok
            else f"CANNOT read {first}, which its instructions name")


def probe_api(endpoint, http_path, method, has_url_in_contract):
    if not endpoint and not has_url_in_contract:
        return ("", "no address in the record or the contract")
    if not endpoint:
        return ("contract", "address only in the contract, not exercised")
    if (method or "").upper() not in ("GET", ""):
        return (f"{endpoint}{http_path}", "not exercised: calling it would change something")
    # A call that carries the agent identity cannot be exercised from here: as
    # root we are nobody to this node, and the refusal that comes back says
    # nothing about whether the agent could make it. Reaching it is all we can
    # honestly claim, and saying more would be inventing a result.
    try:
        with urllib.request.urlopen(endpoint + http_path, timeout=6) as r:
            return (f"HTTP {r.status}", "answers")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return (f"HTTP {e.code}", "not exercised: it answers, and it wants the agent identity")
        return (f"HTTP {e.code}", f"answers {e.code}, which is not a working call")
    except Exception as e:
        return ("", f"unreachable: {type(e).__name__}")


# ------------------------------------------------------------------ build
def main():
    agents = q("SELECT slug, coalesce(title,slug), coalesce(os_user,''), "
               "coalesce(fs_read::text,'{}') FROM sn_config.agents "
               "WHERE archived_at IS NULL AND enabled ORDER BY created_at")
    servers = {r[0]: (r[1], r[2]) for r in q(
        "SELECT slug, transport, config_jsonb::text FROM sn_config.mcp_servers WHERE enabled")}
    tools = q(r"""SELECT t.name, coalesce(t.title,t.name), t.kind,
                  coalesce(t.http_path,''), coalesce(t.http_method,''),
                  coalesce(i.endpoint,''),
                  (coalesce(t.usage_md,'') ~ 'https?://')::text
             FROM sn_core.tools t
             LEFT JOIN sn_config.integrations i ON i.slug=t.integration_slug
            WHERE t.kind <> 'mcp' OR t.name IN (SELECT slug FROM sn_config.mcp_servers)
            ORDER BY t.kind, t.name""")
    grants = {}
    for a, t, c in q("SELECT ag.slug, tl.name, array_to_string(at.granted_caps,'') "
                     "FROM sn_config.agent_tools at "
                     "JOIN sn_config.agents ag ON ag.id=at.agent_id "
                     "JOIN sn_core.tools tl ON tl.id=at.tool_id"):
        grants[(a, t)] = c

    # probe each tool once per distinct agent that holds it, cached where the
    # answer cannot differ between agents
    shared = {}
    notes = []

    def verdict(tool, agent):
        name, title, kind, hp, hm, ep, hasurl = tool
        slug, _, osu, fsr = agent
        if (slug, name) not in grants:
            return ""
        if kind == "builtin" and name == "fs":
            reach, work = probe_fs(fsr, osu or "root", slug)
        elif name in servers:
            transport, cfg = servers[name]
            conf = json.loads(cfg or "{}")
            if transport in ("http", "sse"):
                key = ("http", conf.get("url", ""))
                if key not in shared:
                    shared[key] = probe_http(conf.get("url", ""))
                reach, work = shared[key]
            else:
                args = conf.get("args") or []
                reach, work = probe_stdio(conf.get("command", ""), args[0] if args else "", osu or "root")
        elif kind == "api":
            key = ("api", name)
            if key not in shared:
                shared[key] = probe_api(ep, hp, hm, hasurl == "true")
            reach, work = shared[key]
        elif kind == "builtin":
            reach, work = ("built in", "served by the runtime")
        else:
            reach, work = ("shell", "not exercised here")

        bad = any(w in work.lower() for w in
                  ("no paths", "cannot", "unreachable", "nothing usable", "no tools",
                   "no address", "did not answer", "missing"))
        if bad:
            notes.append(f"<b>{esc(title)}</b> for <b>{esc(slug)}</b>: {esc(work)}")
            return '<span style="font-weight:600">DOES NOT WORK</span>'
        if "not exercised" in work:
            return "reach only"
        if name in servers and servers[name][0] == "stdio":
            return "ok, wake path"
        return "ok"

    head = "".join(f'<th {TH}>{esc(t[1])}</th>' for t in tools)
    body = []
    for a in agents:
        cells = "".join(f"<td {TD}>{verdict(t, a)}</td>" for t in tools)
        body.append(f'<tr><th {TH}>{esc(a[1])}</th>{cells}</tr>')

    print("<h2>3. Tools: every agent against every tool</h2>")
    print("<p>An empty cell means the agent was not given that tool. "
          "<i>ok</i> means it was reached and it did something. "
          "<i>ok, wake path</i> means the same, but it is absent while a person is "
          "talking to the agent. <i>reach only</i> means it is there and exercising it "
          "would have changed something, so it was left alone. "
          "<i>DOES NOT WORK</i> is explained underneath.</p>")
    print('<div style="overflow-x:auto;width:100%">'
          '<table style="border-collapse:collapse;margin:14px 0;min-width:100%">'
          f'<thead><tr><th {TH}>Agent</th>{head}</tr></thead>'
          f'<tbody>{"".join(body)}</tbody></table></div>')
    if notes:
        print("<h3>What is broken</h3><ul>" + "".join(f"<li>{n}</li>" for n in dict.fromkeys(notes)) + "</ul>")
    else:
        print("<p>Nothing broken.</p>")


if __name__ == "__main__":
    main()
