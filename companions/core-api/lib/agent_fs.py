"""Filesystem provisioning for agents.

The filesystem is a PROJECTION of the database (sn_config). An agent directory
is rendered from the static skeleton (_template) plus data from the agent's row.

Layout per agent slug (under SN_AGENTS_ROOT):
    <slug>/
        CLAUDE.md     -- generated: agent prompt (specialist body, or rendered template)
        agent.yaml    -- generated: config from the sn_config.agents row
        config.json   -- generated: runtime config (model, tools)
        README.md     -- generated from template
        CHANGELOG.md  -- append-only human log

There is no entry point in the folder. An agent is woken by the Runner through
brin and runs in a gateway session; the per-agent run.sh that cron
used to call has been retired and removed from the template.

Roots come from the environment (no fallbacks -- fail loud if unset):
    SN_AGENTS_ROOT     e.g. /srv/agents
    SN_AGENT_TEMPLATE  e.g. /srv/agents/_template

Archive on delete: <SN_AGENTS_ROOT>/.archive/<slug>-<YYYYMMDD-HHMMSS>/
"""

from datetime import datetime, timezone
import glob
import re
from pathlib import Path
import json
import os
import shutil

import yaml

AGENTS_ROOT = Path(os.environ["SN_AGENTS_ROOT"])
TEMPLATE_ROOT = Path(os.environ["SN_AGENT_TEMPLATE"])
ARCHIVE_ROOT = AGENTS_ROOT / ".archive"


def _agent_dir(slug: str) -> Path:
    return AGENTS_ROOT / slug


def _read_grants_tools(d: Path) -> list[str]:
    """Tool names granted to this agent, read from grants.json (Tools tab source)."""
    f = d / "grants.json"
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text())
        return sorted((data.get("grants") or {}).keys())
    except Exception:
        return []


def _render(text: str, name: str, role: str, tools: list[str]) -> str:
    return (
        text.replace("{{AGENT_NAME}}", name)
        .replace("{{AGENT_ROLE}}", role or "(no role set)")
        .replace("{{AGENT_TOOLS}}", "\n".join(f"- {t}" for t in tools) or "- (none granted yet)")
    )


def _write_config_files(
    d: Path, slug: str, title: str, model: str,
    role: str | None, description: str | None, tools: list[str],
    perms: dict | None = None,
) -> None:
    """Render agent.yaml + config.json from DB-row values (the data projection).

    `perms` carries the access/permission fields (os_user, schedule, os_groups,
    fs_read, fs_write, forbidden_fs, forbidden_behavior). They render into the
    production v2 agent.yaml shape; OS enforcement (ACL/sudoers) is applied on the
    node from these lists, forbidden_behavior is injected into the prompt.
    """
    p = perms or {}
    # Tools live in grants.json (the Tools tab). If the caller didn't pass an
    # explicit list, derive the agent.yaml tools[] from the granted tool names so
    # the projection reflects what the operator selected.
    if not tools:
        tools = _read_grants_tools(d)
    # An agent with no explicit fs_write is told about its own work area rather
    # than about nothing. The directory is created and chowned to the
    # agent by admin-runner's ensure_agent_users; this line only makes the agent
    # AWARE of it. The two are not the same thing: ownership is the boundary,
    # this list is an instruction, and docs/node/entities.md forbids describing
    # it to an owner as protection.
    fs_write = list(p.get("fs_write") or []) or [str(d / "work")]
    agent_yaml = {
        "name": slug,
        "title": title,
        "model": model,
        "role": role or "",
        "description": description or "",
        "enabled": True,
        "os_user": p.get("os_user") or "",
        "schedule": p.get("schedule") or "",
        "groups": list(p.get("os_groups") or []),
        "mesh_workspace": p.get("mesh_workspace") or "",
        "tools": tools,
        "forbidden_behavior": list(p.get("forbidden_behavior") or []),
        "forbidden_fs": list(p.get("forbidden_fs") or []),
        "directories": {
            "read": list(p.get("fs_read") or []),
            "write": fs_write,
        },
    }
    (d / "agent.yaml").write_text(
        yaml.safe_dump(agent_yaml, sort_keys=False, allow_unicode=True)
    )
    (d / "config.json").write_text(
        json.dumps(
            {
                "version": "2.0",
                "enabled": True,
                "runtime": {"model": model, "os_user": p.get("os_user") or None},
                "tools": tools,
            },
            indent=2,
        )
        + "\n"
    )


