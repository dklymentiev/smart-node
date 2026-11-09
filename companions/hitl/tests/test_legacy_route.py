"""Legacy router migration preserves access policy and is retry safe."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "legacy_route", Path(__file__).resolve().parents[1] / "legacy_route.py"
)
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)


class LegacyRouteTest(unittest.TestCase):
    def test_rewrite_changes_only_upstream_and_preserves_vpn_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "73-hitl.yml"
            before = ("http:\n  routers:\n    hitl:\n"
                      "      rule: \"Host(`decision.example.com`)\"\n"
                      "      middlewares: [vpn-only@file]\n      tls: {}\n"
                      "  services:\n    hitl:\n      loadBalancer:\n"
                      "        servers:\n          - url: \"http://smart-node-hitl:8080\"\n")
            path.write_text(before)
            self.assertEqual(route.find(Path(temp), "decision.example.com"), path)
            route.rewrite(path, "http://smart-node-hitl:8080",
                          "http://host.docker.internal:8792")
            after = path.read_text()
            self.assertEqual(after, before.replace("http://smart-node-hitl:8080",
                                                   "http://host.docker.internal:8792"))
            route.rewrite(path, "http://smart-node-hitl:8080",
                          "http://host.docker.internal:8792")
            self.assertEqual(path.read_text(), after)

    def test_ambiguous_router_and_unexpected_upstream_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("a.yml", "b.yml"):
                (root / name).write_text("Host(`decision.example.com`)\nurl: http://elsewhere:8080\n")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                route.find(root, "decision.example.com")
            with self.assertRaisesRegex(ValueError, "unexpected upstream"):
                route.rewrite(root / "a.yml", "http://smart-node-hitl:8080",
                              "http://host.docker.internal:8792")


if __name__ == "__main__":
    unittest.main()
