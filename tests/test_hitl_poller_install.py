import importlib.util
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "companions/hitl/install-poller.py"
SPEC = importlib.util.spec_from_file_location("install_hitl_poller", SCRIPT)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class PollerInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.flow = self.root / "flow"
        self.source = self.root / "source"
        self.dest = self.root / "install"
        self.config = self.root / "etc/content-article-hitl-poller.env"
        self.crontab = self.root / "cron/smart-node-agents"
        self.dest.mkdir()
        self.source.mkdir()
        (self.flow / "ops").mkdir(parents=True)
        (self.flow / "content-article-hitl.yaml").write_text("flow: test\n")
        (self.source / "flow_poller.py").write_text("poller source\n")
        (self.source / "flow_poller.sh").write_text("#!/bin/sh\n")
        self.prerequisites = [
            self.root / "planner-key-runner.env",
            self.root / "caller/hitl.py",
            self.root / "cron-runner.sh",
        ]
        for path in self.prerequisites:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("present\n")
        self.patches = [
            patch.object(INSTALLER, "FLOW", self.flow),
            patch.object(INSTALLER, "DEST", self.dest),
            patch.object(INSTALLER, "SOURCE", self.source),
            patch.object(INSTALLER, "CONFIG", self.config),
            patch.object(INSTALLER, "CRONTAB", self.crontab),
            patch.object(INSTALLER, "COMMAND", str(self.dest / "flow_poller.sh")),
            patch.object(INSTALLER, "PREREQUISITES", self.prerequisites),
            patch.object(INSTALLER, "api"),
            patch.object(INSTALLER.os, "geteuid", return_value=0),
            patch.object(INSTALLER.os, "chown"),
        ]
        self.mocks = [item.start() for item in self.patches]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_missing_flow_has_no_side_effects(self):
        self.flow.rename(self.root / "not-installed")
        INSTALLER.install()
        self.mocks[7].assert_not_called()
        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_existing_configuration_and_cron_are_preserved(self):
        self.config.parent.mkdir(parents=True)
        original = b"local configuration\n"
        self.config.write_bytes(original)
        self.crontab.parent.mkdir(parents=True)
        self.crontab.write_text("* * * * * root cron-runner.sh content-article-hitl-poll "
                               f"{self.dest / 'flow_poller.sh'}\n")
        api = self.mocks[7]
        api.return_value = {"crons": [{"slug": INSTALLER.SLUG,
                                        "command": INSTALLER.COMMAND,
                                        "enabled": True}]}

        INSTALLER.install()

        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.crontab.read_text().count("content-article-hitl-poll"), 1)
        self.assertEqual((self.dest / "flow_poller.py").read_text(), "poller source\n")
        self.assertEqual([call.args[:2] for call in api.call_args_list], [("GET", "/api/crons")])

    def test_hitl_before_workflows_installs_on_first_attempt(self):
        api = self.mocks[7]
        api.side_effect = [
            {"crons": []},
            {"id": 1},
            {"ok": True},
        ]
        self.crontab.parent.mkdir(parents=True)
        self.crontab.write_text(f"cron-runner.sh {INSTALLER.SLUG} {INSTALLER.COMMAND}\n")

        INSTALLER.install()

        config = self.config.read_text()
        self.assertIn(f"CONTENT_ARTICLE_HITL_FLOW_DIR={shlex.quote(str(self.flow))}", config)
        self.assertTrue((self.dest / "flow_poller.py").is_file())
        self.assertTrue((self.dest / "flow_poller.sh").is_file())
        self.assertEqual([call.args[:2] for call in api.call_args_list], [
            ("GET", "/api/crons"), ("POST", "/api/crons"), ("POST", "/api/crons/render")])

    def test_workflows_before_hitl_defers_then_hitl_retry_installs(self):
        # The workflows stage runs before native HITL on a clean installation.
        self.prerequisites[1].unlink()
        api = self.mocks[7]
        INSTALLER.install()
        api.assert_not_called()
        self.assertFalse(self.config.exists())

        # hitl.sh retries the same helper after creating the caller.
        self.prerequisites[1].write_text("native caller\n")
        api.side_effect = [{"crons": []}, {"id": 1}, {"ok": True}]
        self.crontab.parent.mkdir(parents=True)
        self.crontab.write_text(f"cron-runner.sh {INSTALLER.SLUG} {INSTALLER.COMMAND}\n")
        INSTALLER.install()

        self.assertTrue(self.config.is_file())
        self.assertTrue((self.dest / "flow_poller.py").is_file())
        self.assertEqual([call.args[:2] for call in api.call_args_list], [
            ("GET", "/api/crons"), ("POST", "/api/crons"), ("POST", "/api/crons/render")])

if __name__ == "__main__":
    unittest.main()
