"""The sudo-side Flint wrapper must consume secrets before stream-json input."""
import json
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


WRAPPER = Path(__file__).resolve().parents[2] / "gateway-ops" / "claude-agent-wrapper"
CONFIG = Path(__file__).resolve().parents[1] / "config.py"


class FlintWrapperTest(unittest.TestCase):
    def test_safety_probe_requires_version_without_passing_keys(self):
        spec = importlib.util.spec_from_file_location("flint_gateway_config_test", CONFIG)
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        with tempfile.TemporaryDirectory() as tmp:
            def flint(name, version):
                binary = Path(tmp) / name
                binary.write_text(
                    "#!/usr/bin/env python3\n"
                    "import os, sys\n"
                    "if '--version' in sys.argv and not os.getenv('OPENROUTER_API_KEY'):\n"
                    f"    print('flint {version}')\n"
                )
                binary.chmod(0o755)
                return binary
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-secret"}):
                for name, version, safe in (("new", "1.16.0", True), ("newer", "1.17.2", True),
                                            ("old", "1.15.0", False)):
                    with patch.object(config, "FLINT_PATH", flint(name, version)):
                        self.assertEqual(config.flint_stdio_safe(), safe, version)

    def test_private_env_then_prompt_then_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "fake-flint"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "i = sys.argv.index('--system-prompt-file')\n"
                "with open(sys.argv[i + 1]) as f: prompt = f.read()\n"
                "print(json.dumps({'key': os.getenv('OPENROUTER_API_KEY'), "
                "'provider': os.getenv('FLINT_PROVIDER'), "
                "'prompt': prompt, 'user': sys.stdin.readline().strip()}))\n"
            )
            binary.chmod(0o755)
            private = json.dumps({
                "FLINT_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "test-only-secret",
                "SN_AGENT_SLUG": "pebble",
            }).encode()
            prompt = "Role: Pebble\n".encode()
            user = b'{"type":"user","message":{"role":"user","content":"hi"}}\n'
            frame = lambda b: struct.pack("!Q", len(b)) + b
            result = subprocess.run(
                [sys.executable, str(WRAPPER), "--sn-runtime-flint",
                 "--sn-env-stdin", "--sn-system-prompt-stdin"],
                input=frame(private) + frame(prompt) + user,
                env={**os.environ, "FLINT_PATH": str(binary),
                     "SMART_NODE_AGENT_ENV": str(Path(tmp) / "missing-claude-env")},
                capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            out = json.loads(result.stdout)
            self.assertEqual(out["key"], "test-only-secret")
            self.assertEqual(out["provider"], "openrouter")
            self.assertEqual(out["prompt"], "Role: Pebble\n")
            self.assertEqual(out["user"], user.decode().strip())

    def test_passes_fallback_model_to_flint(self):
        # Flint reads its optional fallback model from the environment, and the
        # provider row is where an operator sets it. A name the wrapper does not
        # admit stops every launch on that provider, not only the fallback.
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "fake-flint"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "print(os.getenv('FLINT_FALLBACK_MODEL'))\n"
            )
            binary.chmod(0o755)
            payload = json.dumps({
                "FLINT_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "test-only-secret",
                "FLINT_FALLBACK_MODEL": "fixture/fallback",
            }).encode()
            result = subprocess.run(
                [sys.executable, str(WRAPPER), "--sn-runtime-flint", "--sn-env-stdin"],
                input=struct.pack("!Q", len(payload)) + payload,
                env={**os.environ, "FLINT_PATH": str(binary)},
                capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout.decode().strip(), "fixture/fallback")

    def test_passes_the_gateway_address_to_flint(self):
        # The gateway relays Flint's model calls: the frame carries the door's
        # address in FLINT_BASE_URL and a gateway token in the key's place.
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "fake-flint"
            binary.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "print(os.getenv('FLINT_BASE_URL'), os.getenv('OPENROUTER_API_KEY'))\n"
            )
            binary.chmod(0o755)
            payload = json.dumps({
                "FLINT_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "sn-test-only-token",
                "FLINT_BASE_URL": "http://127.0.0.1:8000/openai/v1",
            }).encode()
            result = subprocess.run(
                [sys.executable, str(WRAPPER), "--sn-runtime-flint", "--sn-env-stdin"],
                input=struct.pack("!Q", len(payload)) + payload,
                env={**os.environ, "FLINT_PATH": str(binary)},
                capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout.decode().split(),
                             ["http://127.0.0.1:8000/openai/v1", "sn-test-only-token"])

    def test_rejects_unapproved_environment_name(self):
        payload = json.dumps({"NODE_OPTIONS": "--require attacker.js"}).encode()
        result = subprocess.run(
            [sys.executable, str(WRAPPER), "--sn-runtime-flint", "--sn-env-stdin"],
            input=struct.pack("!Q", len(payload)) + payload,
            capture_output=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"unsupported variable", result.stderr)
        self.assertNotIn(b"attacker.js", result.stderr)


if __name__ == "__main__":
    unittest.main()
