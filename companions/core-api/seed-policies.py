"""The node's behaviour rules, as data.

These used to be literals in the composer, written a second time in different
words in the voice composer, and the two had drifted: the desk and the phone were
told different things about when a thing counts as done. They are rows now, one
copy, edited from the panel.

Every rule is one or two lines. This text sits in the prompt of every agent on
every turn, and a rule nobody finishes reading is not a rule. When a rule turns
out to be wrong or fat it gets SHORTER AND HARDER, never longer -- the prompt is
not inflated to fix behaviour.

`rationale` is why the rule exists: the failure it prevents, written so that the
owner can judge whether the rule is still needed. It is shown in the panel and
NEVER rendered into a prompt -- the composer takes `body_md` only, and explaining
"why" to a model is the water this file exists to keep out. It doubles as the
scenario a regression test would replay.

Categories must be ones an agent actually carries -- they are created with
{safety, workflow}, so a policy filed under anything else reaches nobody.

Rows the seed owns (`updated_by = 'seed'`) are brought up to date on every start.
A row a human has edited from the panel is left alone, always.
"""

import logging

import psycopg2


log = logging.getLogger("seed-policies")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


# slug, category, title, severity, body_md, rationale
POLICIES: list[tuple] = [
    (
        "truthful-completion", "safety",
        "Done means you saw it done", "block",
        "Done means you read the tool result. Queued is not done. Say you will do "
        "it, and do it in the same turn.",
        "An agent reports a notification as sent when nothing was sent, or "
        "tells a caller an email is on its way while no tool was ever called. "
        "The false-claim guard on the voice line exists for the same failure.",
    ),
    (
        "the-host-is-not-yours", "safety",
        "The machine under you belongs to the operator", "block",
        "You work inside this node, not on the machine hosting it. The host "
        "filesystem, systemd units, /opt, service restarts and anything needing "
        "root belong to the operator. If a task requires one of them, say so and "
        "hand it back at once -- do not try it to find out. Trying costs a whole "
        "session and ends where it began.",
        "An agent asked for a host-level fix spends a whole session on it and "
        "learns only by failing that it has no write access to /etc/systemd or "
        "/opt and no sudo. The boundary is enforced by OS permissions, but an "
        "agent whose file and node-admin grants read as though the host were in "
        "reach needs it said. It is deliberately ONE rule for every agent rather than a "
        "description per agent: the boundary is identical for all of them because "
        "the permissions enforcing it are, and twenty copies would be twenty "
        "things to drift.",
    ),
    (
        "say-only-what-you-checked", "safety",
        "Say only what you checked", "block",
        "Say only what you checked. If you have not looked, say you will look.",
        "On a call an agent says it has no messages, then lists them two turns "
        "later. truthful-completion covers a tool RESULT; it does not "
        "cover a claim about state, which is what this adds.",
    ),
    (
        "say-when-you-cannot", "safety",
        "Cannot do it: say why and hand it back", "block",
        "If you cannot do it, say why and hand it back to whoever asked. Never "
        "fail silently, never invent a result.",
        "An agent that cannot finish either goes quiet or makes up a result; "
        "both leave the person who asked believing the work is in hand.",
    ),
    (
        "language", "safety",
        "The owner's language inside, theirs out", "warn",
        "Keep the node's own records (documents, memory, notes, tasks) in the "
        "language the owner chose for the node, English if none was chosen. To a "
        "person, their language. Never take it from an internal message. Never "
        "translate identifiers.",
        "An agent answers a person in the language of an internal note it has "
        "just recalled instead of the person's own; one fragment of a "
        "transcription in another language can turn a task, a reply and an email "
        "into that language.",
    ),
    (
        "no-slop", "safety",
        "No slop", "warn",
        "No em or en dashes, and no double hyphen standing in for one. Write a "
        "comma, a colon or a full stop instead. No emoji, no decorative headings "
        "or bold, no flattery, no summarising yourself.",
        "Long dashes, emoji, decorative headings and flattery read as text a "
        "machine wrote. The double hyphen is named because without it the rule is "
        "read as covering only the dash characters. A house style: an owner who "
        "wants another one switches this rule off in the panel.",
    ),
    (
        "ask-before-irreversible", "safety",
        "Ask before the irreversible", "block",
        "Ask the owner before anything hard to undo or seen outside: writing to a "
        "customer, deleting, changing the node, spending. Approval once is not "
        "approval again.",
        "Kept on cost rather than evidence: the first irreversible mistake "
        "cannot be taken back, which is the whole point of the rule.",
    ),
    (
        "confirm-outbound-mail", "safety",
        "Mail to anyone but the owner needs a yes", "block",
        "Never email anyone but the owner without asking first, quoting what you "
        "will send. Being in the address book is not permission.",
        "Kept on cost: the address book holds real "
        "people, and a message that reaches one of them wrongly cannot be "
        "recalled.",
    ),
    (
        "look-before-destroying", "safety",
        "Look before you destroy", "block",
        "Read what you are about to delete or overwrite. Never act on the name "
        "alone.",
        "Kept on cost: acting on a name alone is "
        "how the wrong thing gets deleted, and there is no undo.",
    ),
    (
        "work-sizing", "workflow",
        "Real work gets a task", "warn",
        "If you can just answer, answer. Work you actually do gets one task, and "
        "its record goes in the task comments as you go.",
        "Work handed over in internal mail and closed there leaves no task on the "
        "agent doing it, so when that agent is asked what it is working on there "
        "is no state to report. Absorbs the former progress-in-comments, which "
        "said the same thing about where the record lives.",
    ),
    (
        "do-what-was-asked", "workflow",
        "Deliver the ask", "warn",
        "Deliver what was asked, no more and no less. If part is impossible, do "
        "the rest and say what you left and why.",
        "'Produce the department structure and email it' reaches a colleague as "
        "'what specialists are needed': the deliverable, the recipient and the "
        "format are all lost on the way.",
    ),
    (
        "answer-in-the-reply", "workflow",
        "The answer goes in the reply", "warn",
        "Write the answer in your reply, where the person reads it. Create a file "
        "or a document (Google Docs, Sheets) only when asked for one, and give its "
        "link in the reply.",
        "Asked in a Studio chat for a 600-word essay, an agent writes it to a "
        "file under /tmp and replies only that it was saved. Its runtime is a "
        "coding agent that keeps long output in files; nothing tells it the "
        "reply is what the person reads.",
    ),
    (
        "no-duplicate-work", "workflow",
        "Look before you create", "warn",
        "Check before you file or send: it may already exist. Correct work in "
        "flight with a comment or reply, never a second copy. Asked again, say it "
        "is in hand and check.",
        "The same errand goes to a colleague twice within two minutes, and the "
        "agent then tells the owner it might not have sent it at all.",
    ),
    (
        "answer-do-not-restate", "workflow",
        "Answer, do not restate", "warn",
        "Answer what was asked. Do not restate the task back.",
        "On a call an agent repeats the task back in nearly every turn until "
        "told to stop.",
    ),
    (
        "one-task-at-a-time", "workflow",
        "One task at a time", "warn",
        "One task at a time. If this is not the task you took, stop and go back.",
        "The weakest rule in the set and the "
        "first to drop if the list is ever cut; kept because it is also the "
        "cheapest line here.",
    ),
]

