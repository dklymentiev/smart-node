"""/health on a node without the memory module answers at once.

It probed a default mesh name that resolves nowhere and waited out its 5 s
timeout on every call, longer than the installer's 4 s check, so a fresh
minimal node failed "dispatcher answers /health" (2026-10-30).
"""
import asyncio
import os
import time
import unittest

os.environ.pop("MESH_URL", None)
os.environ.setdefault("AI_GATEWAY_URL", "http://127.0.0.1:1")

from core import health  # noqa: E402


class HealthWithoutMemoryTest(unittest.TestCase):
    def test_answers_without_asking_an_absent_memory(self):
        t = time.time()
        h = asyncio.run(health.get_health(0, 0))
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(h["dependencies"]["mesh"], {"ok": True, "installed": False})


if __name__ == "__main__":
    unittest.main()
