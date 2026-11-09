"""Receiver conflicts must not be resolved by choosing a row; no live node needed."""
import ast
import contextlib
import datetime
import glob
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import uuid

ROOT = Path(sys.argv[1])
REF = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--baseline" else None
sys.argv = sys.argv[:1]


def source(path):
    if REF:
        result = subprocess.run(["git", "-C", str(ROOT), "show", f"{REF}:{path}"], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else ""
    return (ROOT / path).read_text()


def load(path, names, ns):
    selected = []
    for node in ast.parse(source(path)).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(getattr(n, "id", None) in names for n in node.targets):
            selected.append(node)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    exec(compile(tree, path, "exec"), ns)


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code


def _literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "ARRAY[" + ",".join(_literal(v) for v in value) + "]::text[]"
    return "'" + str(value).replace("'", "''") + "'"


class PendingPollOnPostgres(unittest.TestCase):
    """The poll is Postgres SQL (FILTER, json_agg, make_interval, jsonb), so it runs
    on a throwaway private cluster with the shipped schema: own temp directory, own
    socket, no TCP. No psycopg2 here, so each statement is one psql call: the
    transaction around the poll is not what this test is about."""

    @classmethod
    def setUpClass(cls):
        bins = sorted(glob.glob(os.environ.get("PG_BIN", "/usr/lib/postgresql/*/bin")))
        if not bins or not os.path.exists(bins[-1] + "/initdb"):
            raise RuntimeError("cannot run: no initdb (set PG_BIN)")
        cls.bin = bins[-1]
        cls.work = tempfile.mkdtemp(prefix="mailbox-guard-pg-")
        # initdb refuses root; the cluster then belongs to the postgres account.
        cls.as_owner = []
        if os.geteuid() == 0:
            shutil.chown(cls.work, "postgres", "postgres")
            cls.as_owner = ["runuser", "-u", "postgres", "--"]
        data = cls.work + "/data"
        subprocess.run([*cls.as_owner, cls.bin + "/initdb", "-D", data, "-A", "trust", "-U", "postgres"],
                       check=True, capture_output=True)
        subprocess.run([*cls.as_owner, cls.bin + "/pg_ctl", "-D", data, "-w", "-l", cls.work + "/pg.log",
                        "-o", "-k " + cls.work + " -c listen_addresses=", "start"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.addClassCleanup(cls.stop)
        cls.psql = [cls.bin + "/psql", "-h", cls.work, "-U", "postgres", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]
        subprocess.run([*cls.psql, "-d", "postgres", "-c", "CREATE ROLE smartnode LOGIN SUPERUSER"],
                       check=True, capture_output=True)
        subprocess.run([*cls.psql, "-d", "postgres", "-c", "CREATE DATABASE smartnode OWNER smartnode"],
                       check=True, capture_output=True)
        for name in ("B02-schema.sql", "B02-sn-core-schema.sql", "B02-sn-core-seed.sql"):
            done = subprocess.run([*cls.psql, "-d", "smartnode"], input=source("bootstrap/" + name),
                                  text=True, capture_output=True)
            if done.returncode:
                raise RuntimeError(name + ": " + done.stderr[-800:])

    @classmethod
    def stop(cls):
        subprocess.run([*cls.as_owner, cls.bin + "/pg_ctl", "-D", cls.work + "/data", "-m", "fast", "stop"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(cls.work, ignore_errors=True)

    def run_sql(self, sql):
        done = subprocess.run([*self.psql, "-d", "smartnode", "-c", sql], text=True, capture_output=True,
                              env={**os.environ, "PGOPTIONS": "-c search_path=sn_core,sn_config,public"})
        if done.returncode:
            raise RuntimeError(done.stderr + "\n" + sql)
        return done.stdout

    def setUp(self):
        self.run_sql("""
            TRUNCATE sn_core.events, sn_core.messages, sn_config.agent_channels CASCADE;
            DELETE FROM sn_config.agents;
            INSERT INTO sn_config.agents (id, slug, title) VALUES (1, 'hazel', 'Hazel'), (2, 'bruno', 'Bruno'), (3, 'carol', 'Carol');
            INSERT INTO sn_config.agent_channels (agent_id, channel_id, instance_ref)
              SELECT a, (SELECT id FROM sn_config.channels WHERE slug = 'email'), ref
                FROM (VALUES (1, 'bakery@example.test'), (2, 'bakery@example.test'),
                             (3, 'carol@example.test')) AS b(a, ref);
        """)
        outer = self

        class Cursor:
            rows = []
            def execute(self, sql, args=()):
                if sql.startswith(("SET LOCAL", "SAVEPOINT", "RELEASE", "ROLLBACK")):
                    self.rows = []
                    return
                values = iter(args)
                body = re.sub(r"%s", lambda m: _literal(next(values)), sql).strip().rstrip(";")
                if re.match(r"(SELECT|WITH)\b", body):
                    wrapped = "SELECT row_to_json(t) FROM (" + body + ") t"
                elif re.search(r"RETURNING", body):
                    wrapped = "WITH t AS (" + body + ") SELECT row_to_json(t) FROM t"
                else:
                    wrapped = body
                out = outer.run_sql(wrapped)
                self.rows = [json.loads(l) for l in out.splitlines()] if wrapped != body else []
            def fetchone(self):
                return self.rows[0] if self.rows else None
            def fetchall(self):
                return self.rows

        @contextlib.contextmanager
        def cursor(commit=False):
            yield Cursor()

        # The shipped emit: it registers the actor the journal's foreign key needs.
        journal = {}
        exec(compile(source("companions/core-api/events_emit.py"), "events_emit.py", "exec"), journal)

        self.ns = {"HTTPException": HTTPException, "Depends": lambda f: None, "get_principal": lambda: None,
                   "cursor": cursor, "emit": journal["emit"], "_os": os, "_apply_wake_cap": lambda cur, rows: rows}
        load("companions/core-api/lib/channel_routing.py", {"block_ambiguous_inbound"}, self.ns)
        load("companions/core-api/routes/agents.py",
             {"agents_with_pending_work", "WAKE_REMINDERS", "WAKE_RETRY_SECONDS", "_next_wake"}, self.ns)
        obligations = types.ModuleType("routes.messages")
        obligations.sweep_reply_obligations = lambda cur: None
        sys.modules.setdefault("routes", types.ModuleType("routes"))
        sys.modules["routes.messages"] = obligations
        self.addCleanup(sys.modules.pop, "routes.messages", None)

    def mail(self, recipient, external_id):
        out = self.run_sql("INSERT INTO sn_core.messages (channel_type, external_id, direction, sender, recipient, content) "
                           "VALUES ('email', %s, 'inbound', 'buyer@example.test', %s, 'hello') RETURNING id"
                           % (_literal(external_id), _literal(recipient)))
        return int(out.split()[0])

    def test_pending_poll_wakes_neither_receiver_and_keeps_message_unprocessed(self):
        shared = self.mail("bakery@example.test", "x1")
        own = self.mail("carol@example.test", "x2")
        agents = self.ns["agents_with_pending_work"]()["agents"]
        # Carol's own mailbox still wakes her, so an empty answer cannot pass by
        # accident; the shared one is in nobody's list.
        self.assertEqual([(a["slug"], a["due_ids"]) for a in agents], [("carol", [own])])
        state = json.loads(self.run_sql("SELECT row_to_json(s) FROM sn_core.message_state s WHERE message_id = %d" % shared))
        self.assertEqual(state["status"], "blocked")
        self.assertIsNone(state["processed_at"])
        denied = self.run_sql("SELECT count(*) FROM sn_core.events WHERE event_type = 'message.routing_denied'")
        self.assertEqual(denied.strip(), "1")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("now", 0, lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
        self.db.executescript("""
            ATTACH DATABASE ':memory:' AS sn_config;
            ATTACH DATABASE ':memory:' AS sn_core;
            CREATE TABLE sn_config.agents(id INTEGER PRIMARY KEY, slug TEXT, title TEXT, enabled BOOLEAN, archived_at TEXT);
            CREATE TABLE sn_config.channels(id INTEGER PRIMARY KEY, slug TEXT);
            CREATE TABLE sn_config.node_control(id BOOLEAN PRIMARY KEY, node_paused BOOLEAN);
            CREATE TABLE sn_config.agent_channels(agent_id INTEGER, channel_id INTEGER, instance_ref TEXT,
                enabled BOOLEAN, history_before TEXT, PRIMARY KEY(agent_id, channel_id));
            CREATE TABLE sn_core.messages(id INTEGER PRIMARY KEY, channel_type TEXT, recipient TEXT, direction TEXT);
            CREATE TABLE sn_core.message_state(message_id INTEGER PRIMARY KEY, status TEXT, error TEXT, processed_at TEXT);
            CREATE TABLE sn_core.agent_runtime(agent_slug TEXT, status TEXT, lease_until TEXT, updated_at TEXT,
                wake_suspended_until TEXT, budget_suspended_until TEXT, wake_requested_at TEXT);
            CREATE TABLE sn_core.events(event_type TEXT, entity_id TEXT, payload TEXT);
            INSERT INTO sn_config.agents VALUES(1,'hazel','Hazel',true,NULL),(2,'bruno','Bruno',true,NULL);
            INSERT INTO sn_config.channels VALUES(1,'email'),(2,'telegram');
            INSERT INTO sn_config.node_control VALUES(true,false);
            INSERT INTO sn_config.agent_channels VALUES(1,1,'bakery@example.test',true,'2026-01-01T00:00:00+00:00');
        """)
        self.db.commit()
        self.sql = []
        self.model_calls = []
        self.deliveries = []
        outer = self

        class Cursor:
            def __init__(self):
                self.rows = []
            def execute(self, sql, args=()):
                outer.sql.append(sql)
                if sql.startswith("SET LOCAL"):
                    self.rows = []
                    return
                sql = sql.replace(" FOR UPDATE", "").replace("array_agg(DISTINCT a.slug ORDER BY a.slug)",
                                    "json_group_array(DISTINCT a.slug)")
                if "<> ALL(%s::text[])" in sql:
                    sql = sql.replace("m.channel_type <> ALL(%s::text[])", "1=1")
                    args = args[1:]
                sql = sql.replace("bool_or(person)", "max(person)")
                sql = sql.replace("cc.channel_id = ANY (g.allowed_channels)", "cc.channel_id = g.allowed_channels")
                sql = sql.replace("now() - interval '60 seconds'", "datetime('now','-60 seconds')")
                sql = sql.replace("ANY (%s)", "ANY(%s)")
                if "ANY(%s)" in sql:
                    args = list(args)
                    idx = sql[:sql.index("ANY(%s)")].count("%s")
                    items = args.pop(idx)
                    sql = sql.replace("= ANY(%s)", "IN (" + ",".join("%s" for _ in items) + ")", 1)
                    args[idx:idx] = items
                args = tuple(a.isoformat() if isinstance(a, datetime.datetime) else a for a in args)
                cur = outer.db.execute(sql.replace("%s", "?"), args)
                self.rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                for row in self.rows:
                    if row.get("history_before"):
                        row["history_before"] = datetime.datetime.fromisoformat(row["history_before"])
                    if row.get("receivers"):
                        row["receivers"] = sorted(json.loads(row["receivers"]))
            def fetchone(self):
                return self.rows[0] if self.rows else None
            def fetchall(self):
                return self.rows

        @contextlib.contextmanager
        def cursor(commit=False):
            with self.db:
                yield Cursor()

        def emit(cur, **event):
            cur.execute("INSERT INTO sn_core.events VALUES(%s,%s,%s)",
                        (event["event_type"], event["entity_id"], json.dumps(event["payload"])))

        ns = {"HTTPException": HTTPException, "Depends": lambda f: None,
              "get_principal": lambda: None, "cursor": cursor, "emit": emit,
              "_os": types.SimpleNamespace(environ={}), "_apply_wake_cap": lambda cur, rows: rows}
        load("companions/core-api/lib/channel_routing.py",
             {"RECEIVERS_SQL", "channel_receivers", "block_ambiguous_inbound"}, ns)
        load("companions/core-api/routes/agents.py", {"set_agent_channel", "agents_with_pending_work"}, ns)
        # The poll looks for overdue reply obligations before it decides who is
        # woken, and imports that pass by name. It is another test's subject
        # (tests/reply-obligation.test.sh); here it finds nothing to remind.
        obligations = types.ModuleType("routes.messages")
        obligations.sweep_reply_obligations = lambda cur: None
        sys.modules.setdefault("routes", types.ModuleType("routes"))
        sys.modules["routes.messages"] = obligations
        self.addCleanup(sys.modules.pop, "routes.messages", None)
        load("companions/core-api/routes/messages.py", {"resolve_recipient", "_refused_hidden"}, ns)
        ns["_READS_WHOLE_NODE"] = {"super-admin", "operator"}
        self.core = ns

        def query(sql, args=(), fetch=False):
            cur = Cursor()
            cur.execute(sql, args)
            return [tuple(r.values()) for r in cur.rows] if fetch else None

        def state(mid, status, error=None):
            query("UPDATE sn_core.message_state SET status=%s, error=%s WHERE message_id=%s",
                  (status, error, mid))

        def denied(channel, mid, why, actor, **kw):
            emit(Cursor(), event_type="message.routing_denied", entity_id=str(mid),
                 payload={"reason": why, "channel": channel, **kw})

        fast = {"db": types.SimpleNamespace(query=query, emit_routing_denied=denied, emit_turn=lambda *a: None),
            "_set_state": state, "_TAG": "probe", "_TAG_ACTOR": "component:probe", "LOG": lambda *a: None,
            "mailrules": types.SimpleNamespace(is_auto_generated=lambda *a: False),
            "contacts": types.SimpleNamespace(may_wake=lambda *a: (True, "allowed")),
            "_DELIVERERS": {"email": lambda *a: self.deliveries.append(a)},
            "_claim_agent": lambda *a: True, "_release_agent": lambda *a: None,
            "_activity": lambda *a: contextlib.nullcontext(), "uuid": uuid,
            "_lease_kept": lambda *a: contextlib.nullcontext(),
            "_outbound_cursor": lambda: 0, "_committed_tool_reply": lambda *a: None,
            "_ask_brin": lambda *a, **kw: self.model_calls.append(a) or {"final_answer": "reply"},
            "_at_stage": lambda exc, stage: exc}
        # _handle is Msg + _admit + _run_turn since a turn reads every message of one
        # conversation together; _admit also holds a message for a stopped agent.
        load("companions/communication/dispatch.py",
             {"RESOLVE_SQL", "STOPPED_SQL", "AmbiguousReceiver", "_resolve_mailbox", "_handle",
              "Msg", "_admit", "_run_turn", "_turn_text", "_stopped_receiver", "_hold_for_stopped",
              "_BUSY"}, fast)
        self.fast = fast
        self.contacts = {"db": types.SimpleNamespace(query=query),
                         "INTERNAL_CHANNELS": {"internal", "system"},
                         "normalise": lambda channel, value: value,
                         "handles_matching": lambda channel, value: [value],
                         "own_mailboxes": lambda channel: set()}
        load("companions/communication/common/contacts.py", {"_DECIDE_SQL", "may_wake"}, self.contacts)
        # The grant subquery references these tables; an empty catalog is sufficient.
        self.db.executescript("""
            CREATE TABLE sn_config.agent_contacts(agent_id INTEGER, contact_id INTEGER, allowed_channels TEXT);
            CREATE TABLE sn_config.contact_channels(contact_id INTEGER, channel_id INTEGER, enabled BOOLEAN, handle TEXT);
            CREATE TABLE sn_config.contacts(id INTEGER, enabled BOOLEAN, archived_at TEXT);
        """)
        self.db.commit()

    def duplicate(self):
        self.db.execute("INSERT INTO sn_config.agent_channels VALUES(2,1,'bakery@example.test',true,'2026-02-01T00:00:00+00:00')")
        self.db.commit()

    def mail(self):
        self.db.execute("INSERT INTO sn_core.messages VALUES(1,'email','bakery@example.test','inbound')")
        self.db.execute("INSERT INTO sn_core.message_state VALUES(1,'new',NULL,NULL)")
        self.db.commit()

    def bind(self, slug="bruno", enabled=True, channel="email"):
        return self.core["set_agent_channel"](slug, channel,
            types.SimpleNamespace(instance_ref="bakery@example.test", enabled=enabled))

    def events(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM sn_core.events")]

    def message_state(self):
        return dict(self.db.execute("SELECT * FROM sn_core.message_state WHERE message_id=1").fetchone())

    def test_second_receiver_refused_and_holder_named_without_writes(self):
        with self.assertRaises(HTTPException) as error:
            self.bind()
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("Hazel", str(error.exception))
        self.assertEqual(self.db.execute("SELECT count(*) FROM sn_config.agent_channels").fetchone()[0], 1)
        self.assertTrue(any("FOR UPDATE" in q for q in self.sql))

    def test_unchanged_owner_save_preserves_watermark_and_disabling_is_allowed(self):
        saved = self.bind("hazel")
        self.assertEqual(saved["history_before"], "2026-01-01T00:00:00+00:00")
        self.bind(enabled=False)
        self.assertFalse(self.db.execute("SELECT enabled FROM sn_config.agent_channels WHERE agent_id=2").fetchone()[0])

    def test_other_channel_disabled_and_archived_holders_do_not_conflict(self):
        self.bind(channel="telegram")
        self.db.execute("UPDATE sn_config.agents SET enabled=false WHERE id=1")
        self.bind()
        self.db.execute("DELETE FROM sn_config.agent_channels WHERE agent_id=2 AND channel_id=1")
        self.db.execute("UPDATE sn_config.agents SET enabled=true, archived_at='2026-01-01' WHERE id=1")
        self.bind()

    def test_core_resolution_has_no_receiver_and_reports_duplicate_mail_once(self):
        self.duplicate(); self.mail()
        result = self.core["resolve_recipient"]("email", "bakery@example.test")
        self.assertIsNone(result["agent"])
        self.assertEqual(result["reason"], "multiple-receivers")
        self.assertEqual(self.message_state()["status"], "blocked")
        self.assertIsNone(self.message_state()["processed_at"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["event_type"], "message.routing_denied")
        payload = json.loads(self.events()[0]["payload"])
        self.assertEqual(payload["receivers"], ["bruno", "hazel"])
        self.core["resolve_recipient"]("email", "bakery@example.test")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sn_config.agent_channels").fetchone()[0], 2)

    def test_receiver_lookup_is_scoped_to_the_channel(self):
        self.bind(channel="telegram")
        result = self.core["resolve_recipient"]("telegram", "bakery@example.test")
        self.assertEqual(result["agent"], "bruno")
        args = ("bakery@example.test", None)
        if "channel_type" in self.fast["_resolve_mailbox"].__code__.co_varnames:
            args += ("telegram",)
        self.assertEqual(self.fast["_resolve_mailbox"](*args), ("bakery@example.test", "bruno"))

    def test_fast_path_does_not_call_model_deliver_or_consume_duplicate_mail(self):
        self.duplicate(); self.mail()
        self.fast["_handle"](1, "email", "customer@example.test", "bakery@example.test", "order", {}, "thread", "wire")
        self.assertEqual(self.model_calls, [])
        self.assertEqual(self.deliveries, [])
        self.assertEqual(self.message_state()["status"], "blocked")
        self.assertIsNone(self.message_state()["processed_at"])
        self.assertEqual(len(self.events()), 1)

    def test_ambiguous_primary_address_never_falls_back_to_another_box(self):
        self.duplicate()
        self.db.execute("INSERT INTO sn_config.agents VALUES(3,'fallback','Fallback',true,NULL)")
        self.db.execute("INSERT INTO sn_config.agent_channels VALUES(3,1,'fallback@example.test',true,'2026-01-01')")
        args = ("bakery@example.test", "fallback@example.test")
        if "channel_type" in self.fast["_resolve_mailbox"].__code__.co_varnames:
            args += ("email",)
        with self.assertRaises(RuntimeError):
            self.fast["_resolve_mailbox"](*args)

    def test_agents_cannot_read_or_act_on_ambiguity_before_the_poll(self):
        self.duplicate(); self.mail()
        for agent in ["hazel", "bruno"]:
            principal = types.SimpleNamespace(role="agent", username=agent)
            clause, args = self.core["_refused_hidden"](principal, allow_own_outbound=False)
            rows = self.db.execute("SELECT m.id FROM sn_core.messages m JOIN sn_core.message_state ms ON ms.message_id=m.id WHERE " + clause.replace("%s", "?"), args).fetchall()
            self.assertEqual(rows, [])
        owner = types.SimpleNamespace(role="super-admin", username="admin")
        clause, args = self.core["_refused_hidden"](owner)
        self.assertEqual(clause, "TRUE")

    def test_contact_gate_refuses_to_select_one_of_two_receivers(self):
        self.duplicate()
        allowed, reason = self.contacts["may_wake"]("email", "bakery@example.test", "customer@example.test")
        self.assertFalse(allowed)
        self.assertIn("multiple enabled receivers", reason)
        self.assertIn("hazel", reason)
        self.assertIn("bruno", reason)

    def test_even_machine_mail_with_duplicate_receivers_is_not_consumed(self):
        self.duplicate(); self.mail()
        self.fast["mailrules"] = types.SimpleNamespace(is_auto_generated=lambda *a: True)
        self.fast["_handle"](1, "email", "mailer-daemon@example.test", "bakery@example.test", "auto", {}, "thread", "wire")
        self.assertEqual(self.message_state()["status"], "blocked")
        self.assertIsNone(self.message_state()["processed_at"])
        self.assertEqual(self.model_calls, [])
        self.assertEqual(len(self.events()), 1)

    def test_owner_queries_have_no_first_row_tiebreak(self):
        for path, names in [("companions/communication/dispatch.py", {"RESOLVE_SQL"}),
                            ("companions/communication/common/contacts.py", {"_DECIDE_SQL"}),
                            ("companions/communication/common/binding.py", {"_SQL"})]:
            ns = {}; load(path, names, ns)
            for name in names:
                self.assertNotRegex(ns[name], r"(?i)\bLIMIT\s+1\b", path)
        core = source("companions/core-api/routes/messages.py")
        section = core[core.index("def resolve_recipient"):core.index("class MessageStateUpdate")]
        self.assertNotRegex(section, r"(?i)\bLIMIT\s+1\b")


unittest.main(verbosity=2)
