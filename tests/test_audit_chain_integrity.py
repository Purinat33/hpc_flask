# tests/test_audit_chain_integrity.py
from __future__ import annotations
import json
import hashlib
from sqlalchemy import select
from models.base import SessionLocal
from models.schema import AuditLog


def _rehash_chain(rows: list[AuditLog]) -> None:
    prev = ""
    for r in rows:
        payload = {
            "ts": r.ts,
            "actor": r.actor,
            "ip": r.ip,
            "ua": r.ua,
            "method": r.method,
            "path": r.path,
            "action": r.action,
            "target": r.target,
            "status": r.status,
            "extra": json.loads(r.extra or "{}"),
        }
        s = prev + json.dumps(payload, separators=(",", ":"), sort_keys=True)
        expect = hashlib.sha256(s.encode("utf-8")).hexdigest()
        assert expect == (r.hash or ""), "hash mismatch"
        assert (r.prev_hash or "") == prev
        prev = expect


def test_audit_chain_verified_and_detects_tamper(client, app):
    # Trigger a couple of audit rows (login fail + success)
    client.post("/login", data={"username": "admin", "password": "nope"})
    client.post("/login", data={"username": "admin", "password": "admin123"})

    with SessionLocal() as s:
        rows = s.execute(
            select(AuditLog).order_by(AuditLog.id.asc())
        ).scalars().all()
        assert len(rows) >= 2, "expected at least two audit rows"
        _rehash_chain(rows)  # should pass

        # Tamper with the first row
        first_id = rows[0].id
        s.query(AuditLog).filter(AuditLog.id == first_id).update(
            {AuditLog.action: "tampered"}
        )
        s.commit()

        rows2 = s.execute(
            select(AuditLog).order_by(AuditLog.id.asc())
        ).scalars().all()

    tamper_detected = False
    try:
        _rehash_chain(rows2)
    except AssertionError:
        tamper_detected = True

    assert tamper_detected, "audit chain should detect tampering"
