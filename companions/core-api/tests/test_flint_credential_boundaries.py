"""Flint's key must never go through Claude's OAuth or env-file projection."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routes import gateway_secrets


class FlintCredentialBoundaryTest(unittest.TestCase):
    def test_flint_cannot_enter_claude_login_or_projection(self):
        owner = SimpleNamespace(username="owner")
        with patch.object(gateway_secrets, "_require_owner", return_value=owner), \
             patch.object(gateway_secrets, "_runner_op", side_effect=AssertionError("OAuth called")), \
             patch.object(gateway_secrets, "_project_cli_credential",
                          side_effect=AssertionError("projection called")):
            calls = (
                lambda: gateway_secrets.cli_login_start("flint-cli", None),
                lambda: gateway_secrets.cli_login_complete(
                    "flint-cli", gateway_secrets.CliCode(code="test"), None),
                lambda: gateway_secrets.project_cli_credential("flint-cli", None),
            )
            for call in calls:
                with self.subTest(call=call), self.assertRaises(HTTPException) as error:
                    call()
                self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
