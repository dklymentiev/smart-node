#!/usr/bin/env python3
"""Two grids: which rules actually reach each agent, and whether each agent lives.

RULES. A policy in the catalogue is not a rule an agent follows. It reaches the
agent only if the agent carries its category, or it is written for that agent, or
an override pulls it in, AND the composed instructions actually contain it. The
catalogue and the prompt disagree more often than anyone expects, so the check is
against the text the agent reads, not against the join.

LIFE. Not "was it created" but "has it done anything, in what state is it now,
and what was it refused". Those three answer most questions about an agent before
anything else is examined.

Reads only.
"""
import datetime as _dt
import html
import os
import subprocess
import sys

DB = "smartnode"
TD = 'style="border:1px solid #888;padding:5px 7px;vertical-align:top;font-size:12.5px"'
TH = 'style="border:1px solid #888;padding:5px 7px;text-align:left;font-size:12px"'


def q(sql, db=DB):
    out = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", db, "-At", "-F", "\x1f", "-c", sql],
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(out.stderr.strip()[:300])
    return [l.split("\x1f") for l in out.stdout.splitlines() if l]


def esc(s):
    return html.escape(str(s or ""))


def prompt_of(slug):
    p = f"/srv/agents/{slug}/CLAUDE.md"
    out = subprocess.run(["sudo", "cat", p], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def rules_grid(agents):
    pols = q("SELECT slug, category, coalesce(title,slug), coalesce(agent_id::text,''), "
             "coalesce(left(body_md,90),'') FROM sn_config.policies "
             "WHERE archived_at IS NULL AND enabled ORDER BY category, slug")
    cats = {a[0]: (a[4] or "{}").strip("{}").split(",") for a in agents}
    prompts = {a[0]: prompt_of(a[0]) for a in agents}
    ids = {a[0]: a[5] for a in agents}

    head = "".join(f'<th {TH}>{esc(a[1])}</th>' for a in agents)
    rows, notes = [], []
    for slug, cat, title, owner, sample in pols:
        cells = []
        for a in agents:
            should = cat in cats.get(a[0], []) or (owner and owner == ids.get(a[0]))
            # the first sentence of the rule is enough to find it in the prose
            probe = " ".join(sample.split())[:60]
            present = bool(probe) and probe in " ".join(prompts.get(a[0], "").split())
            if should and present:
                cells.append("ok")
            elif should and not present:
                cells.append('<span style="font-weight:600">MISSING</span>')
                notes.append(f"<b>{esc(title)}</b> should reach <b>{esc(a[1])}</b> "
                             f"and is not in its instructions")
            elif present and not should:
                cells.append("unexpected")
                notes.append(f"<b>{esc(title)}</b> is in <b>{esc(a[1])}</b> instructions "
                             f"and nothing says it should be")
            else:
                cells.append("")
        rows.append(f'<tr><th {TH}>{esc(title)}<br>'
                    f'<span style="opacity:.6;font-size:11px">{esc(cat)}</span></th>' +
                    "".join(f"<td {TD}>{c}</td>" for c in cells) + "</tr>")

    print("<h2>Rules: which reach whom, checked against what the agent reads</h2>")
    print(f"<p>{len(pols)} rules in the catalogue. A cell is <i>ok</i> when the rule should "
          "reach that agent and its text is present in the instructions the agent actually "
          "reads. An empty cell means it was never meant to reach it. <i>MISSING</i> means it "
          "should and does not, which is a rule nobody is following.</p>")
    print('<div style="overflow-x:auto;width:100%">'
          '<table style="border-collapse:collapse;margin:14px 0;min-width:100%">'
          f'<thead><tr><th {TH}>Rule</th>{head}</tr></thead>'
          f'<tbody>{"".join(rows)}</tbody></table></div>')
    _notes(notes)


def life_grid(agents):
    crons = {}
    for user, sched, cmd in q(
        "SELECT coalesce(crontab_user,''), coalesce(schedule,''), coalesce(command,'') "
        "FROM sn_core.crons WHERE enabled"):
        for part in (user.replace("agent-", ""), cmd):
            for a in part.split("/"):
                if a and a not in ("srv", "agents", "run.sh"):
                    crons.setdefault(a, sched)

    rt = {r[0]: r[1:] for r in q(
        "SELECT agent_slug, coalesce(status,''), coalesce(to_char(updated_at,'MM-DD HH24:MI'),''), "
        "coalesce(current_ref,'') FROM sn_core.agent_runtime")}
    sess = {r[0]: r[1] for r in q(
        "SELECT a.slug, count(s.id)::text FROM sn_config.agents a "
        "LEFT JOIN sn_core.ai_sessions s ON s.agent_id=a.id GROUP BY a.slug")}
    refused = {}
    for slug, n, last in q(
        "SELECT coalesce(entity_id, payload->>'agent'), count(*)::text, "
        "to_char(max(ts),'MM-DD HH24:MI') FROM sn_core.events "
        "WHERE ts > now() - interval '24 hours' AND (event_type LIKE '%blocked%' "
        "OR event_type LIKE '%failed%' OR event_type LIKE '%denied%') "
        "AND coalesce(entity_id, payload->>'agent') IS NOT NULL GROUP BY 1"):
        refused[slug] = (n, last)

    cols = ["State", "Sessions", "Wake path runs", "What starts it",
            "Instructions rebuilt", "Refused in 24h"]
    head = "".join(f'<th {TH}>{esc(c)}</th>' for c in cols)
    rows, notes = [], []
    for slug, title, cron, _osu, _cats, _id in agents:
        st, since, ref = rt.get(slug, ("never claimed", "", ""))
        logs = subprocess.run(["sudo", "bash", "-c",
                               f"ls /srv/agents/{slug}/logs/ 2>/dev/null | grep -c '^2026' || true"],
                              capture_output=True, text=True).stdout.strip() or "0"
        cm = subprocess.run(["sudo", "bash", "-c",
                             f"date -r /srv/agents/{slug}/CLAUDE.md '+%m-%d %H:%M' 2>/dev/null || true"],
                            capture_output=True, text=True).stdout.strip()
        timer = cron or crons.get(slug, "")
        starts = "work only" if not timer else f"a timer ({timer}) AND work"
        cron = timer
        if cron:
            notes.append(f"<b>{esc(title)}</b> is started by a timer every minute AND by work. "
                         f"Two launchers, and {esc(logs)} log files so far, one per idle wake")
        n_ref = refused.get(slug, ("0", ""))
        cells = [esc(f"{st} since {since}" if since else st), esc(sess.get(slug, "0")),
                 esc(logs), esc(starts), esc(cm),
                 esc(f"{n_ref[0]}" + (f", last {n_ref[1]}" if n_ref[1] else ""))]
        if st == "never claimed":
            notes.append(f"<b>{esc(title)}</b> has never been claimed by the runner")
        rows.append(f'<tr><th {TH}>{esc(title)}</th>' +
                    "".join(f"<td {TD}>{c}</td>" for c in cells) + "</tr>")

    print("<h2>Life: has each agent worked, and what was refused to it</h2>")
    print("<p><i>Sessions</i> counts live conversations, <i>wake path runs</i> counts the times "
          "it was started to work a queue. An agent can be busy on one and untouched on the "
          "other; both are real and they are not the same thing.</p>")
    print('<div style="overflow-x:auto;width:100%">'
          '<table style="border-collapse:collapse;margin:14px 0;min-width:100%">'
          f'<thead><tr><th {TH}>Agent</th>{head}</tr></thead>'
          f'<tbody>{"".join(rows)}</tbody></table></div>')
    _notes(notes)


def cost_grid(agents):
    """What each agent has actually cost, and how much of the budget is left.

    The node records real spend per session, not an estimate: the usage carries
    cost_usd with estimated set to false. So this is not a projection, it is the
    bill so far. Reading tokens dwarfs writing them on every agent here, which
    says the money goes on re-reading context rather than on producing answers.
    """
    rows = q("SELECT coalesce(a.title,'(not an agent)'), count(s.id)::text, "
             "coalesce(round(sum((s.usage->>'cost_usd')::numeric),2)::text,'0'), "
             "coalesce(sum((s.usage->>'output_tokens')::bigint)::text,'0'), "
             "coalesce(sum((s.usage->>'cache_read_tokens')::bigint)::text,'0'), "
             "coalesce(to_char(max(s.created_at),'MM-DD HH24:MI'),'') "
             "FROM sn_core.ai_sessions s LEFT JOIN sn_config.agents a ON a.id=s.agent_id "
             "WHERE s.usage IS NOT NULL GROUP BY 1 "
             "ORDER BY sum((s.usage->>'cost_usd')::numeric) DESC NULLS LAST")
    b = q("SELECT coalesce(period,''), coalesce(round(limit_usd)::text,''), "
          "coalesce(round(current_spend_usd,2)::text,''), "
          "coalesce(period_reset_at::date::text,'') "
          "FROM sn_config.budgets WHERE enabled LIMIT 1")

    ev = {}
    for slug, n in q("SELECT split_part(canonical_actor,':',2), count(*)::text "
                     "FROM sn_core.v_actor_events "
                     "WHERE ts > now() - interval '24 hours' "
                     "AND canonical_actor LIKE 'agent:%' "
                     "GROUP BY 1"):
        ev[slug] = n
    byslug = {a[1]: a[0] for a in agents}

    cols = ["Sessions", "Spent", "Answered (tokens)", "Re-read (tokens)",
            "Acted (24h)", "Last"]
    head = "".join(f'<th {TH}>{esc(c)}</th>' for c in cols)
    body = ""
    for r in rows:
        acted = ev.get(byslug.get(r[0], ""), "0")
        vals = list(r[1:5]) + [acted, r[5]]
        body += (f'<tr><th {TH}>{esc(r[0])}</th>' +
                 "".join(f"<td {TD}>{esc(v)}</td>" for v in vals) + "</tr>")
    print("<h2>Cost: what each agent has spent</h2>")
    print("<p>Recorded per session by the node itself and marked as measured rather than "
          "estimated, so this is the bill and not a projection.</p>")
    print('<div style="overflow-x:auto;width:100%">'
          '<table style="border-collapse:collapse;margin:14px 0;min-width:100%">'
          f'<thead><tr><th {TH}>Agent</th>{head}</tr></thead><tbody>{body}</tbody></table></div>')
    if b:
        period, limit, spent, end = b[0]
        # A percentage of the limit stops meaning anything once the limit is raised
        # far above the spend, so the useful figure is the daily rate: this node
        # started on 2026-08-28, and the rate is what a month would actually cost.
        days = max((_dt.date.today() - _dt.date(2026, 8, 28)).days, 1)
        try:
            rate = float(spent) / days
            proj = f", which is about {rate:.2f} a day and near {rate * 30:.0f} a month at this rate"
        except ValueError:
            proj = ""
        print(f"<p><b>Budget.</b> {esc(period)} limit {esc(limit)}, spent {esc(spent)} over "
              f"{days} days of running{esc(proj)}. The period resets {esc(end)}. Note that "
              "the limit can only be changed in the database: the panel shows it and nothing "
              "in the product writes it.</p>")


def _notes(notes):
    if notes:
        print("<h3>Worth looking at</h3><ul>" +
              "".join(f"<li>{n}</li>" for n in dict.fromkeys(notes)) + "</ul>")
    else:
        print("<p>Nothing worth looking at.</p>")


def main():
    agents = q("SELECT slug, coalesce(title,slug), coalesce(schedule_cron,''), "
               "coalesce(os_user,''), coalesce(policy_categories::text,'{}'), id::text "
               "FROM sn_config.agents WHERE archived_at IS NULL AND enabled ORDER BY created_at")
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("rules", "both"):
        rules_grid(agents)
    if which in ("life", "both"):
        life_grid(agents)
    if which in ("cost", "both"):
        cost_grid(agents)


if __name__ == "__main__":
    main()
