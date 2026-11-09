"""Bootstrap sn_core.tools with the System integration only.

After B2-Integrations, every tool belongs to an integration row in
sn_config.integrations. Companion installers register their own integration
(see companions/_shared/integration.sh + each *.sh "installer); external
ones are registered through the admin panel.

This seed is therefore just the system bootstrap: the four primitives that
exist on every node (bash, fs, fetch) plus the AI Gateway.

Safe to re-run -- ON CONFLICT (name) DO NOTHING for tools, idempotent
INSERT for the integration row.
"""

import logging
import os

import psycopg2

from lib import default_grants


log = logging.getLogger("seed-tools")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


SYSTEM_INTEGRATION = (
    "system",
    "System",
    "Built-in primitives (bash, fs, fetch) and the AI Gateway.",
    "system",         # kind
    "available",      # status
    "",               # install_uri
    None,             # companion_slug -- system has no installer companion
)


SYSTEM_TOOLS: list[tuple] = [
    # name      kind      title         description                            install_uri                       status        tags          integration_slug
    ("bash",    "builtin", "Bash",       "Run a shell command",                "",                               "available", ["primitive"], "system"),
    ("fs",      "builtin", "Filesystem", "Read/write files within the agent's ACL", "",                          "available", ["primitive"], "system"),
    ("fetch",   "builtin", "HTTP fetch", "Plain GET/POST for public URLs",     "",                               "available", ["primitive"], "system"),
    ("gateway", "api",     "AI Gateway", "Single egress to all LLM providers", "http://127.0.0.1:8000/api/v1", "available", ["llm", "egress"], "system"),
]

# The agent runtime's own tools, the ones a chat agent uses most. A tool not
# here still shows in a chat, as its name in words ("list_processes" ->
# "List processes"). name, title, description, doing (what the chat says while
# it runs).
RUNTIME_TOOLS: list[tuple] = [
    ("read_file",              "Read a file",        "Read a file's contents",          "Reading a file"),
    ("write_file",             "Write a file",       "Write or create a file",          "Writing a file"),
    ("edit_file",              "Edit a file",        "Change part of a file",           "Editing a file"),
    ("list_directory",         "List a folder",      "List files and folders",          "Looking through files"),
    ("glob",                   "Find files",         "Find files by a pattern",         "Looking through files"),
    ("search_in_files",        "Search in files",    "Search text across files",        "Searching in files"),
    ("view_image",             "View an image",      "Look at an image file",           "Looking at an image"),
    ("run_command",            "Run a command",      "Run a shell command",             "Running a command"),
    ("run_background_command", "Run in background",  "Run a long command in the background", "Running a command"),
    ("web_search",             "Search the web",     "Search the web",                  "Searching the web"),
    ("web_fetch",              "Read a web page",    "Fetch a web page",                "Reading a page"),
    ("think",                  "Think",              "Reason step by step",             "Thinking"),
    ("memory_search",          "Search its notes",   "Search the runtime's own memory", "Checking its notes"),
    ("memory_write",           "Note down",          "Save a fact to the runtime's own memory", "Making a note"),
]

# What a chat says while a registered tool runs. An MCP server's own row speaks
# for its tools that have no row of their own (messaging, reminders).
TOOL_DOING: dict[str, str] = {
    "bash": "Running a command", "fs": "Working with files", "fetch": "Reading a page",
    "messaging": "Working with messages", "reminders": "Setting a reminder",
    "secrets": "Looking up a credential", "gdrive": "Working in Google Drive",
    "mesh": "Checking its memory", "mesh_search": "Searching its memory",
    "mesh_add": "Remembering", "mesh_update": "Correcting a memory", "mesh_delete": "Forgetting",
    "mesh_get": "Reading from its memory", "mesh_recent": "Checking its memory",
}

# Parameters of the builtins brin runs itself. A model on an HTTP runtime
# learns a tool's arguments only from this schema; with {} it guessed, and bash
# was called with "code" instead of "command" for nine rounds in production.
# A CLI agent is unaffected: its CLI brings its own tools. These rows are the
# product's, so the schema is rewritten on every run. fs is not dispatched by
# brin and gets no schema until it is.
SYSTEM_TOOL_SCHEMAS: dict[str, dict] = {
    "bash": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
        },
        "required": ["command"],
    },
    "fetch": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL of a public site."},
            "method": {"type": "string", "enum": ["GET", "POST"], "description": "Default GET."},
            "body": {"type": "string", "description": "Request body for POST."},
        },
        "required": ["url"],
    },
}


