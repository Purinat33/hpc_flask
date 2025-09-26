# models/audit_store.py
from sqlalchemy import asc
import os
import json
import hmac
import hashlib
from typing import Any, Optional
from datetime import datetime, timezone
from flask import request, has_request_context, g, current_app
from sqlalchemy import select
from models.base import session_scope
from models.schema import AuditLog
from flask_login import current_user

APP_SECRET = (os.getenv("AUDIT_HMAC_SECRET") or "secret-key").encode("utf-8")
ANONYMIZE_IP = os.getenv("AUDIT_ANONYMIZE_IP", "1") == "1"
RAW_UA = os.getenv("AUDIT_STORE_RAW_UA", "0") == "1"
SCHEMA_VERSION = 2
SIGNING_KEY_ID = os.getenv("AUDIT_HMAC_KEY_ID", "k1")

_ALLOWED_EXTRA_KEYS = {"reason", "note",
                       "diff", "count", "totals", "old", "new"}


def _load_keyring() -> dict[str, bytes]:
    ring: dict[str, bytes] = {}
    # Optional ring for rotated keys
    cfg = os.getenv("AUDIT_HMAC_KEYRING", "")
    if cfg:
        for part in cfg.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            kid, sec = part.split("=", 1)
            ring[kid.strip()] = sec.strip().encode("utf-8")
    # Always include current key
    ring[SIGNING_KEY_ID] = APP_SECRET
    return ring


def _ts_to_payload_str(ts_val: Any) -> str:
    """Recreate the exact 'ts' string format used when hashing."""
    if isinstance(ts_val, datetime):
        ts_val = ts_val.astimezone(timezone.utc)
        return ts_val.isoformat(timespec="seconds").replace("+00:00", "Z")
    # fallback: already a string
    s = str(ts_val)
    # normalize common DB formats like 'YYYY-MM-DD HH:MM:SS'
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except Exception:
        return s


def _rebuild_payload_from_row(r: AuditLog) -> dict:
    # NOTE: We only store UA fingerprint (RAW_UA==0), so use that.
    # If you ever enable RAW_UA=1 in production, store it to a column and use it here.
    return {
        "ts": _ts_to_payload_str(r.ts),
        "actor": r.actor,
        "actor_role": getattr(r, "actor_role", None),
        "request_id": getattr(r, "request_id", None),
        "session_id": getattr(r, "session_id", None),
        "ip": r.ip,
        "ua": getattr(r, "ua_fingerprint", None),
        "method": r.method,
        "path": r.path,
        "action": r.action,
        "target_type": getattr(r, "target_type", None),
        "target_id": getattr(r, "target_id", None),
        "outcome": getattr(r, "outcome", None),
        "status": r.status,
        "error_code": getattr(r, "error_code", None),
        "extra": r.extra or {},
        "schema_version": getattr(r, "schema_version", None) or SCHEMA_VERSION,
        "key_id": getattr(r, "key_id", None),
    }


def verify_chain(limit: Optional[int] = None) -> dict:
    """
    Return:
      {
        "ok": bool,
        "checked": int,
        "last_ok_id": int | None,
        "first_bad_id": int | None,
        "reason": str | None
      }
    """
    ring = _load_keyring()
    prev = ""
    checked = 0
    last_ok = None

    with session_scope() as s:
        q = s.execute(select(AuditLog).order_by(asc(AuditLog.id)))
        rows = q.scalars().all()
        if limit:
            rows = rows[: int(limit)]

        for r in rows:
            payload = _rebuild_payload_from_row(r)
            exp_hash = _compute_hash(prev, payload)

            if r.prev_hash != prev:
                return {
                    "ok": False,
                    "checked": checked,
                    "last_ok_id": last_ok,
                    "first_bad_id": r.id,
                    "reason": "prev_hash_mismatch",
                }

            if r.hash != exp_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "last_ok_id": last_ok,
                    "first_bad_id": r.id,
                    "reason": "hash_mismatch",
                }

            kid = payload.get("key_id") or SIGNING_KEY_ID
            key = ring.get(kid)
            if not key:
                return {
                    "ok": False,
                    "checked": checked,
                    "last_ok_id": last_ok,
                    "first_bad_id": r.id,
                    "reason": f"missing_key:{kid}",
                }

            exp_sig = hmac.new(key, exp_hash.encode(
                "utf-8"), hashlib.sha256).hexdigest()
            if r.signature != exp_sig:
                return {
                    "ok": False,
                    "checked": checked,
                    "last_ok_id": last_ok,
                    "first_bad_id": r.id,
                    "reason": "signature_mismatch",
                }

            # advance
            checked += 1
            last_ok = r.id
            prev = r.hash or ""

    return {
        "ok": True,
        "checked": checked,
        "last_ok_id": last_ok,
        "first_bad_id": None,
        "reason": None,
    }


def _now_isoz() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _latest_hash() -> str:
    with session_scope() as s:
        row = s.execute(select(AuditLog).order_by(
            AuditLog.id.desc()).limit(1)).scalars().first()
        return row.hash or "" if row else ""


def _compute_hash(prev_hash: str, payload: dict) -> str:
    s = prev_hash + json.dumps(payload, separators=(",", ":"),
                               sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sign(h: str) -> str:
    return hmac.new(APP_SECRET, h.encode("utf-8"), hashlib.sha256).hexdigest()


def _anon_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    if not ANONYMIZE_IP:
        return ip
    # Simple IPv4 /24 or IPv6 /48 truncation
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:3]) + "::"
    else:
        quads = ip.split(".")
        return ".".join(quads[:3]) + ".0"


