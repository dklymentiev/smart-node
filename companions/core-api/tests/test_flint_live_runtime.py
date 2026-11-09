"""The live runtime view counts Flint as a CLI and shows Gateway readiness."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from routes import runtimes


class _Cursor:
    def __init__(self):
        self.calls = 0

    def execute(self, *_args):
        self.calls += 1

    def fetchall(self):
        if self.calls == 1:
            return [{"prefix": "flint-cli", "n": 1, "slugs": "pebble"}]
        return [{"provider": "flint-cli", "n": 2}]


@contextmanager
def _cursor():
    yield _Cursor()


class FlintLiveRuntimeTest(unittest.TestCase):
    def test_flint_is_available_and_not_counted_as_brin(self):
        with patch.object(runtimes, "_gateway_health", return_value={"status": "ok"}), \
             patch.object(runtimes, "_gateway_cli_backends", return_value={
                 "flint-cli": {"id": "flint-cli", "status": "available"},
                 "claude-cli": {"id": "claude-cli", "status": "available"},
             }), \
             patch.object(runtimes, "_cli_version", return_value=("/usr/local/bin/flint", "flint 1.14.6")), \
             patch.object(runtimes, "cursor", side_effect=_cursor), \
             patch.object(runtimes, "_read_host_data", return_value={
                 "companions": [{"name": "dispatcher", "version": "test"}],
                 "systemd": [{"name": "smart-node-dispatcher.service", "health": "ok", "status": "running"}],
             }):
            rows = runtimes.live_runtimes()["runtimes"]
        flint = next(r for r in rows if r["slug"] == "flint")
        brin = next(r for r in rows if r["slug"] == "brin")
        self.assertEqual(flint["live_status"], "available")
        self.assertEqual(flint["agent_slugs"], ["pebble"])
        self.assertEqual(flint["sessions_7d"], 2)
        self.assertEqual(brin["agents"], 0)
        self.assertEqual(brin["sessions_7d"], 0)


if __name__ == "__main__":
    unittest.main()
