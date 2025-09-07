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


@pytest.fixture
def app(tmp_instance: Path, monkeypatch, sample_csv_text):
    # Write test CSV
    (tmp_instance / "test.csv").write_text(sample_csv_text, encoding="utf-8")

    # Point all DB/files to the temp instance BEFORE app is created
    monkeypatch.setenv("BILLING_DB", str(tmp_instance / "billing.sqlite3"))
    monkeypatch.setenv("FALLBACK_CSV", str(tmp_instance / "test.csv"))
    # optional: isolate users DB too so seeding lands here
    monkeypatch.setenv("USERS_DB", str(tmp_instance / "users.sqlite3"))
    # ensure admin password is deterministic for tests
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")

    from app import create_app
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test-secret",
    )

    ctx = app.app_context()
    ctx.push()
    try:
        yield app
    finally:
        ctx.pop()


@pytest.fixture
def client(app):
    return app.test_client()
