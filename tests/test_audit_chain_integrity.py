import json
import hashlib
from models.db import get_db


def _rehash_chain(rows):
    prev = ""
    for r in rows:
        payload = {
            "ts": r["ts"],
            "actor": r["actor"],
            "ip": r["ip"],
            "ua": r["ua"],
            "method": r["method"],
            "path": r["path"],
            "action": r["action"],
            "target": r["target"],
            "status": r["status"],
            "extra": json.loads(r["extra"] or "{}"),
        }
        s = prev + json.dumps(payload, separators=(",", ":"), sort_keys=True)
        expect = hashlib.sha256(s.encode("utf-8")).hexdigest()
        assert expect == r["hash"], "hash mismatch"
        assert r["prev_hash"] == prev
        prev = expect


def test_audit_chain_verified_and_detects_tamper(client):
    # cause a couple of audit entries (login fail + success)
    client.post("/login", data={"username": "admin", "password": "nope"})
    client.post("/login", data={"username": "admin", "password": "admin123"})

    db = get_db()
    rows = db.execute("""
      SELECT id, ts, actor, ip, ua, method, path, action, target, status, prev_hash, hash, extra
      FROM audit_log ORDER BY id ASC
    """).fetchall()
    _rehash_chain(rows)  # passes

    # Tamper with one row
    first_id = rows[0]["id"]
    with db:
        db.execute(
            "UPDATE audit_log SET action='tampered' WHERE id=?", (first_id,))

    rows2 = db.execute("""
      SELECT id, ts, actor, ip, ua, method, path, action, target, status, prev_hash, hash, extra
      FROM audit_log ORDER BY id ASC
    """).fetchall()

    tamper_detected = False
    try:
        _rehash_chain(rows2)
    except AssertionError:
        tamper_detected = True
    assert tamper_detected, "audit chain should detect tampering"
