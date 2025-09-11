# models/security_throttle.py (Postgres / SQLAlchemy)
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from flask import current_app
from sqlalchemy import select, and_
from models.base import session_scope
from models.schema import AuthThrottle


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_status(username: str, ip: str) -> dict:
    with session_scope() as s:
        row = s.execute(select(AuthThrottle).where(and_(
            AuthThrottle.username == username, AuthThrottle.ip == ip))).scalars().first()
        if not row:
            return {"fail_count": 0, "window_start": None, "locked_until": None}
        return {"fail_count": row.fail_count, "window_start": row.window_start, "locked_until": row.locked_until}


def is_locked(username: str, ip: str) -> Tuple[bool, int]:
    s = get_status(username, ip)
    lu = _parse_iso(s.get("locked_until"))
    if lu:
        now = datetime.now(timezone.utc)
        if now < lu:
            return True, int((lu - now).total_seconds())
    return False, 0


def register_failure(username: str, ip: str,
                     window_sec: Optional[int] = None,
                     max_fails: Optional[int] = None,
                     lock_sec: Optional[int] = None) -> bool:
    cfg = current_app.config if current_app else {}
    window_sec = window_sec or int(cfg.get("AUTH_THROTTLE_WINDOW_SEC", 60))
    max_fails = max_fails or int(cfg.get("AUTH_THROTTLE_MAX_FAILS", 5))
    lock_sec = lock_sec or int(cfg.get("AUTH_THROTTLE_LOCK_SEC", 300))

    now = datetime.now(timezone.utc)
    now_iso = _now_iso()
    with session_scope() as s:
        row = s.execute(select(AuthThrottle).where(and_(
            AuthThrottle.username == username, AuthThrottle.ip == ip))).scalars().first()
        if row:
            ws = _parse_iso(row.window_start)
            fc = int(row.fail_count or 0)
            lu = _parse_iso(row.locked_until)
            if not ws or (now - ws).total_seconds() > window_sec:
                ws = now
                fc = 0
            fc += 1
            locked_until = lu
            locked_now = False
            if fc >= max_fails:
                locked_until = now + timedelta(seconds=lock_sec)
                locked_now = True

            row.window_start = now_iso
            row.fail_count = fc
            row.locked_until = locked_until.isoformat(timespec="seconds").replace(
                "+00:00", "Z") if locked_until else None
            s.add(row)
            return locked_now

        s.add(AuthThrottle(username=username, ip=ip,
              window_start=now_iso, fail_count=1, locked_until=None))
    return False


def reset(username: str, ip: str):
    with session_scope() as s:
        row = s.execute(select(AuthThrottle).where(and_(
            AuthThrottle.username == username, AuthThrottle.ip == ip))).scalars().first()
        if row:
            row.window_start = _now_iso()
            row.fail_count = 0
            row.locked_until = None
            s.add(row)
