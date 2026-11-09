"""Import-time configuration for the admin-runner test run.

`config.py` reads its required settings at import and raises when they are
absent -- correct for a service that must never start half-configured, and it
means the module cannot be imported by a test without them. These are the
values a test needs to get past that, and nothing depends on them: no test here
authenticates a caller or opens a database.

Set before any test module imports `operations`, which imports `config`.
SN_ADMIN_OWNER_ROLES is no longer required under the role-permission model
but is harmless if the install still sets it.
"""
import os

os.environ.setdefault("SN_ADMIN_DB_USER", "smartnode")
