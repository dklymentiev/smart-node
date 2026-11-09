"""Flint's model prefix and readiness report agree with the backend factory."""
import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=smartnode user=smartnode")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from backends import resolve_backend  # noqa: E402


class FlintRegistryTest(unittest.TestCase):
    def test_model_prefix_and_provider_key(self):
        backend = resolve_backend("flint-cli/vendor/model")
        self.assertEqual(backend.provider_name, "flint-cli")
        self.assertTrue(backend.is_opaque)

        def query(sql, params, **kwargs):
            if "FROM sn_config.cli_providers WHERE slug" not in sql:
                return []                      # backends.cli_provider_rows: no extra rows
            self.assertEqual(params, ("flint-cli",))
            return [{"enabled": True, "env_vars": {
                "FLINT_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "secret:gateway/flint-key",
            }}]

        import backends
        backends.forget_rows()
        with patch.object(main, "FLINT_ENABLED", True), \
             patch.object(main, "FLINT_PATH", Path("/usr/local/bin/flint")), \
             patch.object(main, "flint_stdio_safe", return_value=True), \
             patch.object(main.shutil, "which", return_value="/usr/local/bin/flint"), \
             patch.object(main.db, "query", side_effect=query), \
             patch.object(main.secrets_store, "resolve_value", return_value="test-key"), \
             patch.object(main.secrets_store, "get_provider_key", return_value=None):
            reported = asyncio.run(main.list_backends())["backends"]
        flint = next(row for row in reported if row["id"] == "flint-cli")
        self.assertEqual(flint["kind"], "cli")
        self.assertEqual(flint["status"], "available")
        self.assertEqual(flint["key_ref"], "gateway/flint-key")


if __name__ == "__main__":
    unittest.main()
