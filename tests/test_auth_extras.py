from __future__ import annotations
from contextlib import contextmanager
from datetime import timedelta
import pytest
from flask import template_rendered, url_for

from tests.utils import login_user, login_admin
from models.db import get_db

# Helper to capture Jinja context


def _url(app, endpoint, **values):
    """Build a url_for() inside a request context for tests."""
    with app.test_request_context():
        return url_for(endpoint, **values)


@contextmanager
def captured_templates(app):
    rec = []

    def receiver(sender, template, context, **extra):
        rec.append((template, context))
    template_rendered.connect(receiver, app)
    try:
        yield rec
    finally:
        template_rendered.disconnect(receiver, app)


def test_login_get_messages_locked_and_bad(client, app):
    with captured_templates(app) as rec:
        r = client.get("/login?err=locked&left=125&u=bob")
        assert r.status_code == 200
    _, ctx = rec[-1]
    # 125s -> 2:05
    assert "2:05" in ctx["message"]
    assert ctx["last_username"] == "bob"

    with captured_templates(app) as rec2:
        r2 = client.get("/login?err=bad&u=alice")
        assert r2.status_code == 200
    _, ctx2 = rec2[-1]
    assert "Invalid username or password." in ctx2["message"]
    assert ctx2["last_username"] == "alice"


def test_admin_required_redirects_non_admin_and_audits(client, app):
    # Log in as a normal user
    login_user(client, "alice", "alice")
    r = client.get("/admin", follow_redirects=False)
    # Redirected to playground (home)
    assert r.status_code in (302, 303)
    assert _url(app, "playground") in r.headers["Location"]
    # Audit recorded with forbidden action
    db = get_db()
    row = db.execute(
        "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row and row["action"] == "auth.forbidden"


def test_login_post_when_already_locked_redirects_with_left_and_audits(client, monkeypatch, app):
    # Force active lock
    monkeypatch.setattr("controllers.auth.is_locked", lambda u, ip: (True, 90))
    # Post credentials (values don't matter; locked checked first)
    r = client.post(
        "/login", data={"username": "alice", "password": "x"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    loc = r.headers["Location"]
    assert "err=locked" in loc and "left=90" in loc and "u=alice" in loc

    db = get_db()
    row = db.execute(
        "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["action"] == "auth.lockout.active"


def test_login_post_bad_then_generic_error(client, monkeypatch, app):
    # Not locked yet
    monkeypatch.setattr("controllers.auth.is_locked", lambda u, ip: (False, 0))
    monkeypatch.setattr("controllers.auth.verify_password", lambda u, p: False)
    # Not enough failures to lock
    monkeypatch.setattr("controllers.auth.register_failure",
                        lambda u, ip: False)

    r = client.post(
        "/login", data={"username": "eve", "password": "wrong"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "err=bad" in r.headers["Location"] and "u=eve" in r.headers["Location"]

    db = get_db()
    row = db.execute(
        "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["action"] == "auth.login.failure"


def test_login_post_bad_triggers_lock_start_with_configured_seconds(client, monkeypatch, app):
    # Not locked; this failure causes a lock
    monkeypatch.setattr("controllers.auth.is_locked", lambda u, ip: (False, 0))
    monkeypatch.setattr("controllers.auth.verify_password", lambda u, p: False)
    monkeypatch.setattr(
        "controllers.auth.register_failure", lambda u, ip: True)
    app.config.update(
        AUTH_THROTTLE_LOCK_SEC=123,
        AUTH_THROTTLE_WINDOW_SEC=60,
        AUTH_THROTTLE_MAX_FAILS=5,
    )

    r = client.post(
        "/login", data={"username": "eve", "password": "wrong"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    loc = r.headers["Location"]
    assert "err=locked" in loc and "left=123" in loc

    db = get_db()
    # Last two actions should include lockout.start
    row = db.execute(
        "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["action"] == "auth.lockout.start"


def test_login_post_success_resets_and_optionally_emits_lockout_end(client, monkeypatch, app):
    # Simulate prior lock present -> should emit lockout.end on success
    monkeypatch.setattr("controllers.auth.is_locked", lambda u, ip: (False, 0))
    monkeypatch.setattr("controllers.auth.verify_password", lambda u, p: True)
    # get_status indicates previously locked
    monkeypatch.setattr("controllers.auth.get_status",
                        lambda u, ip: {"locked_until": "x"})
    monkeypatch.setattr("controllers.auth.reset", lambda u, ip: None)
    # ensure user exists
    monkeypatch.setattr(
        "controllers.auth.get_user",
        lambda u: {"username": u, "role": "user"}
    )

    r = client.post(
        "/login", data={"username": "alice", "password": "ok"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert _url(app, "playground") in r.headers["Location"]

    db = get_db()
    actions = [r["action"] for r in db.execute(
        "SELECT action FROM audit_log ORDER BY id DESC LIMIT 3"
    ).fetchall()]
    # Expect success and lockout.end among the latest few
    assert "auth.login.success" in actions
    assert "auth.lockout.end" in actions


def test_logout_audits_and_redirects(client, app):
    login_user(client, "alice", "alice")
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert _url(app, "auth.login") in r.headers["Location"]
    db = get_db()
    row = db.execute(
        "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["action"] == "auth.logout"


def test_load_user_missing_and_valid(app, monkeypatch):
    from controllers.auth import load_user, User

    with app.app_context():
        # Missing
        monkeypatch.setattr("controllers.auth.get_user", lambda u: None)
        assert load_user("ghost") is None

        # Present
        monkeypatch.setattr(
            "controllers.auth.get_user",
            lambda u: {"username": "alice", "role": "admin"}
        )
        u = load_user("alice")
        assert isinstance(u, User)
        assert u.username == "alice" and u.is_admin is True
