# tests/conftest.py
import os
import pytest
from sqlalchemy import text
from app import create_app
from models.base import Base, init_engine_and_session
from models.users_db import create_user


@pytest.fixture(scope="session", autouse=True)
def _set_env():
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("METRICS_ENABLED", "0")
    os.environ.setdefault("COPILOT_ENABLED", "0")
    os.environ.setdefault("AUTO_CREATE_SCHEMA", "1")
    yield


@pytest.fixture(scope="session")
def app():
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})


@pytest.fixture(scope="session")
def db_engine():
    engine, _Session = init_engine_and_session()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Ensure audit roles can access the freshly-created tables/sequences
    with engine.begin() as conn:
        conn.execute(text("""
            DO $$
            BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='audit_writer') THEN
                CREATE ROLE audit_writer LOGIN PASSWORD 'auditw';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='audit_reader') THEN
                CREATE ROLE audit_reader LOGIN PASSWORD 'auditro';
            END IF;
            END$$;
        """))
        conn.execute(
            text("GRANT USAGE ON SCHEMA public TO audit_writer, audit_reader;"))
        conn.execute(text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO audit_writer;"))
        conn.execute(
            text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO audit_reader;"))
        conn.execute(text(
            "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO audit_writer;"))

    return engine


@pytest.fixture(autouse=True)
def _db_clean(db_engine):
    with db_engine.begin() as conn:
        conn.execute(text("""
            DO $$
            DECLARE r RECORD;
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


@pytest.fixture
def admin_user(client):
    # ensure admin exists
    create_user("admin", "admin", role="admin")
    # login
    client.post("/login", data={"username": "admin",
                "password": "admin"}, follow_redirects=True)
    yield
    # logout
    client.post("/logout", follow_redirects=True)


@pytest.fixture
def login_admin(client, admin_user):
    u, p = admin_user
    # Your login form field names may differ
    client.post("/login", data={"username": u,
                "password": p}, follow_redirects=True)
    yield
    client.post("/logout")