def rerender_config(
    slug: str, *, title: str, model: str,
    role: str | None = None, description: str | None = None, tools: list[str] | None = None,
    perms: dict | None = None,
) -> Path:
    """Re-render an existing agent's data files after the DB row changed."""
    d = _agent_dir(slug)
    if not d.is_dir():
        raise FileNotFoundError(f"agent directory missing: {d}")
    _write_config_files(d, slug, title, model, role, description, tools or [], perms)
    return d


def provision_agent_dir(
    slug: str,
    title: str,
    specialist_body: str,
    *,
    model: str = "claude-cli/sonnet",
    role: str | None = None,
    description: str | None = None,
    tools: list[str] | None = None,
    version: str = "0.1.0",
    perms: dict | None = None,
) -> Path:
    d = _agent_dir(slug)
    if d.exists():
        raise FileExistsError(f"agent directory already exists: {d}")
    if not TEMPLATE_ROOT.is_dir():
        raise FileNotFoundError(f"agent template missing: {TEMPLATE_ROOT}")

    tools = tools or []

    # Claim the new path atomically. A failed copy/render removes only our own
    # directory; a duplicate create must never remove another agent's files.
    d.mkdir()
    try:
        # 1. Copy the static skeleton (README, CLAUDE.md placeholder).
        shutil.copytree(TEMPLATE_ROOT, d, dirs_exist_ok=True)

        # 2. Render CLAUDE.md: the specialist body IS the agent prompt; otherwise
        #    substitute the template placeholders.
        if specialist_body:
            (d / "CLAUDE.md").write_text(specialist_body)
        else:
            tmpl = (d / "CLAUDE.md").read_text()
            (d / "CLAUDE.md").write_text(_render(tmpl, title, role, tools))

        # 3. Render README.md placeholders.
        readme = d / "README.md"
        if readme.is_file():
            readme.write_text(_render(readme.read_text(), title, role, tools))

        # 4 + 5. agent.yaml + config.json -- rendered from the DB row.
        _write_config_files(d, slug, title, model, role, description, tools, perms)

        # 6. CHANGELOG.md.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (d / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {version} — {ts}\n\n- Agent provisioned from template via core-api.\n"
        )

        return d
    except Exception:
        remove_agent_dir(slug)
        raise


def remove_agent_dir(slug: str) -> None:
    """Undo a failed create, without leaving an archived failed agent."""
    shutil.rmtree(_agent_dir(slug))


def write_mcp_json(slug: str, servers: list[dict]) -> Path:
    """Render /srv/agents/<slug>/.mcp.json from the agent's MCP servers.

    `servers` is a list of {slug, transport, config} (config = dict). Produces the
    claude-code .mcp.json shape: stdio -> {command,args,env}; http/sse -> {url,headers}.
    """
    d = _agent_dir(slug)
    if not d.is_dir():
        raise FileNotFoundError(f"agent directory missing: {d}")
    out: dict = {}
    for s in servers:
        cfg = s.get("config") or {}
        entry: dict = {"type": s["transport"]}
        if cfg.get("url"):
            entry["url"] = cfg["url"]
        if cfg.get("headers"):
            entry["headers"] = cfg["headers"]
        if cfg.get("command"):
            entry["command"] = cfg["command"]
            entry["args"] = cfg.get("args", [])
            if cfg.get("env"):
                entry["env"] = cfg["env"]
        out[s["slug"]] = entry
    target = d / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": out}, indent=2) + "\n")
    return target


def people_directory(cur) -> list[dict]:
    """Humans this node serves (principals) with their enabled channels, read
    from the registry (sn_config.users role<>'agent' + user_channels). Feeds the
    People block. Takes any dict-row cursor; runs no commit."""
    cur.execute(
        "SELECT id, name, role, handle FROM sn_config.users "
        "WHERE role <> 'agent' AND active AND archived_at IS NULL ORDER BY id"
    )
    rows = cur.fetchall() or []
    out = []
    for r in rows:
        cur.execute(
            "SELECT channel_slug FROM sn_config.user_channels "
            "WHERE user_id = %s AND enabled ORDER BY channel_slug",
            (r['id'],),
        )
        chans = [c['channel_slug'] for c in (cur.fetchall() or [])]
        out.append({'name': r['name'], 'role': r['role'],
                    'handle': r['handle'], 'channels': chans})
    return out



