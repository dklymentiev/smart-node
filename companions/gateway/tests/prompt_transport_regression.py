"""Exercise CLI spawns without putting the system prompt in argv or env.

Run directly with Python's standard library. --baseline loads the production
modules from a git revision, keeping these behavioral assertions unchanged.
The fake CLI reads its prompt and one stream-json message in a real process.
"""

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser()
parser.add_argument("--baseline")
args, test_args = parser.parse_known_args()
baseline = tempfile.TemporaryDirectory(prefix="prompt-baseline-")
gateway = ROOT / "companions/gateway"
wrapper = ROOT / "companions/gateway-ops/claude-agent-wrapper"
if args.baseline:
    gateway = Path(baseline.name) / "gateway"
    for relative in ("backends/__init__.py", "backends/cli_claude.py",
                     "session_manager.py", "cli_prompt.py"):
        result = subprocess.run(
            ["git", "show", f"{args.baseline}:companions/gateway/{relative}"],
            cwd=ROOT, capture_output=True,
        )
        if result.returncode == 0:
            target = gateway / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result.stdout)
    wrapper = Path(baseline.name) / "wrapper"
    wrapper.write_bytes(subprocess.check_output(
        ["git", "show", f"{args.baseline}:companions/gateway-ops/claude-agent-wrapper"],
        cwd=ROOT,
    ))
    wrapper.chmod(0o755)

# The installer makes this source script executable; tests use their own copy.
if not args.baseline:
    copied_wrapper = Path(baseline.name) / "wrapper"
    copied_wrapper.write_bytes(wrapper.read_bytes())
    copied_wrapper.chmod(0o755)
    wrapper = copied_wrapper


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    sys.modules[name] = module


stub("db")
stub("secrets_store")
stub("config", CLI_PATH="unused", FLINT_PATH="unused", FLINT_ENABLED=False,
     flint_stdio_safe=lambda: False,
     FLINT_SKIP_PERMISSIONS=False, FLINT_SKIP_PERMISSIONS_USERS=frozenset(),
     spawn_env=lambda: {}, CLI_DEFAULT_CWD="",
     SKIP_PERMISSIONS=False, CLI_RUNAS_ENABLED=True, SUDO_PATH="unused",
     KILL_PATH="unused", STREAM_LIMIT=1024 * 1024, WRAPPER_PATH=str(wrapper),
     HOST="127.0.0.1", PORT=8000, DOOR_URL="http://127.0.0.1:8000",
     RUNTIME_URL="http://127.0.0.1:8002", MODEL_RELAY=False,
     CLI_SESSION_TIMEOUT=3600, CLI_MAX_SESSIONS=10, CLI_QUEUE_TIMEOUT=1)
sys.path.insert(0, str(gateway))
cli = importlib.import_module("backends.cli_claude")
manager_module = importlib.import_module("session_manager")
manager_module.node_turn_bound_seconds = lambda: 600.0  # no registry DB in CLI fixture

PROMPT = "Private prompt sentinel\nnon-ASCII café\r\nquotes ' \" \\\n" + "large\n" * 20000
MESSAGE = {"type": "user", "message": {"role": "user", "content": "first turn"}}

FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, stat, sys
args = sys.argv[1:]
prompt = None
private_file = None
if "--system-prompt-file" in args:
    path = args[args.index("--system-prompt-file") + 1]
    with open(path, encoding="utf-8", newline="") as f:
        prompt = f.read()
        info = os.fstat(f.fileno())
        private_file = (stat.S_IMODE(info.st_mode) == 0o600 and
                        info.st_uid == os.geteuid() and
                        "memfd:" in os.readlink(path))
elif "--system-prompt" in args:
    prompt = args[args.index("--system-prompt") + 1]
message = json.loads(sys.stdin.readline())
print(json.dumps({"prompt": prompt, "message": message, "argv": args,
                  "env": dict(os.environ), "private_file": private_file}), flush=True)
'''

# A sudo boundary that captures exactly what sudo could log, closes passed
# descriptors, then execs the real source wrapper. It does not need root.
FAKE_SUDO = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["AUDIT_FILE"], "w") as f:
    json.dump({"argv": args, "env": dict(os.environ)}, f)
for fd in range(3, 256):
    try: os.close(fd)
    except OSError: pass
index = args.index("--") + 1
os.execv(args[index], args[index:])
'''


class PromptTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="prompt-transport-")
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name)
        self.fake_cli = self.folder / "cli"
        self.fake_cli.write_text(FAKE_CLI)
        self.fake_cli.chmod(0o755)
        self.fake_sudo = self.folder / "sudo"
        self.fake_sudo.write_text(FAKE_SUDO)
        self.fake_sudo.chmod(0o755)
        cred = self.folder / "credential.env"
        cred.write_text("ANTHROPIC_API_KEY=fixture-placeholder\n")
        self.env = {
            "PATH": os.environ["PATH"], "CLI_PATH": str(self.fake_cli),
            "SMART_NODE_AGENT_ENV": str(cred),
            "AUDIT_FILE": str(self.folder / "audit.json"),
        }
        self.calls = []
        self.processes = []
        self.real_spawn = asyncio.create_subprocess_exec

    async def spawn(self, *cmd, **kwargs):
        # Inspect the actual argv and environment at the subprocess boundary.
        self.calls.append((cmd, kwargs))
        self.assertNotIn("Private prompt sentinel", json.dumps(cmd))
        self.assertNotIn("Private prompt sentinel", json.dumps(kwargs.get("env")))
        process = await self.real_spawn(*cmd, **kwargs)
        self.processes.append(process)
        return process

    async def exchange(self, process, prompt):
        try:
            output, error = await asyncio.wait_for(process.communicate(
                (json.dumps(MESSAGE) + "\n").encode(),
            ), timeout=10)
            self.assertEqual(process.returncode, 0, error.decode())
            result = json.loads(output)
            self.assertEqual(result["prompt"], prompt)
            self.assertEqual(result["message"], MESSAGE)
            if prompt:
                self.assertTrue(result["private_file"])
            self.assertNotIn("Private prompt sentinel", json.dumps(result["argv"]))
            self.assertNotIn("Private prompt sentinel", json.dumps(result["env"]))
            return result
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_agent_spawn_keeps_payload_out_of_sudo_audit(self):
        with patch.object(cli, "CLI_PATH", str(self.fake_cli)), \
             patch.object(cli, "SUDO_PATH", str(self.fake_sudo)), \
             patch.object(cli, "_safe_env", return_value=self.env), \
             patch.object(asyncio, "create_subprocess_exec", side_effect=self.spawn):
            process = await cli._spawn("test", PROMPT, str(self.folder),
                                       os_user="agent-example")
        await self.exchange(process, PROMPT)
        audit = json.loads((self.folder / "audit.json").read_text())
        self.assertNotIn("Private prompt sentinel", json.dumps(audit))

    async def test_flint_spawn_keeps_provider_key_out_of_sudo_audit(self):
        sentinel = "test-only-flint-provider-key"
        audit_file = self.folder / "flint-audit.json"
        self.fake_sudo.write_text(FAKE_SUDO.replace(
            'os.environ["AUDIT_FILE"]', repr(str(audit_file))))
        safe_env = {"PATH": os.environ["PATH"], "HOME": str(self.folder)}
        with patch.object(cli, "FLINT_PATH", str(self.fake_cli)), \
             patch.object(cli, "SUDO_PATH", str(self.fake_sudo)), \
             patch.object(cli, "_safe_env", side_effect=lambda: safe_env.copy()), \
             patch.object(asyncio, "create_subprocess_exec", side_effect=self.spawn):
            process = await cli._spawn(
                "vendor/model", PROMPT, str(self.folder),
                extra_env={"FLINT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": sentinel},
                os_user="agent-example", runtime="flint-cli",
            )
        result = await self.exchange(process, PROMPT)
        self.assertEqual(result["env"]["OPENROUTER_API_KEY"], sentinel)
        audit = audit_file.read_text()
        self.assertNotIn(sentinel, audit)
        self.assertNotIn("Private prompt sentinel", audit)

    async def test_flint_permission_bypass_is_limited_to_named_os_users(self):
        self.fake_sudo.write_text(FAKE_SUDO.replace(
            'os.environ["AUDIT_FILE"]', repr(str(self.folder / "audit.json"))))
        safe_env = {"PATH": os.environ["PATH"], "HOME": str(self.folder)}
        with patch.object(cli, "FLINT_PATH", str(self.fake_cli)), \
             patch.object(cli, "SUDO_PATH", str(self.fake_sudo)), \
             patch.object(cli, "_safe_env", side_effect=lambda: safe_env.copy()), \
             patch.object(cli, "FLINT_SKIP_PERMISSIONS_USERS",
                          frozenset({"agent-fixture-a", "agent-fixture-b"})):
            for user in ("agent-fixture-a", "agent-fixture-b", "agent-flint"):
                process = await cli._spawn("vendor/model", None, str(self.folder),
                                           os_user=user, runtime="flint-cli")
                result = await self.exchange(process, None)
                self.assertEqual("--dangerously-skip-permissions" in result["argv"],
                                 user != "agent-flint")

    async def test_direct_spawn_keeps_payload_out_of_process_arguments(self):
        with patch.object(cli, "CLI_PATH", str(self.fake_cli)), \
             patch.object(cli, "_safe_env", return_value=self.env), \
             patch.object(asyncio, "create_subprocess_exec", side_effect=self.spawn):
            process = await cli._spawn("test", PROMPT, str(self.folder))
        await self.exchange(process, PROMPT)
        # The gateway must close its copy after spawning, including on errors.
        for fd in self.calls[0][1].get("pass_fds", ()):
            with self.assertRaises(OSError):
                os.fstat(fd)

    async def test_legacy_create_and_resume_preserve_prompt_and_stream(self):
        with patch.object(manager_module, "CLI_PATH", str(self.fake_cli)), \
             patch.object(manager_module, "spawn_env", return_value=self.env), \
             patch.object(manager_module.SessionManager, "_read_stderr", new=AsyncMock()), \
             patch.object(asyncio, "create_subprocess_exec", side_effect=self.spawn):
            manager = manager_module.SessionManager()
            session = await manager.create_session(system_prompt=PROMPT,
                                                    cwd=str(self.folder))
            await self.exchange(session.process, PROMPT)
            await manager._respawn_session(session, resume=True)
            result = await self.exchange(session.process, PROMPT)
        self.assertIn("--resume", result["argv"])
        self.assertIn(session.session_id, result["argv"])

    async def test_no_prompt_needs_no_frame(self):
        for user in (None, "agent-example"):
            with patch.object(cli, "CLI_PATH", str(self.fake_cli)), \
                 patch.object(cli, "SUDO_PATH", str(self.fake_sudo)), \
                 patch.object(cli, "_safe_env", return_value=self.env):
                process = await cli._spawn("test", None, str(self.folder), os_user=user)
            await self.exchange(process, None)

    async def test_wrapper_consumes_only_frame_when_first_message_is_already_queued(self):
        process = await self.real_spawn(
            str(wrapper), "--sn-system-prompt-stdin", "--print",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env, limit=1024 * 1024,
        )
        payload = PROMPT.encode()
        output, error = await process.communicate(
            struct.pack("!Q", len(payload)) + payload +
            (json.dumps(MESSAGE) + "\n").encode(),
        )
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(json.loads(output)["prompt"], PROMPT)
        self.assertEqual(json.loads(output)["message"], MESSAGE)

    async def test_spawn_failure_closes_prompt_descriptor(self):
        with patch.object(cli, "CLI_PATH", str(self.fake_cli)), \
             patch.object(cli, "_safe_env", return_value=self.env):
            captured = []

            async def fail(*_cmd, **kwargs):
                captured.extend(kwargs.get("pass_fds", ()))
                raise FileNotFoundError("missing CLI")

            with patch.object(asyncio, "create_subprocess_exec", side_effect=fail):
                with self.assertRaises(FileNotFoundError):
                    await cli._spawn("test", PROMPT, str(self.folder))
            self.assertTrue(captured)
            for fd in captured:
                with self.assertRaises(OSError):
                    os.fstat(fd)

    async def test_wrapper_rejects_truncated_prompt_without_echoing_it(self):
        for data in (b"short", struct.pack("!Q", 9999) + b"private-payload"):
            process = await self.real_spawn(
                str(wrapper), "--sn-system-prompt-stdin",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=self.env,
            )
            output, error = await asyncio.wait_for(process.communicate(data), timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"incomplete system prompt frame", error)
            self.assertNotIn(b"private-payload", output + error)

    async def test_cancelled_prompt_transfer_leaves_no_child(self):
        started = asyncio.Event()

        async def blocked_transfer(_process, _prompt):
            started.set()
            await asyncio.Event().wait()

        async def kill_group(pid, _sig, _user):
            os.killpg(pid, signal.SIGKILL)

        with patch.object(cli, "CLI_PATH", str(self.fake_cli)), \
             patch.object(cli, "SUDO_PATH", str(self.fake_sudo)), \
             patch.object(cli, "_safe_env", return_value=self.env), \
             patch.object(cli, "write_prompt_frame", side_effect=blocked_transfer), \
             patch.object(cli, "_signal_group", side_effect=kill_group), \
             patch.object(asyncio, "create_subprocess_exec", side_effect=self.spawn):
            task = asyncio.create_task(cli._spawn(
                "test", PROMPT, str(self.folder), os_user="agent-example",
            ))
            await asyncio.wait_for(started.wait(), timeout=10)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(self.processes[0].returncode)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + test_args)
