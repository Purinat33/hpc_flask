import sqlite3
from models.db import get_db


def _count_audit():
    db = get_db()
    return db.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]


def test_audit_records_login_success_and_failure(client, monkeypatch):
    before = _count_audit()

    # Bad password (failure)
    r1 = client.post("/login", data={"username": "admin", "password": "nope"})
    assert r1.status_code in (302, 303)

    # Good password (success)
    r2 = client.post(
        "/login", data={"username": "admin", "password": "admin123"})
    assert r2.status_code in (302, 303)

    after = _count_audit()
    # At least 2 entries (failure + success), often more due to throttling lookups etc.
    assert after - before >= 2
