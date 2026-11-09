"""Behavioral create/rollback regression; optional --baseline REF must fail."""
import ast
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

ROOT = Path(sys.argv[1])
REF = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--baseline" else None
sys.argv = sys.argv[:1]


def source(path):
    if REF:
        return subprocess.check_output(["git", "-C", str(ROOT), "show", f"{REF}:{path}"], text=True)
    return (ROOT / path).read_text()


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code


class CheckViolation(Exception):
    pass


def load_function(path, name, ns):
    node = next(n for n in ast.parse(source(path)).body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"), ns)
    return ns[name]


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        root = Path(self.scratch.name)
        self.agents = root / "agents"
        self.agents.mkdir()
        template = root / "template"
        template.mkdir()
        (template / "CLAUDE.md").write_text("You are {{AGENT_NAME}}.\n")
        (template / "README.md").write_text("{{AGENT_NAME}}\n")
        env = patch.dict(os.environ, SN_AGENTS_ROOT=str(self.agents), SN_AGENT_TEMPLATE=str(template))
        env.start()
        self.addCleanup(env.stop)
        # JSON is a valid YAML encoding: only safe_dump is needed by provisioning.
        yaml = types.SimpleNamespace(safe_dump=lambda value, **kw: json.dumps(value))
        fs = types.ModuleType("test_agent_fs")
        with patch.dict(sys.modules, yaml=yaml):
            exec(compile(source("companions/core-api/lib/agent_fs.py"), "agent_fs.py", "exec"), fs.__dict__)
        self.fs = fs
        self.state = {}
        self.calls = []
        self.failure = None
        self.privileged = {"ok": True, "result": {"ensured": 1, "failed": 0,
                            "agents": [{"slug": "probe", "os_user": "agent-probe"}]}}
        self.cur = None
        owner = self

        class Cursor:
            def __init__(self):
                self.rows = []
                self.pending = copy.deepcopy(owner.state)
            def execute(self, sql, args=None):
                sql = " ".join(sql.split())
                if sql.startswith("SELECT avatar FROM sn_config.agents"):
                    self.rows = []
                elif sql.startswith("UPDATE sn_config.agents SET avatar"):
                    self.rows = []
                elif sql.startswith("INSERT INTO sn_config.agent_tools"):
                    self.rows = []
                elif sql.startswith("SELECT id FROM sn_config.tenants"):
                    self.rows = [{"id": 1}]
                elif sql.startswith("SELECT 1 FROM sn_config.users"):
                    # No account holds the new agent's name.
                    self.rows = []
                elif sql.startswith("INSERT INTO sn_config.specialists"):
                    self.pending["specialist"] = args[0]
                    self.rows = [{"id": 4, "body_md": args[2]}]
                elif sql.startswith("INSERT INTO sn_config.agents"):
                    if owner.failure == "insert":
                        raise RuntimeError("database refused insert")
                    if "agent" in self.pending:
                        raise RuntimeError("duplicate agent row")
                    self.pending["agent"] = args[2]
                    self.rows = [{"id": 7}]
                elif "WHERE a.id=" in sql:
                    self.rows = [{"id": 7, "slug": "probe"}]
                elif "FROM sn_core.crons" in sql:
                    self.rows = []
                elif "FROM sn_config.agents" in sql:
                    # The old timer's roster must contain an agent to expose its call.
                    self.rows = [{"slug": "probe", "os_user": "agent-probe"}]
                else:
                    raise AssertionError("unexpected SQL: " + sql)
            def fetchone(self):
                return self.rows[0] if self.rows else None
            def fetchall(self):
                return self.rows

        @contextmanager
        def cursor(commit=False):
            cur = Cursor()
            self.cur = cur
            try:
                yield cur
                if commit:
                    if self.failure == "commit":
                        raise RuntimeError("commit refused")
                    self.state = cur.pending
                    self.calls.append("commit")
            except Exception:
                self.calls.append("rollback")
                raise

        def identity(cur, *args):
            cur.pending["identity"] = args[0]

        def compose(cur, slug):
            # Simulate another create-time DB writer: rollback must cover bindings too.
            cur.pending["binding"] = slug
            if self.failure == "compose":
                raise RuntimeError("prompt rendering refused")
            fs.rewrite_claude(slug, "Ready\n")

        def emit(cur, **kw):
            if self.failure == "emit":
                raise RuntimeError("event refused")
            cur.pending["event"] = kw["event_type"]

        def runner(actor, op, args):
            self.calls.append((actor, op, args))
            # Independent readers cannot route to a half-provisioned row.
            self.assertNotIn("agent", self.state)
            self.assertNotIn("identity", self.state)
            self.assertNotIn("event", self.cur.pending, "emit must not lock the runner audit actor")
            if isinstance(self.privileged, Exception):
                raise self.privileged
            return self.privileged

        def row_to_read(row):
            if self.failure == "response":
                raise RuntimeError("response refused")
            return types.SimpleNamespace(model_dump=lambda **kw: dict(row))

        self.ns = {"AgentCreate": object, "HTTPException": HTTPException,
            "Depends": lambda value: None, "get_actor": lambda: None,
            "get_provisioning_actor": lambda: None,
            "NAME_RE": re.compile(r"^[a-z][a-z0-9-]{1,40}$"), "cursor": cursor,
            "agent_fs": fs, "AGENT_COLS": "a.id, a.slug", "runner_op": runner,
            "_canonical_model": lambda cur, model, *args, **kw: model,
            "_perms_from_payload": lambda p: {"os_user": p.os_user},
            "_ensure_agent_identity": identity, "_compose_and_write_claude": compose,
            "emit": emit, "_row_to_read": row_to_read,
            "_register_planner_actor": lambda *args: self.calls.append("planner"),
            "psycopg2": types.SimpleNamespace(errors=types.SimpleNamespace(CheckViolation=CheckViolation)),
            "_is_model_refusal": lambda exc: False,
            "log": types.SimpleNamespace(error=lambda *args: None),
            "AVATARS_DIR": str(root / "no-avatars"), "_os": os}
        grants = types.ModuleType("default_grants")
        exec(compile(source("companions/core-api/lib/default_grants.py"), "default_grants.py", "exec"), grants.__dict__)
        self.ns["default_grants"] = grants
        path = "companions/core-api/routes/agents.py"
        load_function(path, "_next_avatar", self.ns)
        load_function(path, "_resolve_create_tenant", self.ns)
        if "_recompose_roster" in source(path):
            load_function(path, "_recompose_roster", self.ns)
        self.create = load_function(path, "create_agent", self.ns)
        self.payload = types.SimpleNamespace(slug="probe", title="Probe", role=None,
            description=None, model="claude-cli/sonnet", enabled=True, capability="",
            os_user=None, schedule=None, os_groups=[], mesh_workspace=None,
            fs_read=[], fs_write=[], forbidden_fs=[], forbidden_behavior=[],
            specialist_slug=None, specialist_body="You are Probe", tenant_id=None, department_id=None)

    def invoke(self):
        # Baseline has no actor argument; execute it too to prove the bug.
        kw = {"created_by": "forged-audit-name"}
        if "actor" in self.create.__code__.co_varnames:
            kw["actor"] = "actual-owner"
        return self.create(self.payload, **kw)

    def assert_clean(self):
        self.assertEqual(self.state, {})
        self.assertFalse((self.agents / "probe").exists())
        self.assertFalse((self.agents / ".archive").exists())
        self.assertNotIn("planner", self.calls)

    def test_user_ready_before_commit_and_response(self):
        result = self.invoke()
        self.assertEqual(result["slug"], "probe")
        self.assertEqual(self.calls, [("actual-owner", "ensure_agent_users", {
            "agents": [{"slug": "probe", "os_user": "agent-probe"}]}), "commit", "planner"])
        self.assertEqual(self.state["agent"], "probe")
        self.assertEqual(self.state["identity"], "probe")
        self.assertTrue((self.agents / "probe/CLAUDE.md").is_file())

    def test_legacy_alias_event_is_deferred_until_after_runner(self):
        self.payload.model = "sonnet"
        def canonical(cur, model, *args, emit_alias=True):
            if emit_alias:
                cur.pending["event"] = "agent.model_legacy_alias"
            return "claude-cli/sonnet"
        self.ns["_canonical_model"] = canonical
        self.invoke()
        self.assertEqual(self.calls[0][1], "ensure_agent_users")
        self.assertEqual(self.state["event"], "agent.created")

    def test_runner_unreachable_rolls_back_every_create_artifact(self):
        self.privileged = HTTPException(502, "admin-runner unreachable")
        with self.assertRaises(HTTPException):
            self.invoke()
        self.assert_clean()

    def test_http_success_with_agent_error_is_failure(self):
        self.privileged = {"ok": True, "result": {"ensured": 0, "failed": 1,
                            "agents": [], "errors": [{"error": "useradd failed"}]}}
        with self.assertRaises(HTTPException):
            self.invoke()
        self.assert_clean()

    def test_unconfirmed_or_wrong_identity_results_are_failures(self):
        for result in [{}, {"ok": False}, {"ok": True, "result": {}},
                       {"ok": True, "result": {"ensured": 1, "failed": 0, "agents": []}},
                       {"ok": True, "result": {"ensured": 1, "failed": 0, "agents": [
                           {"slug": "other", "os_user": "agent-other"}]}}]:
            with self.subTest(result=result):
                self.privileged = result
                with self.assertRaises(HTTPException):
                    self.invoke()
                self.assert_clean()

    def test_errors_after_directory_creation_also_remove_it(self):
        for phase in ["insert", "compose", "emit", "response", "commit"]:
            with self.subTest(phase=phase):
                self.failure = phase
                with self.assertRaises(Exception):
                    self.invoke()
                self.assert_clean()

    def test_existing_folder_is_preserved_on_conflict(self):
        d = self.agents / "probe"
        d.mkdir()
        (d / "keep").write_text("existing agent data")
        with self.assertRaises(HTTPException) as error:
            self.invoke()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual((d / "keep").read_text(), "existing agent data")
        self.assertEqual(self.state, {})
        self.assertNotIn("planner", self.calls)

    def test_duplicate_db_row_keeps_existing_row_and_removes_new_folder(self):
        self.state = {"agent": "probe"}
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertEqual(self.state, {"agent": "probe"})
        self.assertFalse((self.agents / "probe").exists())
        self.assertFalse((self.agents / ".archive").exists())

    def test_partial_directory_render_is_removed(self):
        with patch.object(self.fs, "_write_config_files", side_effect=OSError("disk full")):
            with self.assertRaises(HTTPException):
                self.invoke()
        self.assert_clean()

    def test_cron_render_only_materializes_crons(self):
        calls = []
        ns = dict(self.ns, Principal=object, get_principal=lambda: None,
                  render_crontab=lambda rows: "", os=os, Path=Path,
                  runner_op=lambda actor, op, args: calls.append(op) or {"result": {"unchanged": True}})
        render = load_function("companions/core-api/routes/crons.py", "render_node_crontab", ns)
        render(types.SimpleNamespace(cross_tenant=True, username="owner"))
        self.assertEqual(calls, ["materialize_crons"])


unittest.main(verbosity=2)
