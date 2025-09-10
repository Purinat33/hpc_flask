# models/audit_store.py
import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from flask import request, has_request_context

USE_PG = bool(os.getenv("DATABASE_URL"))

if USE_PG:
    import sqlalchemy as sa
    from models.base import init_engine_and_session
    from models.schema import AuditLog
    Engine, SessionLocal = init_engine_and_session()
else:
    from models.db import get_db  # legacy SQLite helper


# --- SQLite bootstrap (no-op on Postgres) ---
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                 -- ISO8601Z
  actor TEXT,
  ip TEXT,
  ua TEXT,
  method TEXT,
  path TEXT,
  action TEXT NOT NULL,
  target TEXT,
  status INTEGER,
  extra TEXT,
  prev_hash TEXT,
  hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
"""


def init_audit_schema():
    """Create the SQLite table/indexes if needed. Skipped on Postgres."""
    if USE_PG:
        return  # PG schema is managed by Alembic
    db = get_db()
    with db:
        for stmt in SCHEMA_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s)


def _now_iso() -> str:
    # "YYYY-MM-DDTHH:MM:SSZ"
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _compute_hash(prev_hash: str, payload: dict) -> str:
    s = prev_hash + json.dumps(payload, separators=(",", ":"),
                               sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _latest_hash_sqlite(cur) -> str:
    row = cur.execute(
        "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    return row["hash"] if row and row["hash"] else ""


def _latest_hash_pg(session) -> str:
    prev = session.execute(
        sa.select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)
    ).scalar()
    return prev or ""


def audit(
    action: str,
    target: Optional[str] = None,
    status: Optional[int] = None,
    extra: Optional[dict[str, Any]] = None,
    actor: Optional[str] = None,
):
    """
    Write one audit row. Safe to call anywhere (with or without request ctx).
    """
    ts = _now_iso()

    # Collect request context if present
    ip = ua = method = path = None
    if has_request_context():
        try:
            ip = request.headers.get(
                "X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
        except Exception:
            ip = None
        ua = request.user_agent.string if getattr(
            request, "user_agent", None) else None
        method = getattr(request, "method", None)
        path = getattr(request, "path", None)

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

    if USE_PG:
        from sqlalchemy.exc import SQLAlchemyError
        with SessionLocal() as s:
            prev = _latest_hash_pg(s)
            h = _compute_hash(prev, payload)
            obj = AuditLog(
                ts=payload["ts"],
                actor=payload["actor"],
                ip=payload["ip"],
                ua=payload["ua"],
                method=payload["method"],
                path=payload["path"],
                action=payload["action"],
                target=payload["target"],
                status=payload["status"],
                extra=json.dumps(payload["extra"], ensure_ascii=False),
                prev_hash=prev,
                hash=h,
            )
            s.add(obj)
            try:
                s.commit()
            except SQLAlchemyError:
                s.rollback()
                raise
        return

    # --- SQLite legacy path ---
    db = get_db()
    with db:
        cur = db.cursor()
        prev = _latest_hash_sqlite(cur)
        h = _compute_hash(prev, payload)
        cur.execute(
            """
            INSERT INTO audit_log(ts, actor, ip, ua, method, path, action, target, status, extra, prev_hash, hash)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload["ts"],
                payload["actor"],
                payload["ip"],
                payload["ua"],
                payload["method"],
                payload["path"],
                payload["action"],
                payload["target"],
                payload["status"],
                json.dumps(payload["extra"], ensure_ascii=False),
                prev,
                h,
            ),
        )


def list_audit(limit: int = 500) -> list[dict]:
    if USE_PG:
        with SessionLocal() as s:
            rows = s.execute(
                sa.select(
                    AuditLog.id,
                    AuditLog.ts,
                    AuditLog.actor,
                    AuditLog.ip,
                    AuditLog.method,
                    AuditLog.path,
                    AuditLog.action,
                    AuditLog.target,
                    AuditLog.status,
                )
                .order_by(AuditLog.id.desc())
                .limit(limit)
            ).all()
            return [dict(r._mapping) for r in rows]

    db = get_db()
    rows = db.execute(
        """
        SELECT id, ts, actor, ip, method, path, action, target, status
        FROM audit_log ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def export_csv() -> tuple[str, str]:
    """Return (filename, csv_text)."""
    import io
    import csv
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "ts", "actor", "ip", "ua", "method", "path",
               "action", "target", "status", "prev_hash", "hash", "extra"])

    if USE_PG:
        with SessionLocal() as s:
            rows = s.execute(
                sa.select(
                    AuditLog.id,
                    AuditLog.ts,
                    AuditLog.actor,
                    AuditLog.ip,
                    AuditLog.ua,
                    AuditLog.method,
                    AuditLog.path,
                    AuditLog.action,
                    AuditLog.target,
                    AuditLog.status,
                    AuditLog.prev_hash,
                    AuditLog.hash,
                    AuditLog.extra,
                )
                .order_by(AuditLog.id.desc())
            ).all()
            for r in rows:
                m = r._mapping
                w.writerow([m["id"], m["ts"], m["actor"], m["ip"], m["ua"], m["method"], m["path"],
                            m["action"], m["target"], m["status"], m["prev_hash"], m["hash"], m["extra"]])
            out.seek(0)
            return ("audit_export.csv", out.read())

    db = get_db()
    rows = db.execute(
        """
        SELECT id, ts, actor, ip, ua, method, path, action, target, status, prev_hash, hash, extra
        FROM audit_log ORDER BY id DESC
        """
    ).fetchall()
    for r in rows:
        w.writerow([r["id"], r["ts"], r["actor"], r["ip"], r["ua"], r["method"], r["path"],
                    r["action"], r["target"], r["status"], r["prev_hash"], r["hash"], r["extra"]])
    out.seek(0)
    return ("audit_export.csv", out.read())
