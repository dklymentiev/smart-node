"""The permission chokepoint on routes/notifications.py.

- POST /api/notify        notify          -> notifications.notify
- POST /api/owner-alert   owner_alert_send -> notifications.notify

notifications.notify is held by the admin tier, so the planner and the
dispatcher reach it as the owner, and it is also admitted by trusted origin.
Asked of the real router, the real `requires()`, real identity and a database
built from the tree's bootstrap files.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from permission_db import ADMIN, OWNER, PLAIN, permission_db, session  # noqa: F401

EXPECTED = {
    ("POST", "/api/notify"): "notifications.notify",
    ("POST", "/api/owner-alert"): "notifications.notify",
}


def test_notifications_router_declares_catalogue_permissions():
    from routes import notifications
    from permissions import route_permission

    seen = {}
    for route in notifications.router.routes:
        for method in route.methods - {"HEAD"}:
            declared = route_permission(route)
            assert declared is not None, f"{method} {route.path} declares no permission"
            assert declared[1] is False, f"{method} {route.path} passes an object"
            seen[(method, route.path)] = declared[0]
    assert seen == EXPECTED


# ---------------------------------------------------------------------------
# the real chain: real identity, real `requires()`, the seeded catalogue
# ---------------------------------------------------------------------------

SECRET = "s" * 64


def _send(app, method, path, headers=None, **kwargs):
    import asyncio

    import httpx

    async def _go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as c:
            return await c.request(method, path, headers=headers or {}, **kwargs)

    return asyncio.run(_go())


def _service(name, secret=SECRET):
    return {"X-SN-Service": name, "X-SN-Service-Auth": secret}


NOTICE = {"assignee": "nobody-at-all", "task_id": 1, "event": "assigned",
          "title": "t", "summary": "s"}


@pytest.fixture
def notify_app(permission_db, monkeypatch):
    from routes import notifications

    monkeypatch.setenv("SN_SERVICE_AUTH_SECRET", SECRET)
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS",
                       "flows:notifications.notify;voice:internal.events.write")
    app = FastAPI()
    app.include_router(notifications.router)
    return app


def test_notify_is_refused_for_a_role_without_it_and_named(notify_app):
    r = _send(notify_app, "POST", "/api/notify", session(PLAIN), json=NOTICE)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail and "'notifications.notify'" in detail

    for headers in ({}, _service("voice"), _service("flows", "w" * 64)):
        r = _send(notify_app, "POST", "/api/notify", headers, json=NOTICE)
        assert r.status_code == 403, (headers, r.text)


@pytest.mark.parametrize("who", [OWNER, ADMIN])
def test_notify_is_admitted_by_role_for_the_admin_tier(notify_app, who):
    # The planner and the dispatcher raise their notices as the owner.
    r = _send(notify_app, "POST", "/api/notify", session(who), json=NOTICE)
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == [] and r.json()["user_id"] is None


def test_notify_from_a_listed_service_has_no_account_and_still_answers(notify_app, permission_db):
    """Admitted by origin the handler is handed no account. It must answer for
    a real assignee instead of failing on the tenant it would have compared."""
    with permission_db() as cur:
        cur.execute("SELECT id FROM sn_config.users WHERE email = %s", (OWNER,))
        owner_id = cur.fetchone()["id"]
    r = _send(notify_app, "POST", "/api/notify", _service("flows"),
              json={**NOTICE, "assignee": OWNER})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == owner_id


def test_a_wake_only_notice_never_reaches_a_person(notify_app, permission_db):
    """The planner's stale-task clock is for agents. Sent as an ordinary notice
    it would message the person holding the task on every scan."""
    def _outbound(cur):
        cur.execute("SELECT count(*) AS n FROM sn_core.messages "
                    "WHERE metadata->>'source' = 'notify'")
        return cur.fetchone()["n"]

    with permission_db(commit=True) as cur:
        cur.execute("SELECT id FROM sn_config.users WHERE email = %s", (OWNER,))
        owner_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO sn_config.user_channels (user_id, channel_slug, address, enabled) "
            "VALUES (%s, 'telegram', '1000', true) "
            "ON CONFLICT (user_id, channel_slug) DO UPDATE SET enabled = true, address = '1000'",
            (owner_id,))
        before = _outbound(cur)

    notice = {**NOTICE, "assignee": OWNER, "event": "stale_in_progress"}
    r = _send(notify_app, "POST", "/api/notify", _service("flows"),
              json={**notice, "wake_only": True})
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == [] and r.json()["woke"] is None
    with permission_db() as cur:
        assert _outbound(cur) == before

    # The same notice without the flag does reach them: the flag is what holds it.
    r = _send(notify_app, "POST", "/api/notify", _service("flows"), json=notice)
    assert r.status_code == 200, r.text
    assert "telegram" in r.json()["sent"]
    with permission_db() as cur:
        assert _outbound(cur) == before + 1


# ---------------------------------------------------------------------------
# POST /api/owner-alert: the owner as Settings has him, never a named person
# ---------------------------------------------------------------------------

ALERT = {"subject": "[sn-digest] test", "text": "body", "source": "event-digest"}


def _owner_alert_rows(cur):
    cur.execute("SELECT recipient, sender, metadata->>'subject' AS subject "
                "FROM sn_core.messages WHERE metadata->>'source' = 'event-digest' "
                "ORDER BY id")
    return cur.fetchall()


def test_owner_alert_is_refused_without_the_permission(notify_app):
    r = _send(notify_app, "POST", "/api/owner-alert", session(PLAIN), json=ALERT)
    assert r.status_code == 403, r.text


def test_owner_alert_goes_to_the_owner_channel_from_the_node_sender(
        notify_app, permission_db, monkeypatch):
    from lib import owner_alert
    monkeypatch.setattr(owner_alert, "EMAIL_SENDER", "node@example.test")
    with permission_db(commit=True) as cur:
        cur.execute("SELECT id FROM sn_config.users WHERE role = 'super-admin' "
                    "AND active ORDER BY id LIMIT 1")
        owner_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO sn_config.user_channels (user_id, channel_slug, address, enabled) "
            "VALUES (%s, 'email', 'owner-set-in-settings@example.test', true) "
            "ON CONFLICT (user_id, channel_slug) DO UPDATE SET enabled = true, "
            "address = 'owner-set-in-settings@example.test'", (owner_id,))
    r = _send(notify_app, "POST", "/api/owner-alert", session(OWNER), json=ALERT)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    with permission_db() as cur:
        rows = _owner_alert_rows(cur)
    assert rows[-1]["recipient"] == "owner-set-in-settings@example.test"
    assert rows[-1]["sender"] == "node@example.test"
    assert rows[-1]["subject"] == ALERT["subject"]


def test_owner_alert_without_a_sender_is_409_and_writes_nothing(
        notify_app, permission_db, monkeypatch):
    from lib import owner_alert
    monkeypatch.setattr(owner_alert, "EMAIL_SENDER", None)
    with permission_db() as cur:
        before = len(_owner_alert_rows(cur))
    r = _send(notify_app, "POST", "/api/owner-alert", session(OWNER), json=ALERT)
    assert r.status_code == 409, r.text
    assert "SN_NOTIFY_SENDER" in r.json()["detail"]
    with permission_db() as cur:
        assert len(_owner_alert_rows(cur)) == before
