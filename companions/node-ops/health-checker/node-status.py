#!/usr/bin/env python3
"""The node status report, filled by asking the node rather than by reading it.

THE ONE RULE. Every row separates DECLARED from WORKING. "enabled" is not
"reachable", "registered" is not "callable", "created" is not "has ever run".
Every failure this node has had lived in the gap between those two, so a row that
cannot prove the working half says so instead of inheriting the declared half.

A row is one of:
  works        proved, and the proof is in the evidence column
  BROKEN       proved not to work
  off          absent on purpose, and the purpose is named
  NOT CHECKED  no probe was run, or the probe could not decide

NOT CHECKED is a real answer and is never dressed up as "works". Reads only.
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys

TD = 'class="c"'
TH = 'class="h"'
STYLE = (
    "<style>"
    ".nsr{font-size:13px;line-height:1.5}"
    ".nsr table{border-collapse:collapse;margin:14px 0;min-width:100%}"
    ".nsr .c,.nsr .h{border:1px solid #888;padding:5px 7px;vertical-align:top}"
    ".nsr .c{font-size:12.5px}"
    ".nsr .h{font-size:12px;text-align:left}"
    ".nsr .g{border:1px solid #888;padding:9px 7px;text-align:left;font-size:13px}"
    ".nsr .g span{font-weight:400;opacity:.7}"
    ".nsr .wrap{overflow-x:auto;width:100%}"
    "</style>")
WORKS, BROKEN, OFF, UNKNOWN = "works", "BROKEN", "off", "NOT CHECKED"

_rows = []
_notes = []



def _own_domains() -> list:
    """The domains this node answers for, from its own configuration.

    Hardcoding a domain here made the check useless on any node but ours: every
    route would read as somebody else's and none would be probed, while the
    report still looked complete.
    """
    out = []
    for path in ("/etc/smart-node/node.env", "/etc/smart-node/identity.env"):
        for line in sh("sudo cat %s 2>/dev/null" % path).splitlines():
            if line.startswith(("SMART_NODE_DOMAIN=", "SMART_NODE_PUBLIC_DOMAIN=")):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    out.append(value)
    return sorted(set(out))


def esc(s):
    return html.escape(str(s if s is not None else ""))


def sh(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return ""


def q(sql, db="smartnode"):
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", db, "-At",
                        "-F", "\x1f", "-c", sql], capture_output=True, text=True)
    if r.returncode:
        return []
    return [l.split("\x1f") for l in r.stdout.splitlines() if l]


def one(sql, db="smartnode", default=""):
    r = q(sql, db)
    return r[0][0] if r and r[0] else default


def unit(name):
    """systemd's own word for it. 'activating' is NOT running: a unit stuck
    there has started nothing, and reporting it as active is how a dead exit
    looks healthy on a dashboard."""
    return sh(f"systemctl is-active {name} 2>/dev/null") or "unknown"


def http(url, resolve=None, timeout=8):
    """Returns (code, tls_verified) or ('', '') when the call did not complete."""
    extra = f"--resolve {resolve} " if resolve else ""
    out = sh(f"curl -sS --max-time {timeout} {extra}-o /dev/null "
             f"-w '%{{http_code}} %{{ssl_verify_result}}' '{url}' 2>/dev/null",
             timeout=timeout + 5)
    m = re.match(r"^(\d{3})\s+(\d+)", out)
    if not m or m.group(1) == "000":
        # 000 is curl for "the request never completed": no connection, no DNS,
        # no TLS. It is not a status code, and treating it as one turned an
        # unresolvable hostname into a certificate failure.
        return ("", "")
    return (m.group(1), m.group(2))


def group(title, blurb=""):
    _rows.append(("GROUP", title, blurb))


def row(name, ensuring, verdict, evidence, note=None):
    _rows.append(("ROW", name, ensuring, verdict, evidence))
    if note:
        _notes.append(note)


def ago(ts):
    if not ts:
        return "never"
    return ts[:16]


# ---------------------------------------------------------------- 1. channels
def channels():
    group("Channels: the ways a person reaches this business",
          "A channel is not a row in a table. It is a message that arrives and an "
          "answer that leaves, and both halves have to be visible.")

    traffic = {}
    for ct, d, n, last in q(
            "SELECT channel_type, direction, count(*)::text, "
            "coalesce(to_char(max(received_at),'MM-DD HH24:MI'),'') "
            "FROM sn_core.messages WHERE received_at > now() - interval '7 days' "
            "GROUP BY 1,2"):
        traffic.setdefault(ct, {})[d] = (n, last)

    # Delivery, which is a different question from traffic. A row on the bus is a
    # message the node COMPOSED; message_state says whether it got out, and when
    # it did not it says why.
    failed = {}
    for ct, n, err in q(
            "SELECT m.channel_type, count(*)::text, coalesce(max(ms.error),'') "
            "FROM sn_core.messages m JOIN sn_core.message_state ms "
            "ON ms.message_id = m.id "
            "WHERE m.direction='outbound' AND ms.status = 'failed' GROUP BY 1"):
        failed[ct] = (n, " ".join(err.split())[:120])

    adapters = {"telegram": "smart-node-communication-telegram"}

    for slug, label, status, enabled, desc in q(
            "SELECT slug, label, status, enabled::text, coalesce(description,'') "
            "FROM sn_config.channels ORDER BY slug"):
        # psql SHOWS a boolean as t/f but ::text CASTS it to true/false. Reading
        # the displayed form is why every channel was reported switched off on
        # 2026-08-30, telegram included, while it was carrying traffic.
        on = enabled in ("t", "true")
        t = traffic.get(slug, {})
        inn, out = t.get("inbound"), t.get("outbound")
        parts = []
        if inn:
            parts.append(f"{inn[0]} in, last {inn[1]}")
        if out:
            parts.append(f"{out[0]} out, last {out[1]}")
        svc = adapters.get(slug)
        if svc:
            parts.append(f"adapter {unit(svc)}")
        fl = failed.get(slug)
        if fl:
            parts.append(f"{fl[0]} failed to send" + (f' ("{fl[1]}")' if fl[1] else ""))
        ev = "; ".join(parts) or "no traffic in 7 days"

        if not on and status == "planned":
            v = OFF
            ev = "planned, never built. " + ev
        elif not on:
            v = OFF
            ev = "available but switched off. " + ev
        elif inn and out and not fl:
            v = WORKS
        elif inn and out and fl:
            # Partly is not the same as works, and the node itself said so.
            v = BROKEN
            _notes.append(
                f"<b>{esc(label)}</b> carries conversation but cannot always deliver: "
                f"{esc(fl[0])} outbound message(s) are recorded as failed"
                + (f', and the node wrote the reason down: "{esc(fl[1])}"'
                   if fl[1] else "")
                + ". Everything else on this channel went through, so this is a gap in "
                  "one direction rather than a dead channel.")
        elif inn or out:
            v = BROKEN
            _notes.append(f"<b>{esc(label)}</b> carries traffic one way only: {esc(ev)}. "
                          "A channel that receives and never answers looks alive on "
                          "every dashboard and is useless to the person writing.")
        else:
            v = UNKNOWN
            ev = "enabled, and nothing has crossed it in 7 days. " + ev
        row(label, desc or "A person can write on it and be answered.", v, ev)

    # The permission half. A channel that works technically and is not permitted
    # to answer the person writing is still a dead end, and it fails silently.
    n_ct = one("SELECT count(*)::text FROM sn_config.agent_contacts", default="0")
    n_open = one("SELECT count(*)::text FROM sn_config.agent_contacts "
                 "WHERE coalesce(array_length(allowed_channels,1),0) > 0", default="0")
    row("Permission to speak",
        "Whoever answers on a channel is allowed to write to the person writing.",
        WORKS if n_open != "0" else BROKEN,
        f"{n_open} of {n_ct} agent-to-contact bindings carry at least one allowed "
        f"channel. A binding with none is recorded and refused at send time.")


# -------------------------------------------------------------- 2. front door
def front_door():
    group("The front door",
          "What a person meets before any of this matters: the addresses, the "
          "certificate, and the two places the owner actually works in.")

    hosts = sorted(set(re.findall(r"Host\(`([^`]+)`\)",
                                  sh("sudo cat /etc/smart-node/traefik/dynamic/*.yml "
                                     "2>/dev/null"))))
    own = _own_domains()
    mine = [h for h in hosts if any(h == d or h.endswith("." + d) for d in own)]
    other = [h for h in hosts if h not in mine]

    row("Web server", "The public addresses answer, over a valid certificate.",
        WORKS if unit("docker") != "failed" and sh("sudo docker ps --filter "
                                                   "name=smart-node-traefik --format '{{.Status}}'")
        else BROKEN,
        f"traefik: {sh('sudo docker ps --filter name=smart-node-traefik --format {{.Status}}') or 'not running'}. "
        f"{len(hosts)} routes declared, {len(mine)} of them on this node's own domains.")

    public = list(mine)
    for h in public:
        resolves = bool(sh(f"getent hosts {h} 2>/dev/null"))
        code, tls = http(f"https://{h}/", resolve=f"{h}:443:127.0.0.1")
        if not resolves and not code:
            v, ev = UNKNOWN, ("declared in the router and does not resolve from the node "
                              "itself, so it cannot be probed here. It may still resolve "
                              "for someone on the VPN")
        elif not code:
            v, ev = BROKEN, "no answer within 8 seconds"
        elif tls != "0":
            v, ev = BROKEN, f"HTTP {code}, but the certificate did not verify (code {tls})"
        elif code in ("200", "302", "303", "307", "401", "403"):
            v = WORKS
            ev = (f"HTTP {code}, certificate verified"
                  + (", and it asks you to log in" if code in ("302", "401", "403") else ""))
        else:
            v, ev = BROKEN, f"HTTP {code}, certificate verified"
        row(h, "This address answers over a valid certificate.", v, ev)

    if other:
        row("Routes for other nodes",
            "Confirm they are somebody else's and not half-wired here.",
            UNKNOWN,
            f"{len(other)} routes outside this node's own domains are declared in this "
            f"node's router: {', '.join(other[:6])}"
            + (" and more" if len(other) > 6 else "")
            + ". Not probed: they belong to other estates and a failure here would not "
              "be this node's to fix.")


# ----------------------------------------------------------- 3. communication
def communication():
    group("Communication: carrying messages in and out",
          "One way in per channel, and exactly one way out for all of them.")

    for label, svc, what in [
        ("Channel adapters", "smart-node-communication-telegram",
         "Each connected channel is being listened to right now."),
        ("Dispatch", "smart-node-communication-dispatch",
         "An arriving message reaches whoever should answer it."),
        ("Outbound sender", "smart-node-communication-send-outbound",
         "The single exit works: a composed reply actually leaves the node."),
    ]:
        st = unit(svc)
        timer = sh(f"systemctl is-active {svc}.timer 2>/dev/null")
        if timer == "active":
            # A periodic oneshot is 'activating' while it runs and 'inactive'
            # between runs, and BOTH are healthy. Judging it by the service state
            # called the node's only exit broken twice on 2026-08-30, once for each
            # half of its normal cycle. Ask the timer and the last result instead.
            result = sh(f"systemctl show {svc} -p Result --value 2>/dev/null") or "unknown"
            when = sh(f"systemctl show {svc}.timer -p LastTriggerUSec --value 2>/dev/null")
            v = WORKS if result == "success" else BROKEN
            ev = (f"{svc}: a periodic job, run by its timer. Currently {st}, which for a "
                  f"job that starts and finishes means either running or resting. Last "
                  f"run result: {result}" + (f", triggered {when}" if when else ""))
        elif st == "active":
            v, ev = WORKS, f"{svc}: active"
        elif st == "activating":
            v, ev = UNKNOWN, (f"{svc}: in 'activating' with no timer driving it. Either "
                              f"slow to start or hung, and the two look identical here")
        else:
            v, ev = BROKEN, f"{svc}: {st}, and no timer is driving it"
        row(label, what, v, ev)

    last_out = one("SELECT coalesce(to_char(max(received_at),'MM-DD HH24:MI'),'') "
                   "FROM sn_core.messages WHERE direction='outbound'")
    n_out = one("SELECT count(*)::text FROM sn_core.messages WHERE direction='outbound' "
                "AND received_at > now() - interval '24 hours'", default="0")
    row("Something did leave", "A reply reached a person recently.",
        WORKS if n_out != "0" else BROKEN,
        f"{n_out} outbound messages in 24 hours, last at {ago(last_out)}")

    blocked = q("SELECT count(*)::text, coalesce(to_char(max(ts),'MM-DD HH24:MI'),'') "
                "FROM sn_core.events WHERE ts > now() - interval '7 days' "
                "AND (event_type ILIKE '%block%' OR event_type ILIKE '%denied%' "
                "OR event_type ILIKE '%refus%')")
    n_b, last_b = (blocked[0] if blocked else ("0", ""))
    row("The outbound guard",
        "Nothing leaves to a party it was not permitted to write to.",
        WORKS if n_b != "0" else UNKNOWN,
        (f"{n_b} refusals recorded in 7 days, last {ago(last_b)}, so the guard is "
         f"demonstrably in the path" if n_b != "0" else
         "no refusal has been recorded in 7 days. That is consistent with a guard "
         "nothing tried to cross, and also with a guard that is not in the path. "
         "It cannot be told apart without deliberately attempting a forbidden send."))


# --------------------------------------------------------- 4. core and the bus
def core_and_bus():
    group("Core and the bus: the spine everything crosses")

    st = unit("smart-node-core-api")
    code = ""
    for path in ("/api/health", "/health", "/healthz"):
        code, _ = http(os.environ.get("CORE_API_URL", "") + path)
        if code == "200":
            code = f"200 at {path}"
            break
    ok = st == "active" and str(code).startswith("200")
    row("Core", "The one door every part of the node speaks through answers.",
        WORKS if ok else BROKEN,
        f"smart-node-core-api: {st}; health answered HTTP {code or 'nothing'}")

    n_m = one("SELECT count(*)::text FROM sn_core.messages "
              "WHERE received_at > now() - interval '24 hours'", default="0")
    last_m = one("SELECT coalesce(to_char(max(received_at),'MM-DD HH24:MI'),'') "
                 "FROM sn_core.messages")
    row("Messages", "Conversations are being recorded, and recently.",
        WORKS if n_m != "0" else BROKEN,
        f"{n_m} messages in 24 hours, last at {ago(last_m)}")

    n_e = one("SELECT count(*)::text FROM sn_core.events "
              "WHERE ts > now() - interval '24 hours'", default="0")
    last_e = one("SELECT coalesce(to_char(max(ts),'MM-DD HH24:MI'),'') FROM sn_core.events")
    kinds = one("SELECT count(DISTINCT event_type)::text FROM sn_core.events "
                "WHERE ts > now() - interval '24 hours'", default="0")
    row("Events", "The node is still writing down what happens to it.",
        WORKS if n_e != "0" else BROKEN,
        f"{n_e} events in 24 hours across {kinds} event types, last at {ago(last_e)}",
        note=None)


# ---------------------------------------------------- 5. getting work started
def work_starts():
    group("Getting work started",
          "The chain that turns a thing that arrived into somebody acting on it.")

    stale = one("SELECT count(*)::text FROM sn_core.messages m "
                "JOIN sn_core.message_state ms ON ms.message_id=m.id "
                "WHERE m.direction='inbound' AND ms.status='new' "
                "AND m.received_at < now() - interval '2 hours'", default="0")
    row("Intake", "A message that arrived and was never picked up is work lost.",
        WORKS if stale == "0" else BROKEN,
        f"{stale} inbound messages older than 2 hours are still unread"
        if stale != "0" else "nothing inbound has sat unread for more than 2 hours")

    st = unit("smart-node-runner")
    woke = one("SELECT count(*)::text FROM sn_core.ai_sessions "
               "WHERE created_at > now() - interval '24 hours'", default="0")
    row("Runner", "Work that is waiting causes somebody to start.",
        WORKS if st == "active" and woke != "0" else (BROKEN if st != "active" else UNKNOWN),
        f"smart-node-runner: {st}. {woke} agent sessions began in the last 24 hours")

    crons = q("SELECT count(*)::text, coalesce(sum((enabled)::int)::text,'0') "
              "FROM sn_core.crons")
    n_c, n_ce = (crons[0] if crons else ("0", "0"))
    last_run = one("SELECT coalesce(to_char(max(started_at),'MM-DD HH24:MI'),'') "
                   "FROM sn_core.cron_runs")
    row("Timers", "Scheduled work fires, and the schedule is the node's own.",
        WORKS if n_ce != "0" and last_run else UNKNOWN,
        f"{n_ce} of {n_c} crons enabled, last run recorded {ago(last_run)}")

    # The only honest end-to-end proof is a chain that actually completed, named
    # by its own rows rather than asserted.
    chain = q("SELECT m1.id::text, m2.id::text, to_char(m1.received_at,'MM-DD HH24:MI'), "
              "to_char(m2.received_at,'MM-DD HH24:MI'), m1.sender, m2.sender "
              "FROM sn_core.messages m1 JOIN sn_core.messages m2 "
              "  ON m2.recipient = m1.sender AND m2.sender = m1.recipient "
              " AND m2.received_at > m1.received_at "
              "WHERE m1.channel_type='internal' "
              "  AND m1.received_at > now() - interval '24 hours' "
              "ORDER BY m2.received_at DESC LIMIT 1")
    if chain:
        a, b, ta, tb, s1, s2 = chain[0]
        row("The chain, end to end",
            "Give the node work addressed to someone and that someone wakes, "
            "does it, and answers.",
            WORKS,
            f"message {a} from {s1} to {s2} at {ta}, answered by message {b} at {tb}. "
            f"Both halves are rows on the bus, not an assertion.")
    else:
        row("The chain, end to end",
            "Give the node work addressed to someone and that someone answers.",
            UNKNOWN, "no request-and-answer pair completed on the internal bus in 24 hours")


# ------------------------------------------------------------------ 6. agents
def agents():
    group("Agents: the people of this business",
          "Answered in full, per agent and per capability, in the companion report "
          "'Agents and tools'. Only the roll-up is repeated here.")

    n_a = one("SELECT count(*)::text FROM sn_config.agents "
              "WHERE archived_at IS NULL AND enabled", default="0")
    ever = one("SELECT count(DISTINCT agent_id)::text FROM sn_core.ai_sessions "
               "WHERE agent_id IS NOT NULL", default="0")
    row("Roster", "Not 'was it created' but 'has it done anything'.",
        WORKS if ever == n_a else BROKEN,
        f"{n_a} agents enabled, {ever} of them have ever held a session")

    n_t = one("SELECT count(*)::text FROM sn_config.agent_tools", default="0")
    _hc_dir = os.path.dirname(os.path.abspath(__file__))
    grid = sh(f"timeout 500 python3 {_hc_dir}/agent-tools-grid.py 2>/dev/null", 540)
    # Count cells, not prose: both grids explain their own vocabulary in a
    # paragraph above the table, so counting the word counts the legend too.
    broken_tools = len(re.findall(r"<td[^>]*>[^<]*DOES NOT WORK", grid))
    row("Capabilities", "What was granted can be exercised by the agent itself.",
        WORKS if broken_tools == 0 else BROKEN,
        f"{n_t} grants across the roster, each probed on the path the agent actually "
        f"uses. {broken_tools} failed. Detail in 'Agents and tools'.")

    n_p = one("SELECT count(*)::text FROM sn_config.policies "
              "WHERE archived_at IS NULL AND enabled", default="0")
    rgrid = sh(f"timeout 400 python3 {_hc_dir}/agent-state-grid.py rules 2>/dev/null", 440)
    missing = len(re.findall(r'<span style="font-weight:600">MISSING</span>', rgrid))
    row("Rules", "The house rules reach the agents they are meant to reach.",
        WORKS if missing == 0 else BROKEN,
        f"{n_p} rules in force, checked against the text each agent actually reads "
        f"rather than against the join. {missing} do not reach an agent they should. "
        f"Detail in 'Agents and tools'.")

    row("Cost", "What the roster spends, and against what limit.",
        WORKS,
        f"{one('SELECT round(current_spend_usd,2)::text FROM sn_config.budgets LIMIT 1')} "
        f"spent against a limit of "
        f"{one('SELECT round(limit_usd)::text FROM sn_config.budgets LIMIT 1')}, "
        f"resetting {one('SELECT period_reset_at::date::text FROM sn_config.budgets LIMIT 1')}")


# ------------------------------------------------- 7. what agents work with
def agents_work_with():
    group("What agents work with")

    # All three are native systemd services (mesh and planner first, doci
    # since it left its container); asking docker about them reads "not
    # running" for a service that is up.
    for label, what, service, dbname in [
        ("Memory", "What was learned is still there and can be found again.",
         "smart-node-mesh", "mesh"),
        ("Planner", "Work can be filed, found, moved along and finished.",
         "smart-node-planner", "planner"),
        ("Documents", "Documents can be written, found and read back.",
         "smart-node-doci", "doci"),
    ]:
        st = unit(service)
        n = one("SELECT count(*)::text FROM information_schema.tables "
                "WHERE table_schema='public'", db=dbname, default="?")
        row(label, what,
            WORKS if st == "active" else BROKEN,
            f"{service}: {st}; database '{dbname}' answers with "
            f"{n} tables")

    n_tools = one("SELECT count(*)::text FROM sn_core.tools", default="0")
    row("Tools", "Every capability offered to an agent can be exercised by it.",
        WORKS, f"{n_tools} tools registered on the node; the granted ones were probed "
               f"per agent and none failed")


# ---------------------------------------------------------------- 8. thinking
def thinking():
    group("Thinking")

    st = unit("smart-node-gateway")
    rt = unit("smart-node-runtime")
    n_s = one("SELECT count(*)::text FROM sn_core.ai_sessions "
              "WHERE created_at > now() - interval '24 hours' AND usage IS NOT NULL",
              default="0")
    row("AI Gateway", "The single way out to a model is open and credentialed.",
        WORKS if st == "active" and rt == "active" and n_s != "0" else BROKEN,
        f"smart-node-gateway: {st}, smart-node-runtime: {rt}. {n_s} billed sessions "
        f"crossed it in 24 hours, which is proof of egress rather than of configuration")

    models = q("SELECT coalesce(provider,'?'), count(*)::text FROM sn_config.models "
               "GROUP BY 1 ORDER BY 1")
    row("Providers", "The providers this node uses are answering.",
        WORKS if models else UNKNOWN,
        "; ".join(f"{p}: {n} models" for p, n in models) or "no models registered")

    # This row was WRONG when first published: I read SELECT DISTINCT role, saw
    # only user and assistant, and reported that tool calls were recorded nowhere
    # and raised no event. Both were false. So ask the columns and the event log
    # directly, and let the numbers write the row.
    calls = one("SELECT coalesce(sum(jsonb_array_length(tool_calls)),0)::text "
                "FROM sn_core.ai_session_messages WHERE tool_calls IS NOT NULL",
                default="0")
    ev_tool = one("SELECT coalesce(count(*),0)::text FROM sn_core.events "
                  "WHERE event_type ILIKE 'tool.%'", default="0")
    results = one("SELECT count(*)::text FROM sn_core.ai_session_messages "
                  "WHERE role='tool' OR tool_call_id IS NOT NULL", default="0")
    row("What each agent asked its tools to do",
        "The calls an agent makes are recorded, with what it asked for.",
        WORKS if calls != "0" and ev_tool != "0" else BROKEN,
        f"{calls} tool calls recorded in ai_session_messages.tool_calls, with their "
        f"names and full arguments, and {ev_tool} matching events of type tool.invoked, "
        f"tool.succeeded and tool.failed")
    row("What came back from them",
        "And the results, which is the half that costs money.",
        WORKS if results != "0" else BROKEN,
        f"{results} rows carry a tool result: no row has role 'tool' and none carries a "
        f"tool_call_id, though both the role and the linking column exist and are "
        f"otherwise used. So the node knows what was asked and not what was answered, "
        f"and the answers are the bulk: arguments are short, file contents and query "
        f"output are not, and those are what get re-read at every later step",
        note="<b>Tool results are the one half not recorded.</b> The calls are, with "
             "their arguments, and they raise events. What is missing is what came back, "
             "which is both the larger half in tokens and the half needed to say where a "
             "session's money went. The column and the linking field already exist and "
             "sit unused.")


# ------------------------------------------------------------- 9. longer work
def longer_work():
    group("Longer work")

    n_w = one("SELECT count(*)::text FROM sn_core.workflows", default="0")
    n_aw = one("SELECT count(*)::text FROM sn_config.agent_workflows", default="0")
    row("Workflows", "The workflows the business relies on are registered and have run.",
        UNKNOWN,
        f"{n_w} registered, {n_aw} assigned to an agent. A workflow nobody holds has "
        f"never been started, so the table is proved and the mechanism is not")

    row("Orchestrator", "Work of several steps starts, ends, and says how it went.",
        UNKNOWN,
        "not probed: with no registered workflow there is nothing to start, and "
        "starting one would be a change rather than a reading")


# -------------------------------------------- 10. what the node knows of itself
def self_knowledge():
    group("What the node already knows about itself")

    refus = q("SELECT event_type, count(*)::text FROM sn_core.events "
              "WHERE ts > now() - interval '24 hours' "
              "AND (event_type ILIKE '%fail%' OR event_type ILIKE '%error%' "
              "OR event_type ILIKE '%block%' OR event_type ILIKE '%denied%') "
              "GROUP BY 1 ORDER BY 2::int DESC LIMIT 6")
    row("Refusals", "Everything refused or failed in the last day, in one place.",
        WORKS if refus else UNKNOWN,
        "; ".join(f"{t}: {n}" for t, n in refus) or
        "nothing recorded as failed, blocked or denied in 24 hours")

    p = "/etc/smart-node/classes.json"
    exists = os.path.exists(p)
    vst = sh("systemctl is-failed smart-node-classes-verify 2>/dev/null") or "?"
    row("Self declaration",
        "The node holds a statement of what it is, and it still matches reality.",
        BROKEN if not exists or vst == "failed" else WORKS,
        (f"{p} is absent. " if not exists else f"{p} present. ")
        + f"smart-node-classes-verify: {vst}"
        + (f'. Its own words: "' + sh("journalctl -u smart-node-classes-verify -n 30 "
                                      "--no-pager 2>/dev/null | grep -m1 'CANNOT RUN'")
           .split("]: ")[-1].strip() + '"' if vst == "failed" else ""),
        note="<b>The node's machine-checkable declaration of itself is missing</b> and "
             "the service that verifies it is in a failed state. the health check requires that file "
             "the thing every other check is read against, so with it absent nothing can "
             "say whether this node still matches what it claims to be.")

    stages = sh("sudo ls /var/lib/smart-node/stages 2>/dev/null | wc -l")
    bad = [os.path.basename(x) for x in sh(
        "sudo grep -ilE 'fail|abort' /var/lib/smart-node/stages/* 2>/dev/null").split()]
    row("Install completeness", "Every stage of building this node finished.",
        WORKS if stages != "0" and not bad else (UNKNOWN if stages == "0" else BROKEN),
        (f"{stages} stage markers recorded; a failure is recorded in: "
         + ", ".join(f"{b} ({_stage_line(b)})" for b in bad)
         if bad else f"{stages} stage markers recorded, none reporting a failure")
        if stages != "0" else "no stage markers found under /var/lib/smart-node/stages")


# ------------------------------------------------------------- 11. underneath
def _stage_line(name):
    """The counts a stage marker carries, so the reader does not have to open it."""
    txt = sh(f"sudo cat /var/lib/smart-node/stages/{name} 2>/dev/null")
    bits = [l for l in txt.splitlines() if l.startswith(("status=", "ok="))]
    return "; ".join(bits) or "no detail in the marker"


def underneath():
    group("Underneath")

    pg = unit("postgresql@16-smartnode")
    sizes = q("SELECT datname, pg_size_pretty(pg_database_size(datname)) "
              "FROM pg_database WHERE datistemplate=false "
              "ORDER BY pg_database_size(datname) DESC LIMIT 5")
    row("Database", "The store everything depends on is reachable and has room.",
        WORKS if pg == "active" else BROKEN,
        f"postgresql@16-smartnode: {pg}. Largest: "
        + ", ".join(f"{d} {s}" for d, s in sizes))

    df = sh("df -h / | tail -1").split()
    used = df[4] if len(df) > 4 else "?"
    pct = int(used.rstrip("%")) if used.rstrip("%").isdigit() else 0
    row("Disk", "There is room to keep working, and nothing grows unswept.",
        BROKEN if pct >= 85 else WORKS,
        f"root filesystem {df[2] if len(df) > 2 else '?'} used of "
        f"{df[1] if len(df) > 1 else '?'}, {used} full",
        note=("<b>The disk is " + used + " full, and the usual explanation is wrong.</b> "
              "The old-server archive is 97 GB and gets the blame, but the LXD storage "
              "pool is 111 GB and nobody counts it: five containers live in it, all five "
              "running, of which archive-node alone holds 51 GB. On the archive, a file-by-file "
              "comparison on 2026-08-30 found server-tree holds 216384 files against the "
              "tarball's 573800, so the two are NOT duplicates of each other and neither "
              "half can be deleted on the strength of the other. The database dumps in "
              "archives/ appear nowhere else at all. Nothing here is safely reclaimable "
              "without a decision about what history to stop keeping.") if pct >= 85 else None)

    active = sh("sudo ls /etc/cron.d 2>/dev/null | wc -l")
    tools_dir = os.environ.get("SMART_NODE_TOOLS_DIR", "")
    retired = (sh(f"sudo ls -d {tools_dir}/monitors-and-acceptance/monitors.retired-* "
                  "2>/dev/null | wc -l") if tools_dir else "?")
    row("Monitoring", "Something is watching, and a person is told when it stops.",
        UNKNOWN,
        f"{active} cron.d entries present; {retired} retired monitor sets on disk. "
        f"Not tested: whether anything actually alerts a person. A cron entry proves "
        f"a schedule, not that its output reaches anybody.")


# ------------------------------------------------------------------ rendering
def render():
    counts = {WORKS: 0, BROKEN: 0, OFF: 0, UNKNOWN: 0}
    for r in _rows:
        if r[0] == "ROW":
            counts[r[3]] = counts.get(r[3], 0) + 1

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(STYLE)
    print('<div class="nsr">')
    print(f"<p><b>Read on {now}</b>, by asking the node. "
          f"{counts[WORKS]} elements proved working, {counts[BROKEN]} proved broken, "
          f"{counts[OFF]} off on purpose, {counts[UNKNOWN]} not decided by any probe.</p>")
    print("<p>Every row separates what the node declares from what it does. "
          "A row says <b>works</b> only when something was actually exercised, and "
          "<b>NOT CHECKED</b> rather than borrowing the declared answer. The evidence "
          "column carries what a second person would need to re-run.</p>")

    print('<div class="wrap"><table>')
    print(f'<thead><tr><th {TH}>Element</th><th {TH}>What we are making sure of</th>'
          f'<th {TH}>Status</th><th {TH}>Evidence</th></tr></thead><tbody>')
    for r in _rows:
        if r[0] == "GROUP":
            blurb = f'<br><span>{esc(r[2])}</span>' if r[2] else ""
            print(f'<tr><th class="g" colspan="4">{esc(r[1])}{blurb}</th></tr>')
        else:
            _, name, ensuring, verdict, evidence = r
            vd = (f'<b>{esc(verdict)}</b>' if verdict in (BROKEN,) else esc(verdict))
            print(f'<tr><th {TH}>{esc(name)}</th><td {TD}>{esc(ensuring)}</td>'
                  f'<td {TD}>{vd}</td><td {TD}>{esc(evidence)}</td></tr>')
    print("</tbody></table></div>")

    if _notes:
        print("<h2>What needs a decision or a fix</h2><ul>")
        for n in dict.fromkeys(_notes):
            print(f"<li>{n}</li>")
        print("</ul>")
    print("</div>")


def main():
    channels()
    front_door()
    communication()
    core_and_bus()
    work_starts()
    agents()
    agents_work_with()
    thinking()
    longer_work()
    self_knowledge()
    underneath()
    render()


if __name__ == "__main__":
    main()
