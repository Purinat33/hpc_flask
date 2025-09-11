# models/audit_store.py (Postgres / SQLAlchemy)
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional
from flask import request, has_request_context
from sqlalchemy import select
from models.base import session_scope
from models.schema import AuditLog


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _latest_hash() -> str:
    with session_scope() as s:
        row = s.execute(select(AuditLog).order_by(
            AuditLog.id.desc()).limit(1)).scalars().first()
        return row.hash or "" if row else ""


def _compute_hash(prev_hash: str, payload: dict) -> str:
    s = prev_hash + json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def audit(action: str, target: Optional[str] = None, status: Optional[int] = None,
          extra: Optional[dict[str, Any]] = None, actor: Optional[str] = None):
    ts = _now_iso()
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

    if actor is None:
        try:
            from flask_login import current_user
            actor = getattr(current_user, "username", None) or "anonymous"
        except Exception:
            actor = "anonymous"

    payload = {"ts": ts, "actor": actor, "ip": ip, "ua": ua, "method": method, "path": path,
               "action": action, "target": target, "status": status, "extra": (extra or {})}

    prev = _latest_hash()
    h = _compute_hash(prev, payload)

    with session_scope() as s:
        s.add(AuditLog(
            ts=ts, actor=actor, ip=ip, ua=ua, method=method, path=path,
            action=action, target=target, status=status,
            extra=json.dumps(payload["extra"], ensure_ascii=False),
            prev_hash=prev, hash=h
        ))


def list_audit(limit: int = 500) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(select(AuditLog).order_by(
            AuditLog.id.desc()).limit(limit)).scalars().all()
        return [
            {"id": r.id, "ts": r.ts, "actor": r.actor, "ip": r.ip, "method": r.method,
             "path": r.path, "action": r.action, "target": r.target, "status": r.status}
            for r in rows
        ]


def export_csv() -> tuple[str, str]:
    import io
    import csv
    with session_scope() as s:
        rows = s.execute(select(AuditLog).order_by(
            AuditLog.id.desc())).scalars().all()
        data = [(r.id, r.ts, r.actor, r.ip, r.ua, r.method, r.path,
                 r.action, r.target, r.status, r.prev_hash, r.hash, r.extra)
                for r in rows]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "ts", "actor", "ip", "ua", "method", "path",
               "action", "target", "status", "prev_hash", "hash", "extra"])
    for tup in data:
        w.writerow(tup)
    out.seek(0)
    return ("audit_export.csv", out.read())
