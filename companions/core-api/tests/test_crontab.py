"""Unit tests for the DB -> cron.d renderer (no database needed)."""

from lib.crontab import render_crontab, job_name, is_materialized, read_installed


def test_render_basic():
    rows = [
        {"id": 1, "schedule": "*/5 * * * *", "command": "echo hi",
         "crontab_user": "root", "enabled": True, "description": "tick"},
    ]
    out = render_crontab(rows)
    assert "GENERATED from sn_core.crons" in out
    assert "# tick" in out
    assert "*/5 * * * * root /usr/local/lib/smart-node/cron-runner.sh cron-1 echo hi" in out


def test_disabled_excluded():
    rows = [
        {"id": 1, "schedule": "* * * * *", "command": "a", "enabled": False},
        {"id": 2, "schedule": "* * * * *", "command": "b", "enabled": True},
    ]
    out = render_crontab(rows)
    assert "cron-2 b" in out
    assert "cron-1" not in out


def test_default_user_and_job_name():
    rows = [{"id": 7, "schedule": "0 * * * *", "command": "x", "enabled": True}]
    out = render_crontab(rows)
    assert " root /usr/local/lib/smart-node/cron-runner.sh cron-7 x" in out
    assert job_name({"id": 7}) == "cron-7"


def test_slug_used_as_job_name():
    rows = [{"id": 9, "slug": "nightly-backup", "schedule": "@daily",
             "command": "x", "enabled": True}]
    out = render_crontab(rows)
    assert "@daily root /usr/local/lib/smart-node/cron-runner.sh nightly-backup x" in out
    assert job_name({"id": 9, "slug": "nightly-backup"}) == "nightly-backup"
    assert job_name({"id": 9, "slug": None}) == "cron-9"


def test_custom_runner_and_user():
    rows = [{"id": 3, "schedule": "@daily", "command": "y",
             "crontab_user": "agent", "enabled": True}]
    out = render_crontab(rows, runner="/x/runner.sh")
    assert "@daily agent /x/runner.sh cron-3 y" in out


def test_materialized_follows_the_installed_file():
    row = {"id": 7, "slug": "nightly-collect", "schedule": "17 4 * * *",
           "command": "collect", "crontab_user": "agent-demo", "enabled": True}
    installed = render_crontab([row])
    assert is_materialized(row, installed) is True
    assert is_materialized(row, render_crontab([])) is False


def test_a_changed_schedule_is_not_materialized_until_rendered():
    row = {"id": 7, "slug": "j", "schedule": "17 4 * * *", "command": "x", "enabled": True}
    installed = render_crontab([row])
    assert is_materialized(dict(row, schedule="18 4 * * *"), installed) is False


def test_a_disabled_row_is_materialized_once_its_line_is_gone():
    row = {"id": 7, "slug": "j", "schedule": "17 4 * * *", "command": "x", "enabled": False}
    assert is_materialized(row, render_crontab([])) is True
    assert is_materialized(row, render_crontab([dict(row, enabled=True)])) is False


def test_an_unreadable_file_is_unknown_not_missing(tmp_path):
    row = {"id": 7, "schedule": "* * * * *", "command": "x", "enabled": True}
    assert read_installed(str(tmp_path / "absent")) is None
    assert is_materialized(row, None) is None
