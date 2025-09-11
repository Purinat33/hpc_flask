# tests/conftest.py
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url, URL

from app import create_app
from models.base import Base, init_engine_and_session
from models.users_db import create_user

# Use a dedicated test DB (never point at your real one!)
TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://hpc_user:muict@localhost:5433/hpc_test",
)


def _ensure_db_exists(url_str: str) -> None:
    """
    Connect to the maintenance DB and CREATE DATABASE <name> if missing.
    Uses the same credentials as TEST_DB_URL. If your role can't create
    databases, create it once manually instead.
    """
    url = make_url(url_str)
    dbname = url.database
    admin_url: URL = url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.exec_driver_sql(
                "SELECT 1 FROM pg_database WHERE datname=%s", (dbname,)
            ).scalar()
            if not exists:
                owner = url.username or "postgres"
                conn.exec_driver_sql(
                    f'CREATE DATABASE "{dbname}" OWNER "{owner}";')
    finally:
        admin_engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _db_bootstrap():
    """
    Point the app at a test Postgres, create/drop all tables once
    for the entire test session.
    """
    os.environ["DATABASE_URL"] = TEST_DB_URL

    # Create the test DB if it doesn't exist yet
    _ensure_db_exists(TEST_DB_URL)

    engine, _Session = init_engine_and_session()

    # Start clean
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    # Optional: ensure the partial unique index that Alembic normally creates
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE schemaname='public'
                  AND indexname='uq_payments_provider_idem_notnull'
              ) THEN
                CREATE UNIQUE INDEX uq_payments_provider_idem_notnull
                ON payments (provider, idempotency_key)
                WHERE idempotency_key IS NOT NULL;
              END IF;
            END$$;
            """
        )

    yield

    # Tear down when tests finish
    Base.metadata.drop_all(engine)


@pytest.fixture
def tmp_instance(tmp_path: Path) -> Path:
    (tmp_path / "instance").mkdir()
    return tmp_path / "instance"


@pytest.fixture
def sample_csv_text():
    return textwrap.dedent(
        """\
        User|JobID|Elapsed|TotalCPU|ReqTRES|End|State
        a.u|1|01:00:00|02:00:00|cpu=2,mem=4G|2025-02-05T10:00:00|COMPLETED
        b.u|2|00:30:00|00:30:00|cpu=1,mem=1G|2025-12-31T00:00:00|COMPLETED
        """
    )


@pytest.fixture
def app(tmp_instance: Path, monkeypatch, sample_csv_text):
    # CSV used by data_sources fallback
    (tmp_instance / "test.csv").write_text(sample_csv_text, encoding="utf-8")

    # PG-only env; no SQLite variables
    monkeypatch.setenv("FALLBACK_CSV", str(tmp_instance / "test.csv"))
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SEED_DEMO_USERS", "0")
    # DATABASE_URL is already set by _db_bootstrap

    flask_app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test-secret",
        }
    )

    # Seed users in THIS test DB
    with flask_app.app_context():
        for u in (("alice", "alice"), ("bob", "bob")):
            try:
                create_user(u[0], u[1], role="user")
            except Exception:
                pass  # already exists

    ctx = flask_app.app_context()
    ctx.push()
    try:
        yield flask_app
    finally:
        ctx.pop()


@pytest.fixture
def client(app):
    return app.test_client()
