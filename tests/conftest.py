import os
import io
import textwrap
import sqlite3
import pytest
from app import create_app
from datetime import date
from pathlib import Path


@pytest.fixture
def tmp_instance(tmp_path: Path):
    # temp instance dir with a temp DB and CSV used by the app config
    (tmp_path / "instance").mkdir()
    return tmp_path / "instance"


@pytest.fixture
def sample_csv_text():
    return textwrap.dedent("""\
        User|JobID|Elapsed|TotalCPU|ReqTRES|End|State
        a.u|1|01:00:00|02:00:00|cpu=2,mem=4G|2025-02-05T10:00:00|COMPLETED
        b.u|2|00:30:00|00:30:00|cpu=1,mem=1G|2025-12-31T00:00:00|COMPLETED
    """)


# tests/conftest.py


# tests/conftest.py
@pytest.fixture
def app(tmp_instance: Path, monkeypatch, sample_csv_text):
    (tmp_instance / "test.csv").write_text(sample_csv_text, encoding="utf-8")

    monkeypatch.setenv("BILLING_DB", str(tmp_instance / "billing.sqlite3"))
    monkeypatch.setenv("FALLBACK_CSV", str(tmp_instance / "test.csv"))
    monkeypatch.setenv("USERS_DB", str(tmp_instance / "users.sqlite3"))
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    # make behavior deterministic
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SEED_DEMO_USERS", "0")   # we'll seed ourselves

    app = create_app({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test-secret",
    })

    # Ensure alice/bob exist in THIS test database
    from models.users_db import get_user, create_user
    with app.app_context():
        if not get_user("alice"):
            create_user("alice", "alice", role="user")
        if not get_user("bob"):
            create_user("bob", "bob", role="user")

    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def client(app):
    return app.test_client()
