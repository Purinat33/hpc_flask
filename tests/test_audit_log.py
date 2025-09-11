# tests/test_audit_log.py
from sqlalchemy import select, func
from models.base import SessionLocal
from models.schema import AuditLog


def _count_audit() -> int:
    with SessionLocal() as s:
        return s.execute(select(func.count(AuditLog.id))).scalar_one()


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
    # At least 2 entries (failure + success); there may be more from throttle checks, etc.
    assert after - before >= 2
