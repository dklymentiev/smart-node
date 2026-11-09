"""The seed grants the work view to a user with the tool enabled."""

from pathlib import Path
import os
import re
import unittest


ROOT = Path(os.environ.get("SN_TEST_REPO", Path(__file__).resolve().parents[1]))


class WorkReadRoleTest(unittest.TestCase):
    def test_user_role_has_work_read_in_seed(self):
        sql = (ROOT / "bootstrap/B02-sn-core-schema.sql").read_text()
        values = sql.split("INSERT INTO sn_config.role_permissions (role, permission) VALUES", 1)[1]
        values = values.split("ON CONFLICT", 1)[0]
        pairs = set(re.findall(r"\('([^']+)',\s*'([^']+)'\)", values))
        self.assertTrue(("user", "work.read") in pairs,
                        "role user lacks work.read in the seed")

    def test_work_route_still_uses_role_permission(self):
        source = (ROOT / "companions/core-api/routes/work.py").read_text()
        self.assertIn('Depends(requires("work.read"))', source)


if __name__ == "__main__":
    unittest.main()