# The modules of this release (their manifests, laid by the installer) against
# the ones this node has (companions.d, one record per installed companion).
# An agent told only what the release can do offered email and workflows on a
# node that had neither (tester, 2026-10-30); modules are chosen at install and
# added later, so this is read at every compose, never written once.
MANIFESTS_DIR = os.environ.get("SN_MANIFESTS_DIR", "/etc/smart-node/manifests")
COMPANIONS_DIR = os.environ.get("SN_COMPANIONS_DIR", "/etc/smart-node/companions.d")


def node_facts() -> str:
    catalog = {}
    for f in sorted(glob.glob(os.path.join(MANIFESTS_DIR, "*.manifest.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        if m.get("name") and not m.get("core"):
            catalog[m["name"]] = m
    if not catalog:
        return ""
    have = {os.path.basename(f)[:-5] for f in glob.glob(os.path.join(COMPANIONS_DIR, "*.json"))}
    # a manifest's own words, without the internal names some carry in brackets
    word = lambda m: "%s (%s)" % (m.get("title") or m["name"], re.sub(r"\s*\([^)]*\)", "", m.get("provides") or "").strip())
    on = [word(m) for n, m in catalog.items() if n in have]
    off = [word(m) for n, m in catalog.items() if n not in have]
    lines = ["---\n\n# This node\n"]
    if on:
        lines.append("Installed here: " + "; ".join(on) + ".")
    if off:
        lines.append("Not installed here: " + "; ".join(off) + ". Each of these exists and "
                     "can be added: say what it gives, that it is not working yet, and that "
                     "installing its module switches it on. Never say it cannot be done.")
    lines.append("Put work you were asked for in your reply, in the chat or to the colleague who "
                 "asked: a file you write is seen by nobody else. Where a documents module or a "
                 "drive is installed, ask the person where to create it. Once you pass work to a "
                 "colleague you do not know its state until the answer comes: say you passed it on, "
                 "never what they are doing now. Asking a colleague for something, say what result you "
                 "expect and what to do with it: who gets it and how. Once a result has reached the person, "
                 "stop: the next step is theirs to decide. A message that asks nothing of you needs no "
                 "answer: do not answer it.")
    domain = os.environ.get("SMART_NODE_DOMAIN", "")
    if domain == "localhost" or domain.endswith(".localhost"):
        lines.append("This node is local: only this computer opens it. It can reach out "
                     "(web pages, APIs), but it cannot receive email, webhooks or phone "
                     "calls, and links to it do not open for other people.")
    return "\n\n".join(lines)


def compose_claude_md(
    role_body: str,
    tool_contracts: list[dict] | None = None,
    policies: list[dict] | None = None,
    knowledge: list[dict] | None = None,
    tools: list[dict] | None = None,
    mesh_workspace: str | None = None,
    people: list[dict] | None = None,
    team: list[str] | None = None,
    documents: list[dict] | None = None,
    workflows: list[dict] | None = None,
    contacts: list[dict] | None = None,
    channels: list[dict] | None = None,
    specialist_slug: str | None = None,
    agent_name: str | None = None,
) -> str:
    """Assemble an agent's CLAUDE.md from registry blocks (the prompt projection).

    The prompt is NOT hand-written: it is the deterministic sum of reusable blocks
    pulled from the registries -- role (specialists.body_md), per-tool usage
    contracts (sn_core.tools.usage_md, only for granted tools), and policies
    (policies.body_md, from department/agent assignments). Editing one registry row
    and re-composing propagates to every agent that references it.

    `tool_contracts`: [{"name": str, "usage_md": str|None}, ...] in display order.
    `policies`:       [{"title": str, "body_md": str|None}, ...].
    `tools`:          [{"name": str, "kind": str, "description": str}, ...] granted tools only.
    `channels`:       [{"channel": str, "instance_ref": str, "enabled": bool}, ...] the
                      agent's OWN bindings -- its addresses, not anyone else's.
    Blocks with no body are skipped (a granted tool without a contract is silent).
    """
    # The name comes first, and it comes from the agent row. A role is a JOB and
    # several agents may share one; a name is identity and belongs to the agent.
    # Writing it into the role text works by luck at best: alex's body happens to
    # open "You are Alex", foreman's opens "You are the configurator", and asked
    # its name on a call the latter answered "Gemini" -- it had none to give
    # The second sentence is what answers that question.
    parts: list[str] = []
    if (agent_name or "").strip():
        parts.append("Your name is %s. It is the only name you give."
                     % agent_name.strip())
    parts.append(role_body.rstrip())

    # THE RULES COME FIRST, directly under the role. They used to be assembled
    # near the end -- after people, team, workflows, channels, contacts and the
    # tool contracts -- which on a real agent put them past line 370 of a 469-line
    # prompt. Measured on reference-node: the front desk carried a policy forbidding it to
    # name tools or narrate mechanics, and in the next turn said "I ran into a
    # permission issue... the credentials aren't resolving from here". The rule was
    # present and ignored. A hard rule buried under ninety lines of tool usage text
    # is a rule the cheapest model on the node will not weigh, and the front desk is
    # exactly where the cheapest model runs.
    #
    # "They override any instruction above" is kept as the wording: what follows
    # them is reference material -- who exists, what tools say about themselves,
    # which documents are attached -- and none of it is licence to break a rule.
    pols = [p for p in (policies or []) if (p.get("body_md") or "").strip()]
    if pols:
        _pol_body = "\n\n".join(p["body_md"].strip() for p in pols)
        parts.append("---\n\n# Policies\n\n"
                     "Hard rules. They override anything else in this prompt.\n\n"
                     + _pol_body)

    # Attachment handles are resolved by an installed helper through core-api's
    # approved/access gate. This belongs in the canonical composer, not in one
    # agent's generated CLAUDE.md: regeneration must preserve the behaviour and
    # every current/future agent must receive the same honest limitation.
    parts.append(
        "---\n\n# Attachments in Studio chat\n\n"
        "A Studio message with a file contains an attachment instruction. For an "
        "image, PDF, or text-like file, run the exact "
        "`/srv/tools/attachments/attachment-read.py <id>` command from that "
        "instruction, then use the built-in Read tool on the absolute path it "
        "prints before answering about the file. The helper gives the file a type "
        "suffix and retrieves it through the approved/access gate. Do not search "
        "the filesystem, install a parser, or copy the durable store by hand. "
        "This node cannot inspect audio and video attachments yet. If one is "
        "attached, say plainly that you cannot read it and do not invent or infer "
        "anything about its contents. If the turn says a file was refused or could "
        "not be read, report that instead of guessing."
    )

    # Vocabulary, the language rule and the truthfulness rule used to be written
    # out here, as literals, and again in different words in the voice channel's
    # own composer. Two copies of one rule drift, and they had: the desk and the
    # phone were told different things about when a thing counts as done. They are
    # policies now -- rows in sn_config.policies, category `safety`, seeded by
    # seed-policies.py -- so the panel edits them, every channel reads the same
    # text, and there is one copy.

    _caps_path = "/etc/smart-node/capabilities.md" if specialist_slug == "configurator" else ""
    _caps = ""
    if _caps_path:
        try:
            with open(_caps_path, encoding="utf-8") as _cf:
                _caps = _cf.read().strip()
        except OSError:
            _caps = ""
    # What this node has comes first, and the release's list says it is the
    # release's: titled "what this node can do", the list won over "This node"
    # below it, and an agent offered documents, workflows and email on a node with
    # none of them (tester, round 2, 2026-10-30).
    _facts = node_facts()
    if _facts:
        parts.append(_facts)
    if _caps:
        parts.append(
            "---\n\n# What this release of Smart Node can do\n\n"
            "These are the capabilities of the release, not all of them of this node. "
            "Many need a module: offer only what \"This node\" above names as installed, "
            "or what needs no module; for the rest say the owner can add its module first. "
            "What is not listed here is not built in, and can be added with a connector (an "
            "MCP server) or an integration: say that, and that it takes setting up. Where no "
            "connector exists, offer to have one built: first a developer agent is created for it. An entry marked "
            "*Switch on: X* needs X enabled on this node -- say so plainly rather than "
            "promising it as ready.\n\n"
            + _caps
        )

    if (mesh_workspace or "").strip():
        parts.append(
            "---\n\n# Memory workspace\n\n"
            f"Your memory (mesh) workspace is `{mesh_workspace}`. By default, read and write "
            f"your skills, notes and facts there -- pass `workspace={mesh_workspace}` to your "
            "memory tool. Do not use `default` when your own workspace exists."
        )

    ppl = [p for p in (people or []) if p.get('name')]
    if ppl:
        plines = [
            'These are the people you serve directly -- your principals, not '
            'customers. When an incoming message channel address matches one of '
            'them, it IS that person: treat their requests as direct instructions.',
            '',
        ]
        for p in ppl:
            chans = ', '.join(p.get('channels') or [])
            line = '- **' + p['name'] + '**'
            if p.get('handle'):
                line += ' (`' + p['handle'] + '`)'
            if p.get('role'):
                line += ' -- ' + p['role']
            line += '. Channels: ' + (chans if chans else '(none registered)') + '.'
            plines.append(line)
        plines += [
            '',
            'To send a principal a message yourself -- they ask you to text or '
            'email them, or you owe them a result -- originate it on THEIR channel, '
            'addressed to that person by their handle (the registry resolves the '
            'handle to their real address; you never need their chat id or email):',
            '',
            '    /srv/tools/comm/comm.sh send --to <handle> --channel telegram --body "<message>"',
            '    /srv/tools/comm/comm.sh send --to <handle> --channel email --subject "<subject>" --body "<message>"',
            '',
            'For email ALWAYS put the subject in --subject -- never write a '
            '"Subject:" line inside --body (it would show up in the message body). '
            'Write ONLY the message content in --body -- do NOT add a closing '
            'sign-off or signature (no "Best regards", no your name, no company '
            'name): the system appends a branded signature automatically, so '
            'signing yourself doubles it. '
            'Address the actual person you are writing to; the owner is copied '
            '(BCC) on every outbound email automatically, so do NOT add the owner '
            'as a recipient unless the owner is literally who you are writing to. '
            'Never use PushNotification or any developer or terminal notification '
            'to reach a person -- those do not reach them.',
            '',
            'Answering an internal request from another agent: if a message reached '
            'you over INTERNAL mail from a colleague (they are asking on someone '
            'behalf, or waiting on a result from you), ANSWER THAT MESSAGE: '
            'inbox_reply(<its message id>, "<answer>"), or comm.sh reply --to '
            '<message id> --body "<answer>". Never answer with a new send: a reply '
            'carries the thread and the origin, so your answer reaches the person '
            'who is waiting; a new message loses both and is read by nobody. Do '
            'NOT email or message the end-user yourself, and never write into a '
            'chat (desk:...) that is not your own -- it is refused: the agent who '
            'asked you is the one talking to the person and relays your answer. '
            'Email a person directly only when you are the one handling their own '
            'external email thread.',
            '',
            'Simple things a principal asks you to do for them -- send a message, '
            'look up a task or its status, check something -- do yourself with your '
            'tools, and say plainly what you did or are doing: e.g. "Sure, sending it '
            'now -- check in a moment." NEVER say you passed it to the team or a '
            'colleague for something you can do yourself.',
            '',
            'When a request genuinely needs a specialist, hand it over AND name the '
            'person to the caller -- their real name from your team list, together '
            'with what they handle. Never a vague "I passed it to the team."',
        ]
        parts.append('---\n\n# People\n\n' + '\n'.join(plines))

    if team:
        # Business-AGNOSTIC on purpose: the domains belong to the colleagues listed
        # below, which come from this node's own DB (sn_config.agents + specialists).
        # Never name a trade or a job title here -- a node may be a studio, a clinic
        # or a shop, and a hardcoded example overrides the real roster.
        team_intro = (
            'Your colleagues -- this is your TEAM. Each one below is listed with what '
            'they handle. When a request belongs to a colleague domain, delegate it '
            'to the right person here with send-message over internal mail, saying '
            'what the person asked, so the colleague can answer the person. Asked in '
            'a Studio chat, their answer appears in that chat by itself, from them: '
            'do not repeat it. Asked anywhere else, relay their answer to the caller. '
            'Name the person to the caller when you hand it over. If you are unsure '
            'whose domain it is, ask one brief clarifying question. Do NOT delegate '
            'things you can answer yourself from your own documents.')
        parts.append('---\n\n# Team\n\n' + team_intro + '\n\n' + '\n'.join(team))

    # Workflows the agent is granted (sn_config.agent_workflows -> sn_core.workflows).
    # WHY: the grant existed but was never surfaced, so a delegated agent did not know
    # it had a flow to run and improvised a one-off reply instead -- the gap that left
    # the Workflows view dead in the demo. We list the flows and tell the agent to
    # START the matching one (a workflow_id-linked task), which the Runner then drives
    # via Rein with its gates. The selection is the agent's own (its grant menu), not a
    # caller decision or a hardcoded intent map.
    flows = [w for w in (workflows or []) if w.get("name")]
    if flows:
        flines = [
            'These are repeatable procedures you are trained to run. A flow is a fixed '
            'sequence of steps (draft, check, file, wait for approval) that the node '
            'executes for you step by step, pulling in the right colleagues and an '
            'owner approval gate where one is needed. Running a flow is how you do real '
            'multi-step work; prefer it over improvising a one-off answer when a '
            'request matches one.',
            '',
            'When a request you are handling matches one of these, START that flow '
            'instead of replying free-form -- create a task linked to the flow and the '
            'node drives it from there:',
            '',
            '    /srv/tools/planner/task.sh create "<short title of the request>" --workflow-id <flow-name>',
            '',
            'Pick the flow whose purpose fits the request. If none fits, handle it '
            'yourself as usual. Your flows:',
            '',
        ]
        for w in flows:
            line = '- **' + w['name'] + '**'
            if w.get('title'):
                line += ' (' + w['title'] + ')'
            if (w.get('description') or '').strip():
                line += ' -- ' + w['description'].strip()
            flines.append(line)
        parts.append('---\n\n# Workflows you can run\n\n' + '\n'.join(flines))

    # The agent's OWN addresses (sn_config.agent_channels). Nothing in the prompt
    # used to say this, so an agent could not answer "what is your email" about
    # itself, and -- worse -- repeated whatever address it had once read in a
    # document, long after the binding changed. The binding is the fact; a note
    # in memory is a memory. This section is rendered from the binding.
    my_channels = [c for c in (channels or []) if c.get("instance_ref")]
    if my_channels:
        mlines = [
            "These are YOUR OWN addresses on this node -- what a message from you "
            "arrives as. They are the live bindings, so if something you read "
            "elsewhere (a document, a note, your memory) names a different address "
            "for you, that source is out of date and this is right.",
            "",
        ]
        for c in my_channels:
            line = "- **" + c["channel"] + "** -- `" + c["instance_ref"] + "`"
            if not c.get("enabled", True):
                line += " (disabled -- nothing arrives or leaves here right now)"
            mlines.append(line)
        mlines.append("")
        mlines.append(
            "You do not choose which of these a reply leaves from: an answer goes "
            "out as the address it came in to. Mail addressed to another of the "
            "node's addresses may be forwarded into your mailbox -- when that "
            "happens you answer as yourself, from the address above, which is "
            "normal and needs no apology.")
        parts.append("---\n\n# How people reach you\n\n" + "\n".join(mlines))

    # Contacts the agent is granted (sn_config.agent_contacts -> contacts).
    # WHY this is in the prompt at all, given the gate already enforces it: the gate
    # is silent. An agent that does not know its boundary writes messages that never
    # leave and cannot understand why. The prompt INFORMS, the gate ENFORCES, and both
    # read the same grant table, so they cannot disagree. No grants -> no section, and
    # the agent correctly knows no one.
    people_i_may_write_to = [c for c in (contacts or []) if c.get("display_name")]
    if people_i_may_write_to:
        clines = [
            "These are the people and parties you are allowed to contact, and the only "
            "ones. This is not advice: a message to anyone else is refused before it "
            "leaves the node, so writing to an address that is not listed here simply "
            "does nothing. If you need to reach someone who is not on this list, ask "
            "the owner to add them rather than trying.",
            "",
        ]
        for c in people_i_may_write_to:
            line = "- **" + c["display_name"] + "**"
            if c.get("kind"):
                line += " (" + c["kind"] + ")"
            if c.get("org_name"):
                line += ", " + c["org_name"]
            reach = c.get("reach") or []
            if reach:
                line += " -- " + ", ".join(reach)
            clines.append(line)
        parts.append("---\n\n# Who you may contact\n\n" + "\n".join(clines))


    contracts = [t for t in (tool_contracts or []) if (t.get("usage_md") or "").strip()]
    if contracts:
        body = "\n\n".join(t["usage_md"].strip() for t in contracts)
        parts.append("---\n\n# Tool contracts\n\n"
                     "How to use the tools granted to you. These are not optional.\n\n"
                     + body)

    tool_list = [t for t in (tools or []) if t.get("name")]
    if tool_list:
        sorted_tools = sorted(tool_list, key=lambda t: t.get("name", "").lower())
        lines = ["You have access to:"]
        for t in sorted_tools:
            name = t.get("name", "")
            kind = t.get("kind", "")
            desc = t.get("description", "")
            if kind:
                lines.append(f"- {name} ({kind}): {desc}" if desc else f"- {name} ({kind})")
            else:
                lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        body = "\n".join(lines)
        parts.append("---\n\n# Available Tools\n\n" + body)

    # (Policies moved to the top of the prompt, directly under the role -- see
    # the note there. This is where they used to be assembled.)

    kn = [k for k in (knowledge or []) if (k.get("content") or "").strip()]
    if kn:
        body = "\n\n".join("## " + (k.get("title") or "Note") + "\n" + k["content"].strip() for k in kn)
        parts.append("---\n\n# Knowledge\n\n"
                     "Reference facts and documents you can rely on.\n\n" + body)

    docs = [d for d in (documents or []) if (d.get("title") or "").strip()]
    pinned = [d for d in docs if (d.get("content") or "").strip()]
    unpinned = [d for d in docs if not (d.get("content") or "").strip() and (d.get("how") or "").strip()]
    if pinned:
        blocks = []
        for d in pinned:
            snap = (" (snapshot %s)" % d["snapshot"]) if d.get("snapshot") else ""
            blocks.append("## %s%s\n%s" % (d["title"].strip(), snap, d["content"].strip()))
        parts.append("---\n\n# Pinned knowledge\n\n"
                     "These documents are PINNED: their content is included below as of the "
                     "snapshot time. Rely on it. It may be stale if the source changed after the "
                     "snapshot; if you need the very latest, read the live document instead.\n\n"
                     + "\n\n".join(blocks))
    if unpinned:
        body = "\n".join("- %s -- %s" % (d["title"].strip(), (d.get("how") or "").strip()) for d in unpinned)
        parts.append("---\n\n# Documents you can read\n\n"
                     "These documents are attached to you. Read the relevant one before you "
                     "state a fact or quote a number; never answer facts from memory. Each line "
                     "says how to read it.\n\n" + body)

    return "\n\n".join(parts).rstrip() + "\n"


# The names a runtime reads its instructions from (cli_providers.instructions_file).
# Exactly one of them is in an agent's folder: two would hand a runtime that
# reads both the agent twice, and the stale one would outlive a runtime change.
INSTRUCTION_FILES = ("CLAUDE.md", "FLINT.md", "AGENTS.md")


def rewrite_claude(slug: str, body: str, filename: str = "CLAUDE.md") -> Path:
    """Write the agent's composed prompt under the name its runtime reads, and
    remove the other managed names."""
    if filename not in INSTRUCTION_FILES:
        raise ValueError(f"unknown instructions file {filename!r}")
    d = _agent_dir(slug)
    if not d.is_dir():
        raise FileNotFoundError(f"agent directory missing: {d}")
    target = d / filename
    target.write_text(body)
    for other in INSTRUCTION_FILES:
        if other != filename:
            (d / other).unlink(missing_ok=True)
    return target


def read_claude(slug: str) -> str | None:
    d = _agent_dir(slug)
    for name in INSTRUCTION_FILES:
        if (d / name).is_file():
            return (d / name).read_text()
    return None


def append_changelog(slug: str, version: str, note: str) -> Path:
    d = _agent_dir(slug)
    f = d / "CHANGELOG.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = f.read_text() if f.is_file() else "# Changelog\n\n"
    new = existing + f"\n## {version} — {ts}\n\n- {note}\n"
    f.write_text(new)
    return f


def archive_agent_dir(slug: str) -> Path:
    src = _agent_dir(slug)
    if not src.is_dir():
        raise FileNotFoundError(f"agent directory missing: {src}")
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dst = ARCHIVE_ROOT / f"{slug}-{ts}"
    shutil.move(str(src), str(dst))
    return dst