def main():
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor() as cur:
            # Idempotent system integration row
            cur.execute(
                """
                INSERT INTO sn_config.integrations
                  (slug, label, description, kind, status, install_uri, companion_slug)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                  label        = EXCLUDED.label,
                  description  = EXCLUDED.description,
                  kind         = EXCLUDED.kind,
                  status       = EXCLUDED.status,
                  install_uri  = EXCLUDED.install_uri,
                  updated_at   = now(),
                  archived_at  = NULL
                """,
                SYSTEM_INTEGRATION,
            )

            # Existing MCP servers may predate the umbrella catalogue contract.
            # Seed from the server registry, not a hand-maintained slug list: a
            # restarted node must repair screenbox, analytics, google-ads, secrets,
            # and any other registered server in the same way.
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, mcp_server_id, status,
                   created_by, updated_by)
                SELECT s.slug, 'mcp', s.name, 'Grant to use this MCP server',
                       s.id, 'available', 'seed', 'seed'
                  FROM sn_config.mcp_servers s
                 WHERE true
                ON CONFLICT (name) DO UPDATE SET
                  mcp_server_id = EXCLUDED.mcp_server_id,
                  updated_at = now()
                WHERE sn_core.tools.kind = 'mcp'
                """
            )

            import json as _json
            inserted = 0
            for r in SYSTEM_TOOLS:
                schema = SYSTEM_TOOL_SCHEMAS.get(r[0])
                cur.execute(
                    """
                    INSERT INTO sn_core.tools
                      (name, kind, title, description, install_uri, status, tags,
                       integration_slug, input_schema, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            COALESCE(%s::jsonb, '{}'::jsonb), 'seed', 'seed')
                    ON CONFLICT (name) DO UPDATE SET
                      integration_slug = COALESCE(sn_core.tools.integration_slug, EXCLUDED.integration_slug),
                      input_schema     = CASE WHEN %s::jsonb IS NULL THEN sn_core.tools.input_schema
                                              ELSE EXCLUDED.input_schema END,
                      updated_at       = now()
                    """,
                    (*r, _json.dumps(schema) if schema else None,
                     _json.dumps(schema) if schema else None),
                )
                inserted += cur.rowcount
            log.info("seed: system integration ok, %d tool rows touched", inserted)

            # The agent runtime's own tools (Flint's), so a chat can say what an
            # agent does in words. Nobody is granted them: the runtime has them
            # anyway, and brin does not dispatch an unwired builtin.
            for name, title, desc, doing in RUNTIME_TOOLS:
                cur.execute(
                    "INSERT INTO sn_core.tools (name, kind, title, description, status, tags, "
                    "  integration_slug, doing, created_by, updated_by) "
                    "VALUES (%s, 'builtin', %s, %s, 'available', ARRAY['runtime'], 'system', %s, 'seed', 'seed') "
                    "ON CONFLICT (name) DO NOTHING", (name, title, desc, doing))
            # Rows that predate the column get their words once; an operator's
            # own wording, set in the panel, is never overwritten.
            for name, doing in TOOL_DOING.items():
                cur.execute("UPDATE sn_core.tools SET doing = %s WHERE name = %s AND doing IS NULL",
                            (doing, name))

            # messages_search: the cross-channel recall endpoint on
            # core-api itself, exposed as an api catalog tool so ANY agent -- the
            # voice line and the desk chat alike -- can look up a person's history
            # with parameters. Point the core-api integration at core-api's docker0
            # address, upsert the tool, and grant it to every enabled agent.
            import json as _json
            # UPSERT, not UPDATE. An UPDATE of a row that is not there succeeds and
            # changes nothing, and the tool inserted below carries a foreign key to
            # this integration. So on a FRESH node the seed died right here with
            # "Key (integration_slug)=(core-api) is not present in table
            # integrations", core-api never started, and the install stopped at its
            # first Phase 2 companion. On an existing node the row was already there
            # from an earlier release, which is why this stayed invisible until a
            # node was built from scratch on 2026-08-16.

            # the capability lookup repair, for nodes that were updated rather than built.
            # The ON CONFLICT below deliberately keeps whatever endpoint is already
            # stored, so re-installing can never correct a wrong one. Two wrong
            # values are in the field: legacy nodes hold a Docker bridge
            # literal (http://172.19.0.1:8030 on this node), and 1.12.33 wrote
            # http://host.docker.internal:8030. Every reader of the row is native --
            # brin is smart-node-dispatcher.service, and tools/update-on.sh Phase 4
            # curls this endpoint from the node host, where neither resolves: the
            # bridge IP moves when the network is recreated, and getent exits 2 on
            # the container-side name, so the health probe returns HTTP 000.
            # Normalise the host and keep the port, so a node running core-api on a
            # non-default port keeps it. Reported from a node in the field against 1.12.33.
            cur.execute(
                """
                UPDATE sn_config.integrations
                   SET endpoint   = regexp_replace(endpoint,
                                                   %s, %s),
                       updated_at = now()
                 WHERE slug = %s
                   AND endpoint LIKE %s
                   AND endpoint NOT LIKE %s
                """,
                ("^http://[^/:]+", "http://127.0.0.1",
                 "core-api", "http://%", "http://127.0.0.1:%"),
            )

            cur.execute(
                """
                INSERT INTO sn_config.integrations
                  (slug, label, description, kind, status, install_uri,
                   companion_slug, endpoint)
                -- kind and companion_slug copied from the row a live node already
                -- carries, not invented: kind is constrained to
                -- system|app|external|mcp-only, and core-api is 'system' there
                -- with companion_slug NULL. Guessing 'api' here failed the check
                -- constraint on the first fresh install.
                VALUES ('core-api', 'Core API',
                        'The node controller: registries, messages, system status.',
                        'system', 'available', '', NULL, %s)
                ON CONFLICT (slug) DO UPDATE SET
                  endpoint   = COALESCE(NULLIF(sn_config.integrations.endpoint, ''),
                                        EXCLUDED.endpoint),
                  updated_at = now()
                """,
                (os.environ.get("SN_CORE_API", ""),),
            )
            _ms_desc = ("Look up your conversation with a specific person across EVERY "
                        "channel (telegram, email, web chat, voice). Use it whenever they "
                        "ask what you discussed, what has come in, or refer to earlier "
                        "messages -- and BEFORE ever telling them you cannot see their "
                        "messages. Pass only identity by default (it searches everything, "
                        "newest first). Add channel ONLY if they name one (e.g. telegram), "
                        "and since/until ONLY if they ask for a time range -- otherwise "
                        "omit them. To find what was SAID, pass q (words, any order, all "
                        "must occur): with identity it searches that person's messages, "
                        "without it every conversation you are part of, newest first -- "
                        "use it to find a message, a decision or a promise you only "
                        "remember by its words.")
            _ms_schema = _json.dumps({
                "type": "object",
                "properties": {
                    "identity": {"type": "string", "description": "Who to look up: the caller's email or handle. For the person you are talking to, use the email or name you were told for them. Optional when q is given."},
                    "q": {"type": "string", "description": "Words to find in the message text, any order; every word must occur; case-insensitive."},
                    "limit": {"type": "integer", "description": "How many recent messages to return. Default 20; raise it when asked to look deeper or further back."},
                    "since": {"type": "string", "description": "ISO date or datetime lower bound, e.g. 2026-08-15 for since yesterday."},
                    "until": {"type": "string", "description": "ISO date or datetime upper bound."},
                    "channel": {"type": "string", "description": "Restrict to one channel: telegram, email, web-chat, or voice."},
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('messages_search', 'api', 'Search messages with a person', %s,
                        'POST', '/api/messages/search', 'core-api', %s::jsonb,
                        'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  http_method      = 'POST',
                  http_path        = '/api/messages/search',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  description      = EXCLUDED.description,
                  updated_at       = now()
                """,
                (_ms_desc, _ms_schema),
            )
            # Every agent has it: marked in the registry, granted by the one
            # function core-api also calls when it creates an agent.
            cur.execute("UPDATE sn_core.tools SET default_caps = '{read}' WHERE name = ANY(%s)",
                        (['messages_search'],))
            default_grants.apply(cur)
            log.info("seed: messages_search tool + agent grants ok")
            # my_wakes: an agent must be able to account to its owner for
            # its own activity and spend. Until now it could read its crontab and
            # nothing else -- which describes one of the two ways it is woken, and
            # not the one in use since the cron wake was retired. Asked
            # why it kept waking, an agent answered truthfully from the only
            # source it had and under-reported itself.
            #
            # GRANTED TO EVERY ENABLED AGENT, deliberately, like messages_search.
            # Making self-accounting an opt-in privilege would reproduce the
            # problem it exists to solve: the capability would exist, some node
            # would not have switched it on, and nobody would notice until an
            # owner asked a question nobody could answer.
            #
            # No parameter names the agent: the endpoint answers about its caller,
            # whom the kernel names over the unix socket.
            _mw_desc = ("Report your OWN wake history and spend: how many times you "
                        "were woken over the last N days, by what, how many model "
                        "sessions that became, and what they cost. Use it whenever the "
                        "owner asks why you keep waking, what you have been doing, or "
                        "what you are costing -- and BEFORE answering such a question "
                        "from your schedule, which describes only one of the ways you "
                        "can be woken and not the one in use. It reports on you; there "
                        "is no way to ask about another agent.")
            _mw_schema = _json.dumps({
                "type": "object", "required": [],
                "properties": {
                    "days": {"type": "integer", "description": "How far back to look. Default 7, maximum 90."},
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('my_wakes', 'api', 'Account for your own wakes and spend', %s,
                        'POST', '/api/agents/me/wakes', 'core-api', %s::jsonb,
                        'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  http_method      = 'POST',
                  http_path        = '/api/agents/me/wakes',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  description      = EXCLUDED.description,
                  updated_at       = now()
                """,
                (_mw_desc, _mw_schema),
            )
            # Every agent has it: marked in the registry, granted by the one
            # function core-api also calls when it creates an agent.
            cur.execute("UPDATE sn_core.tools SET default_caps = '{read}' WHERE name = ANY(%s)",
                        (['my_wakes'],))
            default_grants.apply(cur)
            log.info("seed: my_wakes tool + agent grants ok")

            # messaging for an agent on an HTTP runtime. The `messaging`
            # grant is a stdio MCP server that the gateway spawns as the agent's
            # own OS user for a CLI session, so the core-api socket knows who is
            # calling. brin's tool loop cannot do that: it expands only MCP
            # servers with a URL, and skipped `messaging`, so an agent on Gemini
            # or OpenRouter could not read the letter that woke it or answer it.
            # These are the same four endpoints as api tools, which brin calls
            # with the node service secret on behalf of the agent (the fast
            # tier; brin:* is in SN_SERVICE_AGENTS by default). They are not a
            # second grant decision: every agent that holds `messaging` gets
            # them, and a CLI agent never uses them (its loop is the CLI's).
            _msg_tools = [
                ("messages_inbox", "Read your inbox", "GET", "/api/messages/inbox",
                 "Check what is waiting for you. view=new returns only the messages still "
                 "waiting for you, each with its id and thread_id. view=thread with a "
                 "thread_id returns that one conversation's recent history.",
                 {"type": "object", "required": ["view"], "properties": {
                     "view": {"type": "string", "enum": ["new", "thread"]},
                     "thread_id": {"type": "string", "description": "With view=thread."},
                     "limit": {"type": "integer", "description": "With view=thread, at most 50."}}}),
                ("messages_reply", "Answer a message", "POST", "/api/messages/reply",
                 "Answer one message, including email, in its existing thread and close it. "
                 "Email keeps the original audience by default; only_sender replies privately. "
                 "Every external recipient, including cc, needs a contact grant or the "
                 "outbound gate blocks delivery.",
                 {"type": "object", "required": ["to_message_id", "body"], "properties": {
                     "to_message_id": {"type": "integer", "description": "id of the message you answer."},
                     "body": {"type": "string", "description": "Your reply."},
                     "cc": {"type": "array", "items": {"type": "string"},
                            "description": "Explicit email copy list; replaces the default audience and takes precedence over only_sender. Each address needs a contact grant."},
                     "only_sender": {"type": "boolean", "description": "For email, reply only to the sender when cc is omitted."}}}),
                ("messages_send", "Start a message", "POST", "/api/messages/send",
                 "Start a new message to a colleague or email address, or relay an answer "
                 "to your requester. Email is queued for delivery; every To and Cc "
                 "address needs a contact grant or the outbound gate blocks it.",
                 {"type": "object", "required": ["to", "body"], "properties": {
                     "to": {"type": "string", "description": "A colleague's agent slug or an email address containing @. For a to_relay item, use its origin conversation (your own chat), never another agent's chat. To answer an existing thread use messages_reply."},
                     "body": {"type": "string"},
                     "channel": {"type": "string", "description": "For a relay only: the origin channel. Email is inferred from an @ address."},
                     "subject": {"type": "string", "description": "Subject for a new email thread."},
                     "cc": {"type": "array", "items": {"type": "string"},
                            "description": "Email copy addresses; each needs a contact grant."}}}),
                ("messages_dismiss", "Close a message without replying", "POST", "/api/messages/dismiss",
                 "Close a message that needs no answer (a thanks or an acknowledgment).",
                 {"type": "object", "required": ["message_id"], "properties": {
                     "message_id": {"type": "integer"}}}),
            ]
            for name, title, method, path, desc, schema in _msg_tools:
                cur.execute(
                    """
                    INSERT INTO sn_core.tools
                      (name, kind, title, description, http_method, http_path,
                       integration_slug, input_schema, status, created_by, updated_by)
                    VALUES (%s, 'api', %s, %s, %s, %s, 'core-api', %s::jsonb,
                            'available', 'seed', 'seed')
                    ON CONFLICT (name) DO UPDATE SET
                      title            = EXCLUDED.title,
                      description      = EXCLUDED.description,
                      http_method      = EXCLUDED.http_method,
                      http_path        = EXCLUDED.http_path,
                      integration_slug = 'core-api',
                      input_schema     = EXCLUDED.input_schema,
                      updated_at       = now()
                    """,
                    (name, title, desc, method, path, _json.dumps(schema)),
                )
            cur.execute(
                """
                INSERT INTO sn_config.agent_tools (agent_id, tool_id, granted_caps)
                SELECT g.agent_id, t.id, '{read,write}'
                  FROM sn_config.agent_tools g
                  JOIN sn_core.tools m ON m.id = g.tool_id AND m.name = 'messaging'
                  JOIN sn_core.tools t ON t.name = ANY(%s)
                ON CONFLICT (agent_id, tool_id) DO NOTHING
                """,
                ([n for n, *_ in _msg_tools],),
            )
            log.info("seed: messaging api tools + grants for messaging holders ok")

            # planner for an agent on an HTTP runtime. Same shape as
            # messaging above: a CLI agent reaches the planner through the
            # tracking companion's stdio MCP server (planner_task, _comments,
            # _comment, _advance, _search), which brin cannot run. The HTTP agent
            # had only `planner` (file) and `planner_tasks` (list), so it could
            # not read the task it was woken for, log its work on it, or move it.
            # These are the same planner routes behind core-api's proxy, which
            # gates them on the `planner` grant. brin fills {task_id} into the
            # path. Reads go to every holder of `planner`; the two writes only to
            # holders whose grant includes write.
            _planner_tools = [
                ("planner_task", "Read a task", "GET", "/api/v1/tasks/{task_id}",
                 "Read one task in full: what was actually asked, and its current state "
                 "(status, wait_reason or close_outcome, assignee, reviewer).",
                 {"type": "object", "additionalProperties": False, "required": ["task_id"], "properties": {
                     "task_id": {"type": "integer"}}}, False),
                ("planner_comments", "Read a task's log", "GET", "/api/v1/tasks/{task_id}/comments",
                 "Read the log of a task. Everything anyone did to it is written here. "
                 "With task_id alone you get the whole log, oldest first, and a long log "
                 "is cut off before its end. So when you were told a comment id, pass "
                 "comment_id and read that comment; to see what happened most recently, "
                 "pass last; to find where something was said in this task, pass q.",
                 {"type": "object", "additionalProperties": False, "required": ["task_id"], "properties": {
                     "task_id": {"type": "integer"},
                     "comment_id": {"type": "integer", "description": "Only this comment."},
                     "last": {"type": "integer", "description": "Only the newest N comments, max 200."},
                     "q": {"type": "string", "description": "Only comments of this task containing "
                           "this text; case-insensitive."}}}, False),
                ("planner_search", "Search tasks", "GET", "/api/v1/tasks/search",
                 "Find tasks by words, across titles, descriptions and comments, in every "
                 "status. Type the words you remember, in any order. Use it BEFORE filing: "
                 "a duplicate splits the record in two.",
                 {"type": "object", "additionalProperties": False, "required": ["q"], "properties": {
                     "q": {"type": "string", "description": "Words to look for, any order; every word must occur in the task; case-insensitive."},
                     "author": {"type": "string", "description": "Only tasks this name wrote a comment on or filed."},
                     "limit": {"type": "integer", "description": "At most this many, default 50, max 200."}}},
                 False),
                ("planner_comment", "Log work on a task", "POST", "/api/v1/tasks/{task_id}/comments",
                 "Write what you did, found, decided or rejected onto the task, as you go. "
                 "Work that is not in the task did not happen as far as the next reader can tell.",
                 {"type": "object", "additionalProperties": False, "required": ["task_id", "content"], "properties": {
                     "task_id": {"type": "integer"},
                     "content": {"type": "string"}}}, True),
                ("planner_advance", "Move a task", "PATCH", "/api/v1/tasks/{task_id}",
                 "Move a task to a new status, and nothing else. This call changes the "
                 "status and adds the comment the status needs; it cannot set an assignee, "
                 "a reviewer, a priority or a title, and passing one is refused rather "
                 "than ignored. A task is queued (not being worked on; wait_reason says "
                 "why), in_progress, or closed (close_outcome says how). Start work: "
                 "in_progress. Hand to review: queued with wait_reason review. Stop for a "
                 "person: queued with wait_reason person and a comment saying who and why. "
                 "Finish: closed with close_outcome completed and the report as comment. "
                 "Any status may follow any other; what the planner refuses it names. A "
                 "fix you made yourself goes to review, not closed: only the reviewer "
                 "closes a reviewed task as completed. cancelled, rejected and "
                 "not_possible are a person's call, not an agent's.",
                 {"type": "object", "additionalProperties": False, "required": ["task_id", "status"], "properties": {
                     "task_id": {"type": "integer"},
                     "status": {"type": "string", "enum": ["queued", "in_progress", "closed"]},
                     "wait_reason": {"type": "string", "description": "With queued: why it waits, "
                                     "e.g. review, approval, owner, person, dependency, schedule, error."},
                     "close_outcome": {"type": "string", "description": "With closed: how it ended, "
                                       "completed or duplicate for an agent."},
                     "comment": {"type": "string"}}}, True),
            ]
            # The rows carry a foreign key to the planner integration, which the
            # tracking stage registers AFTER core-api is installed. On a fresh node
            # the insert died on that key, seed-tools exited 1 and core-api never
            # started, so a clean install stopped at its first Phase 2 companion:
            # the 2026-08-16 core-api failure above, again. Skip the rows until the
            # integration exists and say so; tracking restarts core-api once it has
            # registered the integration, and this seed writes them then.
            cur.execute("SELECT 1 FROM sn_config.integrations WHERE slug = 'planner'")
            if cur.fetchone() is None:
                log.info("seed: planner integration not registered yet -- planner api "
                         "rows skipped; the tracking stage reseeds when it registers it")
            else:
                for name, title, method, path, desc, schema, _w in _planner_tools:
                    cur.execute(
                        """
                        INSERT INTO sn_core.tools
                          (name, kind, title, description, http_method, http_path,
                           integration_slug, input_schema, status, created_by, updated_by)
                        VALUES (%s, 'api', %s, %s, %s, %s, 'planner', %s::jsonb,
                                'available', 'seed', 'seed')
                        ON CONFLICT (name) DO UPDATE SET
                          title            = EXCLUDED.title,
                          description      = EXCLUDED.description,
                          http_method      = EXCLUDED.http_method,
                          http_path        = EXCLUDED.http_path,
                          integration_slug = 'planner',
                          input_schema     = EXCLUDED.input_schema,
                          updated_at       = now()
                        """,
                        (name, title, desc, method, path, _json.dumps(schema)),
                    )
                for names, caps, need_write in (
                    ([n for n, *_, w in _planner_tools if not w], "{read}", False),
                    ([n for n, *_, w in _planner_tools if w], "{read,write}", True),
                ):
                    cur.execute(
                        """
                        INSERT INTO sn_config.agent_tools (agent_id, tool_id, granted_caps)
                        SELECT g.agent_id, t.id, %s::text[]
                          FROM sn_config.agent_tools g
                          JOIN sn_core.tools p ON p.id = g.tool_id AND p.name = 'planner'
                          JOIN sn_core.tools t ON t.name = ANY(%s)
                          JOIN sn_config.agents a ON a.id = g.agent_id
                           AND a.archived_at IS NULL
                           AND a.model <> 'none'
                           -- an HTTP runtime, the only one that dispatches api rows:
                           -- the model's provider is not an enabled cli_providers row
                           AND NOT EXISTS (
                                 SELECT 1 FROM sn_config.cli_providers cp
                                  WHERE cp.enabled AND cp.slug = CASE
                                        WHEN position('/' IN a.model) = 0 THEN 'claude-cli'
                                        ELSE split_part(a.model, '/', 1) END)
                         WHERE NOT %s OR 'write' = ANY(g.granted_caps)
                        ON CONFLICT (agent_id, tool_id) DO NOTHING
                        """,
                        (caps, names, need_write),
                    )
                log.info("seed: planner api tools + grants for planner holders ok")

            # secrets_resolve: agents are not told where credentials live, so
            # they search. An agent that got 404 from /resolve concluded the secret
            # did not exist and went hunting on the filesystem; it found a stale file
            # and pushed it into a container where it did not work. This tool contract
            # -- usage_md only, no HTTP dispatch -- tells every agent the store exists,
            # how to call it, and what the status codes mean. It is a documentation
            # tool, not a callable: the agent makes the HTTP call directly, the same
            # way it calls the planner or core-api. Granted to all agents so the
            # contract appears in every CLAUDE.md without per-agent configuration.
            # Short on purpose: every agent reads it on every model call, and most
            # have no secrets server, so the status codes and the store-and-grant
            # tools were text for the few (2026-10-29: ~350 of ~7.7k tokens).
            _sr_usage = (
                "## secrets -- the node's credential store\n\n"
                "Credentials live in the secret store, never in files: do not search the "
                "filesystem, archives or containers for one, and never write a value "
                "anywhere. With the `secrets` server, `resolve_secret(ref)` reads a ref "
                "granted to you; 403 means not granted (ask the Configurator), 404 means no such "
                "ref (ask the operator). Without it, ask the Configurator. The Configurator and the "
                "sysadmin store credentials and grant refs with `set_secret` and "
                "`grant_secret_ref`."
            )
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, usage_md,
                   integration_slug, status, created_by, updated_by)
                VALUES ('secrets_resolve', 'api',
                        'Secrets store contract',
                        'Documents the node secret store (GET /api/secrets/resolve?ref=<ref>). '
                        'Read this before looking for a credential anywhere else.',
                        %s, 'core-api', 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  usage_md   = EXCLUDED.usage_md,
                  description = EXCLUDED.description,
                  updated_at  = now()
                """,
                (_sr_usage,),
            )
            # Every agent has it: marked in the registry, granted by the one
            # function core-api also calls when it creates an agent.
            cur.execute("UPDATE sn_core.tools SET default_caps = '{read}' WHERE name = ANY(%s)",
                        (['secrets_resolve'],))
            default_grants.apply(cur)
            log.info("seed: secrets_resolve tool contract + agent grants ok")

            # reminders: every agent has the `reminders` MCP server (marked
            # default_on below). The contract says where a reminder to a person
            # goes, because a runtime's own task list looks like the place and is
            # not: an agent on Flint put "remind me in 2 minutes" there, promised
            # it, and nothing ever reached the chat (2026-10-29).
            _rm_usage = (
                "## reminders -- anything delayed\n\n"
                "Use `remind_me` (the `reminders` server). Your runtime's own task or "
                "schedule list does not fire on this node."
            )
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, usage_md,
                   integration_slug, status, created_by, updated_by)
                VALUES ('reminders_contract', 'api', 'Reminders contract',
                        'Telling a person something at a time, and doing something later: the reminders MCP server.',
                        %s, 'core-api', 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  usage_md   = EXCLUDED.usage_md,
                  description = EXCLUDED.description,
                  updated_at  = now()
                """,
                (_rm_usage,),
            )
            # Every agent has it: marked in the registry, granted by the one
            # function core-api also calls when it creates an agent.
            cur.execute("UPDATE sn_core.tools SET default_caps = '{read}' WHERE name = ANY(%s)",
                        (['reminders_contract', 'messaging'],))
            default_grants.apply(cur)
            # messaging and reminders are in every .mcp.json: their installers
            # register them with default_on (this seed runs before those rows exist)
            log.info("seed: reminders + messaging contracts for every agent ok")

            # node_summary: one endpoint answers "is the node alright over
            # the last N hours". Granted to the foreman agent only -- foreman is the
            # one who answers operational questions from the owner. Other agents can
            # be granted it later via the panel if needed.
            _ns_desc = (
                "Get an operational summary of the node over the last N hours. "
                "Returns verdict (ok/degraded/broken), reasons, tool call counts, "
                "refused-calls, agent activity (who is doing what right now), "
                "cron health, gateway stats, health-probe failures, and the active "
                "release state. Use this when asked whether the node is healthy, "
                "what is failing, or who is busy. Pass hours=2 for the last two hours. "
                "One call answers all of it; do not split it into multiple lookups. "
                "Do NOT pass hours larger than 24 unless asked for a specific range."
            )
            _ns_schema = _json.dumps({
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "number",
                        "description": (
                            "How many hours to cover. Default 1. Use 2 for 'last two hours'. "
                            "Accepts fractions (0.5). Maximum 168 (one week)."
                        ),
                        "default": 1,
                    },
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('node_summary', 'api',
                        'Node health summary',
                        %s, 'GET', '/api/system/summary',
                        'core-api', %s::jsonb, 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  http_method      = 'GET',
                  http_path        = '/api/system/summary',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  description      = EXCLUDED.description,
                  updated_at       = now()
                """,
                (_ns_desc, _ns_schema),
            )
            # Grant to the configurator agent (answers operational questions
            # from the owner). Look up by capability='configurator' (
            # Part 3), never by a literal slug: any node where the owner has
            # renamed the configurator would otherwise miss the grant. The
            # role='Foreman' fallback covers the narrow
            # window on a legacy node where the capability column exists
            # (B02 CREATE TABLE) but the backfill CASE has not yet run.
            cur.execute(
                """
                INSERT INTO sn_config.agent_tools (agent_id, tool_id, granted_caps)
                SELECT a.id, t.id, '{read}'
                  FROM sn_config.agents a, sn_core.tools t
                 WHERE t.name = 'node_summary'
                   AND (a.capability = 'configurator'
                        OR (a.capability = '' AND a.role = 'Foreman'))
                   AND a.enabled AND a.archived_at IS NULL
                ON CONFLICT (agent_id, tool_id) DO NOTHING
                """
            )
            log.info("seed: node_summary tool + configurator grant ok")

            # work_now: first slice. Answers "who is doing what right now" on
            # this node. Returns the same JSON as GET /api/work/now, including
            # the attribution block that reports how each tie was made
            # (explicit | inferred | unattributed). Grants OFF by default.
            _wn_desc = (
                "Get a one-row-per-live-agent snapshot of work on this node right now. "
                "Each row carries the agent, the task_id they are tied to (when a tie "
                "can be made), task_title, since, last_step, and attribution "
                "('explicit' when a planner tool call named the task_id, 'inferred' "
                "when the planner dashboard says the agent has it in_progress but no "
                "explicit event tie exists in the 60-minute window, 'unattributed' "
                "otherwise). The response also carries an attribution block that "
                "reports coverage and the layer's own blind spots. Call this when "
                "asked who is busy or what an agent is doing right now; one call "
                "answers it. Pass fresh=1 to bypass the 10-second cache."
            )
            _wn_schema = _json.dumps({
                "type": "object",
                "properties": {
                    "fresh": {
                        "type": "integer",
                        "enum": [0, 1],
                        "default": 0,
                        "description": "Pass 1 to bypass the 10-second cache. Default 0.",
                    },
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('work_now', 'api',
                        'Work state: who is doing what right now',
                        %s, 'GET', '/api/work/now',
                        'core-api', %s::jsonb, 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  http_method      = 'GET',
                  http_path        = '/api/work/now',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  description      = EXCLUDED.description,
                  updated_at       = now()
                """,
                (_wn_desc, _wn_schema),
            )
            # No auto-grant. Grants are off by default; the owner names the
            # first agents to receive the tool via the panel after this
            # preview lands.
            log.info("seed: work_now tool registered (grants off by default)")

            # project_state
            #
            # Sibling of work_now on the work state: one deterministic
            # read across the eight sources (planner, mesh, Doci, sn_core.messages)
            # scoped to a single project and a time window. Returns raw rows and
            # counts the agent uses to write a fixed-shape stand-up card. The
            # collector produces facts; the agent writes prose ("facts, not narrative").
            _ps_desc = (
                "Collect project state facts for one project over one window. "
                "Reads all eight sources (goals, linked tasks, moved, comments, "
                "cross-project mentions, mesh memory, Doci documents, owner "
                "channels) and emits two derived fields (waiting_for_owner of "
                "any age, where_it_stopped as a fallback anchor for silent "
                "windows). Window accepts ISO 8601 UTC, a bare date "
                "(2026-10-10), or a relative shorthand (-7d, -72h, -30m). "
                "Pass fresh=1 to bypass the 30-second cache. Any single source "
                "failure is surfaced in `notes` so a partial card is still "
                "useful. Call this when asked for a stand-up on a project; one "
                "call answers it."
            )
            _ps_schema = _json.dumps({
                "type": "object",
                "required": ["slug", "since"],
                "properties": {
                    "slug": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "description": "Planner project slug (e.g. website, mobile-app).",
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Start of the window: ISO 8601 UTC "
                            "(2026-10-10T00:00:00Z), bare date (2026-10-10), "
                            "or relative shorthand (-7d, -72h, -30m)."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "End of the window; defaults to now when omitted. "
                            "Same formats as since."
                        ),
                    },
                    "fresh": {
                        "type": "integer",
                        "enum": [0, 1],
                        "default": 0,
                        "description": "Pass 1 to bypass the 30-second cache. Default 0.",
                    },
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('project_state', 'api',
                        'Project state: facts for one project over one window',
                        %s, 'GET', '/api/project-state/collect',
                        'core-api', %s::jsonb, 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  http_method      = 'GET',
                  http_path        = '/api/project-state/collect',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  description      = EXCLUDED.description,
                  updated_at       = now()
                """,
                (_ps_desc, _ps_schema),
            )
            # No auto-grant. Grants are off by default; the owner names the
            # agents that receive the tool via the panel.
            log.info("seed: project_state tool registered (grants off by default)")

            # lxc_node_provision / lxc_node_destroy / lxc_node_status
            #
            # Three test-node lifecycle operations exposed through core-api's
            # /api/test-nodes/* proxy (which forwards to the privileged admin-runner).
            # NOT auto-granted: these are admin-only operations. Grant explicitly via
            # the panel or a future companion seed. The usage_md in the DB is what
            # allows a granted agent to call them without per-agent prompt edits
            #
            # DISCONNECTED mode: omit seed_manifest or pass null. The test container
            # runs without any live credentials (no Telegram, no email, no external
            # domain). This is the safe default for acceptance testing.

            _prov_desc = (
                "Create a test LXC container and run the Smart Node installer inside "
                "it. Idempotent: if the container already exists, reports its state "
                "and returns without re-provisioning. Omit seed_manifest for "
                "disconnected mode (no live credentials -- safe default). "
                "Credential isolation is enforced by admin-runner: any seed_manifest "
                "value matching a live node secret is refused before lxc launch."
            )
            _prov_usage = (
                "## lxc_node_provision -- create a test Smart Node container\n\n"
                "POST /api/test-nodes/provision\n\n"
                "    {\"name\": \"sn-test-1\", \"installer_version\": \"1.12.29\"}\n\n"
                "    # With a seed manifest (named test credentials):\n"
                "    {\"name\": \"sn-test-1\", \"installer_version\": \"1.12.29\",\n"
                "     \"seed_manifest\": {\"TELEGRAM_BOT_TOKEN\": \"<test-token>\"}}\n\n"
                "DISCONNECTED MODE (default, omit seed_manifest or pass null):\n"
                "The container runs with no live credentials. No Telegram adapter, no\n"
                "email poller, no external domain. Planner, doci, mesh, agents, and\n"
                "core-api start and function normally. Use this for acceptance testing.\n\n"
                "IDEMPOTENT: if the container name already exists and is running, the\n"
                "operation returns {idempotent: true, state: \"Running\"} and does nothing.\n\n"
                "Returns: {name, version, state, idempotent}\n\n"
                "PREREQUISITE: the host-level operator setup must be complete (LXD socket\n"
                "bind-mounted into admin-runner, lxd group membership). Without it the\n"
                "operation fails at lxc launch. This is a one-time operator action.\n"
            )
            _prov_schema = _json.dumps({
                "type": "object",
                "required": ["name", "installer_version"],
                "properties": {
                    "name": {"type": "string", "description": "Container name (lowercase a-z, digits, hyphens; max 41 chars; must start with a letter). Example: sn-test-1."},
                    "installer_version": {"type": "string", "description": "Smart Node version string, e.g. 1.12.29."},
                    "seed_manifest": {"type": "object", "description": "Optional credential manifest (KEY: value dict). Omit or pass null for disconnected mode (safe default). Any value matching a live node secret is refused.", "nullable": True},
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, usage_md, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('lxc_node_provision', 'api',
                        'Provision a test LXC node',
                        %s, %s, 'POST', '/api/test-nodes/provision',
                        'core-api', %s::jsonb, 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  description      = EXCLUDED.description,
                  usage_md         = EXCLUDED.usage_md,
                  http_method      = 'POST',
                  http_path        = '/api/test-nodes/provision',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  updated_at       = now()
                """,
                (_prov_desc, _prov_usage, _prov_schema),
            )
            log.info("seed: lxc_node_provision tool ok")

            _destr_desc = (
                "Stop and delete a test LXC container (two-call confirmation gate). "
                "Hard guards before any lxc command: refuses if the name matches the "
                "live node hostname; refuses if the name is not in the test-node registry. "
                "First call returns {needs_confirm, confirm_token}; re-submit with the "
                "token to execute."
            )
            _destr_usage = (
                "## lxc_node_destroy -- stop and delete a test container (DESTRUCTIVE)\n\n"
                "POST /api/test-nodes/destroy\n\n"
                "Two-call confirmation gate:\n\n"
                "    # Step 1: request confirmation\n"
                "    {\"name\": \"sn-test-1\"}\n"
                "    -> {\"needs_confirm\": true, \"confirm_token\": \"abc123\", \"message\": \"...\"}\n\n"
                "    # Step 2: execute with the token\n"
                "    {\"name\": \"sn-test-1\", \"confirm_token\": \"abc123\"}\n"
                "    -> {\"name\": \"sn-test-1\", \"destroyed\": true}\n\n"
                "Hard guards (run BEFORE any lxc command):\n"
                "  - Refused if name matches the live node hostname.\n"
                "  - Refused if name is not in the test-node registry.\n\n"
                "The confirm_token expires after 5 minutes. A mismatched or expired token\n"
                "returns HTTP 409. Re-submit the first call to get a new token.\n"
            )
            _destr_schema = _json.dumps({
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Container name (must be in the test-node registry)."},
                    "confirm_token": {"type": "string", "description": "Confirmation token from the first call. Omit for the first call."},
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, usage_md, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('lxc_node_destroy', 'api',
                        'Destroy a test LXC node',
                        %s, %s, 'POST', '/api/test-nodes/destroy',
                        'core-api', %s::jsonb, 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  description      = EXCLUDED.description,
                  usage_md         = EXCLUDED.usage_md,
                  http_method      = 'POST',
                  http_path        = '/api/test-nodes/destroy',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  updated_at       = now()
                """,
                (_destr_desc, _destr_usage, _destr_schema),
            )
            log.info("seed: lxc_node_destroy tool ok")

            _stat_desc = (
                "Read-only: lxc info state for a test container plus its /health "
                "endpoint response (if the container is running). Returns "
                "{found: false} if the container does not exist. No confirmation "
                "required."
            )
            _stat_usage = (
                "## lxc_node_status -- check a test container's state\n\n"
                "POST /api/test-nodes/status\n\n"
                "    {\"name\": \"sn-test-1\"}\n\n"
                "Returns:\n"
                "  - {name, found: false}  -- container does not exist\n"
                "  - {name, found: true, state: \"Running\", health: \"...\"} -- running\n"
                "  - {name, found: true, state: \"Stopped\", health: null} -- stopped\n\n"
                "Read-only. No confirmation required. Use this to check that a\n"
                "provisioned container is up before running acceptance tests.\n"
            )
            _stat_schema = _json.dumps({
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "description": "Container name to query."},
                },
            })
            cur.execute(
                """
                INSERT INTO sn_core.tools
                  (name, kind, title, description, usage_md, http_method, http_path,
                   integration_slug, input_schema, status, created_by, updated_by)
                VALUES ('lxc_node_status', 'api',
                        'Query a test LXC node status',
                        %s, %s, 'POST', '/api/test-nodes/status',
                        'core-api', %s::jsonb, 'available', 'seed', 'seed')
                ON CONFLICT (name) DO UPDATE SET
                  description      = EXCLUDED.description,
                  usage_md         = EXCLUDED.usage_md,
                  http_method      = 'POST',
                  http_path        = '/api/test-nodes/status',
                  integration_slug = 'core-api',
                  input_schema     = EXCLUDED.input_schema,
                  updated_at       = now()
                """,
                (_stat_desc, _stat_usage, _stat_schema),
            )
            log.info("seed: lxc_node_status tool ok")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