def _ua_fingerprint(ua: str | None) -> str | None:
    if not ua:
        return None
    return None if RAW_UA else hashlib.sha256(ua.encode("utf-8")).hexdigest()[:32]


def _fingerprint(val: str | None, maxlen: int = 64) -> str | None:
    if not val:
        return None
    # hex sha256 is 64 chars; truncate further if desired
    return hashlib.sha256(val.encode("utf-8")).hexdigest()[:maxlen]


def _clean_extra(extra: Optional[dict[str, Any]]) -> dict:
    if not extra:
        return {}
    out = {}
    for k, v in extra.items():
        if k not in _ALLOWED_EXTRA_KEYS:
            continue
        if isinstance(v, str) and len(v) > 512:
            v = v[:512] + "…"
        out[k] = v
    return out


def audit(
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    outcome: str | None = None,            # 'success'|'failure'
    status: int | None = None,
    error_code: str | None = None,
    extra: Optional[dict[str, Any]] = None,
    actor: Optional[str] = None
) -> None:
    ts = _now_isoz()

    ip = ua = method = path = req_id = sess_id = None
    if has_request_context():
        try:
            fwd = request.headers.get("X-Forwarded-For", "")
            ip = (fwd.split(",")[0].strip() or request.remote_addr)
        except Exception:
            ip = None
        method = request.method
        path = request.path
        req_id = getattr(g, "request_id", None) or request.headers.get(
            "X-Request-ID")
        # Never store raw session cookies; keep a stable fingerprint only.
        cookie_name = getattr(
            current_app, "session_cookie_name", None) or "session"
        sess_raw = request.cookies.get(cookie_name, None)
        sess_id = _fingerprint(sess_raw, 64)

        ua_full = request.user_agent.string if request.user_agent else None
        ua = ua_full if RAW_UA else _ua_fingerprint(ua_full)
        ip = _anon_ip(ip)

    if actor is None:
        try:
            actor = getattr(current_user, "username", None) or "anonymous"
            actor_role = getattr(current_user, "role", None)
        except Exception:
            actor, actor_role = "anonymous", None
    else:
        actor_role = None

    payload = {
        "ts": ts, "actor": actor, "actor_role": actor_role,
        "request_id": req_id, "session_id": sess_id,
        "ip": ip, "ua": ua, "method": method, "path": path,
        "action": action, "target_type": target_type, "target_id": target_id,
        "outcome": outcome, "status": status, "error_code": error_code,
        "extra": _clean_extra(extra or {}),
        "schema_version": SCHEMA_VERSION,
        "key_id": SIGNING_KEY_ID,
    }

    prev = _latest_hash()
    h = _compute_hash(prev, payload)
    sig = _sign(h)

    with session_scope() as s:
        s.add(AuditLog(
            ts=ts,
            actor=actor, actor_role=actor_role,
            request_id=req_id, session_id=sess_id,
            ip=ip, ua_fingerprint=(ua if not RAW_UA else None),
            method=method, path=path,
            action=action, target_type=target_type, target_id=target_id,
            outcome=outcome, status=status, error_code=error_code,
            extra=payload["extra"],
            prev_hash=prev, hash=h, signature=sig, schema_version=SCHEMA_VERSION,
            key_id=SIGNING_KEY_ID
        ))


def list_audit(limit: int = 500) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "ts": r.ts,
                "actor": r.actor,
                "ip": r.ip,
                "method": r.method,
                "path": r.path,
                "action": r.action,
                # show a friendly one-field “target” while using the columns you actually have
                "target": (
                    f"{r.target_type}:{r.target_id}"
                    if (getattr(r, "target_type", None) or getattr(r, "target_id", None))
                    else None
                ),
                "status": r.status,
                "outcome": getattr(r, "outcome", None),
                "error_code": getattr(r, "error_code", None),
                "actor_role": getattr(r, "actor_role", None),
            }
            for r in rows
        ]


def export_csv() -> tuple[str, str]:
    import io
    import csv
    with session_scope() as s:
        rows = s.execute(
            select(AuditLog).order_by(AuditLog.id.desc())
        ).scalars().all()

        # Build tuples using existing columns; prefer ua_fingerprint over ua (which doesn’t exist)
        data = [(
            r.id,
            r.ts,
            r.actor,
            r.ip,
            getattr(r, "ua_fingerprint", None),
            r.method,
            r.path,
            r.action,
            getattr(r, "target_type", None),
            getattr(r, "target_id", None),
            r.status,
            getattr(r, "outcome", None),
            getattr(r, "error_code", None),
            getattr(r, "actor_role", None),
            getattr(r, "request_id", None),
            getattr(r, "session_id", None),
            getattr(r, "schema_version", None),
            getattr(r, "prev_hash", None),
            getattr(r, "hash", None),
            getattr(r, "signature", None),
            getattr(r, "key_id", None),
            r.extra,
        ) for r in rows]

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "id", "ts", "actor", "ip", "ua_fingerprint", "method", "path", "action",
        "target_type", "target_id", "status", "outcome", "error_code", "actor_role",
        "request_id", "session_id", "schema_version", "prev_hash", "hash", "signature",
        "key_id", "extra",
    ])
    w.writerows(data)
    out.seek(0)
    return ("audit_export.csv", out.read())
