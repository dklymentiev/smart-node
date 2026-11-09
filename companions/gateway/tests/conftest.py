"""Collection hygiene for this directory.

request_usage_regression.py (imported by test_flint_live_turn) replaces
config, db, httpx, secrets_store, budget and psycopg2 in sys.modules with
stubs so it can replay recorded usage without a database, and leaves them
there. Every test module collected after it then imports the stubs instead of
the real modules and fails with "cannot import name ... from 'config'
(unknown location)". A stub has no __file__; drop those before each module
is imported, so each test module starts from the real code.
"""
import sys
import types

import pytest

STUBBED = ("config", "db", "httpx", "secrets_store", "budget", "psycopg2", "psycopg2.extras")


def _drop_stubs():
    for name in STUBBED:
        mod = sys.modules.get(name)
        if isinstance(mod, types.ModuleType) and getattr(mod, "__file__", None) is None:
            del sys.modules[name]


def pytest_collectstart(collector):
    if isinstance(collector, pytest.Module):
        _drop_stubs()
