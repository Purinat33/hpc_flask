# models/audit_store.py
import json
import hashlib
from datetime import datetime
from typing import Any, Optional
from flask import request, has_request_context
from models.db import get_db

# --- schema (ensure this runs once at startup/migrate time) ---
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                 -- ISO8601Z
  actor TEXT,                       -- username (or 'anonymous')
  ip TEXT,                          -- remote addr
  ua TEXT,                          -- user agent
  method TEXT,                      -- HTTP method
  path TEXT,                        -- request path
  action TEXT NOT NULL,             -- short key: 'rates.update', 'auth.login.success'
  target TEXT,                      -- e.g. 'tier=mu' or 'receipt=123'
  status INTEGER,                   -- HTTP status or domain status (optional)
  extra TEXT,                       -- JSON blob (optional)
  prev_hash TEXT,                   -- previous event hash
  hash TEXT                         -- sha256(prev_hash + payload)
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
"""


def init_audit_schema():
    db = get_db()
    with db:
        for stmt in SCHEMA_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s)


def _latest_hash(cur) -> str:
    row = cur.execute(
        "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    return row["hash"] if row and row["hash"] else ""


def _compute_hash(prev_hash: str, payload: dict) -> str:
    s = prev_hash + json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def audit(action: str,
          target: Optional[str] = None,
          status: Optional[int] = None,
          extra: Optional[dict[str, Any]] = None,
          actor: Optional[str] = None):
    """
    Write one audit row. Safe to call anywhere (with or without request ctx).
    """
    db = get_db()
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # Collect context if we’re inside a request
    ip = ua = method = path = None
    if has_request_context():
        try:
            ip = request.headers.get(
                "X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
        except Exception:
            ip = None
        ua = (request.user_agent.string if request.user_agent else None)
        method = request.method
        path = request.path

    # Decide actor if not provided
    if actor is None:
        try:
            from flask_login import current_user
            actor = getattr(current_user, "username", None) or "anonymous"
        except Exception:
            actor = "anonymous"

    payload = {
        "ts": ts,
        "actor": actor,
        "ip": ip,
        "ua": ua,
        "method": method,
        "path": path,
        "action": action,
        "target": target,
        "status": status,
        "extra": (extra or {}),
    }

    with db:
        cur = db.cursor()
        prev = _latest_hash(cur)
        h = _compute_hash(prev, payload)
        cur.execute("""
            INSERT INTO audit_log(ts, actor, ip, ua, method, path, action, target, status, extra, prev_hash, hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            payload["ts"], payload["actor"], payload["ip"], payload["ua"],
            payload["method"], payload["path"], payload["action"], payload["target"],
            payload["status"], json.dumps(
                payload["extra"], ensure_ascii=False),
            prev, h
        ))


def list_audit(limit: int = 500) -> list[dict]:
    db = get_db()
    rows = db.execute("""
      SELECT id, ts, actor, ip, method, path, action, target, status
      FROM audit_log ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def export_csv() -> tuple[str, str]:
    """Return (filename, csv_text)."""
    db = get_db()
    rows = db.execute("""
      SELECT id, ts, actor, ip, ua, method, path, action, target, status, prev_hash, hash, extra
      FROM audit_log ORDER BY id DESC
    """).fetchall()
    import io
    import csv
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "ts", "actor", "ip", "ua", "method", "path",
               "action", "target", "status", "prev_hash", "hash", "extra"])
    for r in rows:
        w.writerow([r["id"], r["ts"], r["actor"], r["ip"], r["ua"], r["method"], r["path"],
                    r["action"], r["target"], r["status"], r["prev_hash"], r["hash"], r["extra"]])
    out.seek(0)
    return ("audit_export.csv", out.read())