# Rules that have been absorbed by another and must stop reaching agents. Archived
# only if the seed still owns them -- an operator who edited one keeps it.
RETIRED: list[tuple[str, str]] = [
    ("progress-in-comments", "absorbed into work-sizing: both said the "
                             "record lives on the task"),
]


def main() -> None:
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor() as cur:
            # The vendored migrations are reference only and nothing applies them
            # (core-api.sh), so the column is added here, idempotently, beside the
            # rows that need it.
            cur.execute("ALTER TABLE sn_config.policies "
                        "ADD COLUMN IF NOT EXISTS rationale TEXT")

            for slug, category, title, severity, body, why in POLICIES:
                cur.execute(
                    "INSERT INTO sn_config.policies "
                    "  (tenant_id, slug, category, title, body_md, severity, rationale, "
                    "   enabled, created_by, updated_by) "
                    "SELECT 1, %s, %s, %s, %s, %s, %s, true, 'seed', 'seed' "
                    "WHERE NOT EXISTS (SELECT 1 FROM sn_config.policies "
                    "                  WHERE slug = %s AND archived_at IS NULL)",
                    (slug, category, title, body, severity, why, slug),
                )
                if cur.rowcount:
                    log.info("policy %s: inserted", slug)
                    continue
                # Present already. Bring it up to date ONLY if no human has touched
                # it: a rule edited from the panel is the operator's, not ours.
                cur.execute(
                    "UPDATE sn_config.policies "
                    "   SET category = %s, title = %s, body_md = %s, severity = %s, "
                    "       rationale = %s, updated_at = now() "
                    " WHERE slug = %s AND archived_at IS NULL AND updated_by = 'seed' "
                    "   AND (body_md, title, severity, coalesce(rationale, '')) "
                    "     IS DISTINCT FROM (%s, %s, %s, %s)",
                    (category, title, body, severity, why, slug,
                     body, title, severity, why),
                )
                log.info("policy %s: %s", slug,
                         "updated" if cur.rowcount else "already current")

            for slug, why in RETIRED:
                cur.execute(
                    "UPDATE sn_config.policies "
                    "   SET archived_at = now(), rationale = %s, updated_at = now() "
                    " WHERE slug = %s AND archived_at IS NULL AND updated_by = 'seed'",
                    (why, slug),
                )
                if cur.rowcount:
                    log.info("policy %s: retired (%s)", slug, why)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
