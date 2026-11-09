"""Budget enforcement tests -- gateway/budget.py against a live smartnode database.

Why these exist: sn_config.budgets and budget.py shipped together, metered correctly for
weeks, and still enforced nothing, because the only row on the node carried
limit_usd = 100000 with hard_limit = false. A meter with no brake looks identical to a
working brake in every dashboard. These tests assert the brake, not the meter.

Isolation: every behavioural test runs against a synthetic tenant id that owns no real
sessions. record_spend() updates EVERY applicable budget row in one statement, so testing
it against tenant 1 would corrupt the production counter the owner's limits are read from.
test_record_spend_leaves_other_scopes_alone asserts that isolation explicitly.

Runs two ways:
    pytest tests/test_budget.py          (tests/run-all.sh, once pytest is installed)
    ./venv/bin/python tests/test_budget.py

Both require SMARTNODE_DB_DSN and a postgres role that can write sn_config.budgets, i.e.
run it as the gateway service user.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget  # noqa: E402
import db      # noqa: E402

# No real session carries this tenant, so no production budget row is applicable to it.
FAKE_TENANT = 999000001

# The owner's policy of 2026-08-31. Any change to
# these numbers is a policy decision and must be made here deliberately, not silently in
# the database.
OWNER_POLICY = {
    "monthly": {"limit_usd": 400, "hard_limit": True},
    "daily": {"limit_usd": 75, "hard_limit": True},
}


# ---------------------------------------------------------------- fixtures

def _seed(period, limit_usd, hard_limit, spend=0, reset_at=None):
    """Create one isolated budget row and return its id."""
    if reset_at is None:
        reset_at = datetime.now(timezone.utc) + timedelta(days=1)
    rows = db.query(
        """INSERT INTO sn_config.budgets
               (scope_type, scope_id, period, limit_usd, current_spend_usd,
                period_reset_at, hard_limit, enabled)
           VALUES ('tenant', %s, %s, %s, %s, %s, %s, true)
           ON CONFLICT (scope_type, scope_id, period) DO UPDATE
              SET limit_usd = EXCLUDED.limit_usd,
                  current_spend_usd = EXCLUDED.current_spend_usd,
                  period_reset_at = EXCLUDED.period_reset_at,
                  hard_limit = EXCLUDED.hard_limit,
                  enabled = true
           RETURNING id""",
        (str(FAKE_TENANT), period, limit_usd, spend, reset_at, hard_limit),
        fetch=True, as_dict=True,
    )
    return rows[0]["id"]


def _spend_of(row_id):
    rows = db.query(
        "SELECT current_spend_usd, period_reset_at FROM sn_config.budgets WHERE id = %s",
        (row_id,), fetch=True, as_dict=True,
    )
    return rows[0]


def _cleanup():
    db.query("DELETE FROM sn_config.budgets WHERE scope_id = %s", (str(FAKE_TENANT),))


# ---------------------------------------------------------------- tests

def test_hard_limit_blocks_when_over():
    """A hard budget at or over its limit must refuse the next send."""
    try:
        _seed("daily", limit_usd=1.00, hard_limit=True, spend=0)

        allowed, reason, status = budget.check_budget(FAKE_TENANT)
        assert allowed is True, "fresh budget must allow"
        assert reason is None

        budget.record_spend(FAKE_TENANT, cost_usd=0.60)
        allowed, reason, _ = budget.check_budget(FAKE_TENANT)
        assert allowed is True, "0.60 of 1.00 must still allow"

        # The limit is a ceiling on accrued spend, so the turn that crosses it is billed
        # and the NEXT one is refused. That is the design: cost is only known after send.
        budget.record_spend(FAKE_TENANT, cost_usd=0.60)
        allowed, reason, status = budget.check_budget(FAKE_TENANT)
        assert allowed is False, "1.20 of 1.00 must block"
        assert "exhausted" in reason
        assert status[0]["over"] is True and status[0]["hard"] is True
    finally:
        _cleanup()


def test_soft_limit_reports_but_allows():
    """hard_limit=false is a warning surface, never a refusal."""
    try:
        _seed("daily", limit_usd=1.00, hard_limit=False, spend=5.00)
        allowed, reason, status = budget.check_budget(FAKE_TENANT)
        assert allowed is True, "soft budget must never block"
        assert reason is None
        assert status[0]["over"] is True, "but it must still report the breach"
        assert status[0]["pct"] >= 500.0
    finally:
        _cleanup()


def test_record_spend_leaves_other_scopes_alone():
    """record_spend must touch only rows applicable to the caller's scope.

    This is the guard on the production counter: a leak here would inflate tenant 1 and
    trip the owner's daily hard limit on spending that never happened.
    """
    before = db.query(
        "SELECT id, current_spend_usd FROM sn_config.budgets WHERE scope_id = '1' ORDER BY id",
        fetch=True, as_dict=True,
    )
    try:
        _seed("daily", limit_usd=100.00, hard_limit=True, spend=0)
        budget.record_spend(FAKE_TENANT, cost_usd=3.25)

        after = db.query(
            "SELECT id, current_spend_usd FROM sn_config.budgets WHERE scope_id = '1' ORDER BY id",
            fetch=True, as_dict=True,
        )
        assert [r["current_spend_usd"] for r in before] == [r["current_spend_usd"] for r in after], \
            "spend recorded for one tenant must not reach another tenant's budgets"
    finally:
        _cleanup()


def test_zero_and_negative_spend_is_ignored():
    """A failed turn reports no cost, and must not be recorded as one."""
    try:
        row_id = _seed("daily", limit_usd=10.00, hard_limit=True, spend=0)
        budget.record_spend(FAKE_TENANT, cost_usd=0)
        budget.record_spend(FAKE_TENANT, cost_usd=-1.5)
        assert float(_spend_of(row_id)["current_spend_usd"]) == 0.0
    finally:
        _cleanup()


def test_period_reset_zeroes_spend_and_advances_boundary():
    """An elapsed period must reset lazily on the next check, not accumulate forever."""
    try:
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        row_id = _seed("daily", limit_usd=1.00, hard_limit=True, spend=99.00, reset_at=past)

        # Before the reset this row is over a hard limit, so a stale boundary would look
        # exactly like a real breach and block the node until someone noticed.
        allowed, _, _ = budget.check_budget(FAKE_TENANT)
        assert allowed is True, "elapsed period must reset and allow"

        row = _spend_of(row_id)
        assert float(row["current_spend_usd"]) == 0.0
        assert row["period_reset_at"] > datetime.now(timezone.utc)
    finally:
        _cleanup()


def test_disabled_budget_is_ignored():
    """enabled=false must take a row out of enforcement entirely."""
    try:
        _seed("daily", limit_usd=1.00, hard_limit=True, spend=99.00)
        db.query("UPDATE sn_config.budgets SET enabled = false WHERE scope_id = %s",
                 (str(FAKE_TENANT),))
        allowed, _, status = budget.check_budget(FAKE_TENANT)
        assert allowed is True
        assert status == [], "a disabled row must not even appear in status"
    finally:
        _cleanup()


def test_owner_policy_is_configured():
    """Regression guard on the live configuration, not on the code.

    The failure this catches is the one that already happened: limits present but
    unenforceable. It asserts the numbers the owner set on 2026-08-31, so raising them
    again requires editing this file and saying why.
    """
    rows = db.query(
        """SELECT period, limit_usd, hard_limit, enabled
             FROM sn_config.budgets
            WHERE scope_type = 'tenant' AND scope_id = '1'""",
        fetch=True, as_dict=True,
    ) or []
    by_period = {r["period"]: r for r in rows}

    for period, want in OWNER_POLICY.items():
        assert period in by_period, f"tenant 1 has no {period} budget row"
        row = by_period[period]
        assert float(row["limit_usd"]) == float(want["limit_usd"]), \
            f"{period} limit is {row['limit_usd']}, owner policy says {want['limit_usd']}"
        assert row["hard_limit"] is want["hard_limit"], \
            f"{period} hard_limit is {row['hard_limit']}, owner policy says {want['hard_limit']}"
        assert row["enabled"] is True, f"{period} budget is disabled"


# ---------------------------------------------------------------- runner

def main():
    """Standalone runner, because no component venv on the node has pytest installed."""
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print("=" * 60)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
