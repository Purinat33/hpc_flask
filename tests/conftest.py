import os
import pytest
from sqlalchemy import text
from app import create_app
from models.base import Base, init_engine_and_session


@pytest.fixture(scope="session", autouse=True)
def _set_env():
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("METRICS_ENABLED", "0")
    os.environ.setdefault("COPILOT_ENABLED", "0")
    # IMPORTANT: point to test Postgres (compose.test.yml sets it inside container)
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:55432/hpc_test")
    yield


@pytest.fixture(scope="session")
def app():
    # Disable CSRF in tests to simplify form posts
    test_config = {
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
    }
    app = create_app(test_config)
    return app


@pytest.fixture(scope="session")
def db_engine():
    engine, _Session = init_engine_and_session()
    # Create schema once per session
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture(autouse=True)
def _db_clean(db_engine):
    # Truncate all tables before each test for isolation
    # Works even if models call their own session_scope() under the hood
    with db_engine.begin() as conn:
        conn.execute(text("""
            DO $$
            DECLARE
              r RECORD;
            BEGIN
              FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
                EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' RESTART IDENTITY CASCADE';
              END LOOP;
            END$$;
        """))
    yield


@pytest.fixture()
def client(app):
    return app.test_client()
