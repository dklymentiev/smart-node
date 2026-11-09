#!/usr/bin/env python3
"""Every agent against every channel, and whom each may answer.

Rows are agents, columns are the channels the node has. A cell says whether the
agent is on that channel and whether anything has actually gone through it.

WHY THE SECOND HALF MATTERS MORE THAN THE FIRST. Being bound to a channel is not
being able to answer on it: this node spent two days with an agent bound to
Telegram and permitted to write to nobody, so every message in both directions
was recorded and refused while everything looked configured. So a cell is only
`ok` when the binding exists AND there is a party the agent may answer AND
something has actually been delivered.

Evidence comes from the event log rather than from a probe: sending a real
message to prove a channel works would write to a person.
"""
import html
import subprocess
import sys

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


def main():
    agents = q("SELECT slug, coalesce(title,slug) FROM sn_config.agents "
               "WHERE archived_at IS NULL AND enabled ORDER BY created_at")
    channels = q("SELECT slug, coalesce(status,''), enabled FROM sn_config.channels ORDER BY slug")

    bound = {(r[0], r[1]): r[2] for r in q(
        "SELECT a.slug, c.slug, ac.instance_ref FROM sn_config.agent_channels ac "
        "JOIN sn_config.agents a ON a.id=ac.agent_id "
        "JOIN sn_config.channels c ON c.id=ac.channel_id WHERE ac.enabled")}

    # whom each agent is permitted to write to, per channel
    # A contact grant names the channels it covers, as ids on the grant row.
    allowed = {}
    for a, ch, who in q(
        "SELECT ag.slug, ch.slug, co.display_name "
        "FROM sn_config.agent_contacts ac "
        "JOIN sn_config.agents ag ON ag.id=ac.agent_id "
        "JOIN sn_config.contacts co ON co.id=ac.contact_id "
        "JOIN sn_config.channels ch ON ch.id = ANY(ac.allowed_channels)"):
        allowed.setdefault((a, ch), set()).add(who)

    # what the log says actually happened, per channel
    # What actually happened, from the log. Sending a real message to prove a
    # channel works would write to a person, so the evidence is what the node
    # already recorded rather than a probe.
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

    head = "".join(f'<th {TH}>{esc(c[0])}<br>'
                   f'<span style="opacity:.6">{esc(c[1])}</span></th>' for c in channels)
    rows, notes = [], []

    for slug, title in agents:
        cells = []
        for ch, status, enabled in channels:
            ref = bound.get((slug, ch))
            if not ref:
                cells.append("")
                continue
            who = allowed.get((slug, ch), set())
            out = traffic.get((ch, "out"))
            blocked = traffic.get((ch, "blocked"))
            if not who:
                notes.append(f"<b>{esc(title)}</b> on <b>{esc(ch)}</b>: bound as "
                             f"{esc(ref)} and permitted to answer NOBODY, so every message "
                             f"is recorded and refused")
                cells.append('<span style="font-weight:600">BROKEN</span>')
            elif out:
                cells.append(f"ok<br><span style='opacity:.6'>last out {esc(out[1])}</span>")
            else:
                notes.append(f"<b>{esc(title)}</b> on <b>{esc(ch)}</b>: bound as {esc(ref)} "
                             f"and permitted, but nothing has gone out in seven days")
                cells.append("bound, silent")
        rows.append(f'<tr><th {TH}>{esc(title)}</th>' +
                    "".join(f"<td {TD}>{c}</td>" for c in cells) + "</tr>")

    print("<h2>Channels: who is reachable, and whom they may answer</h2>")
    print("<p>An empty cell means the agent is not on that channel. <i>ok</i> means bound, "
          "permitted to answer somebody, and something has actually gone out. "
          "<i>bound, silent</i> means the first two and no traffic. <i>BROKEN</i> means bound "
          "and permitted to answer nobody, which looks configured and delivers nothing.</p>")
    print('<div style="overflow-x:auto;width:100%">'
          '<table style="border-collapse:collapse;margin:14px 0;min-width:100%">'
          f'<thead><tr><th {TH}>Agent</th>{head}</tr></thead>'
          f'<tbody>{"".join(rows)}</tbody></table></div>')

    if traffic.get(("telegram", "blocked")):
        n, last = traffic[("telegram", "blocked")]
        print(f"<p><b>Refused in the last seven days:</b> {esc(n)} messages, most recently "
              f"{esc(last)}. A refusal is written down with its reason at the moment it "
              f"happens, so this number falling to zero is the proof a channel was fixed.</p>")

    if notes:
        print("<h3>What is wrong</h3><ul>" +
              "".join(f"<li>{n}</li>" for n in dict.fromkeys(notes)) + "</ul>")
    else:
        print("<p>Nothing wrong.</p>")


if __name__ == "__main__":
    main()
