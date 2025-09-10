# models/security_throttle.py
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
import os
from flask import current_app

USE_PG = bool(os.getenv("DATABASE_URL"))

if USE_PG:
    import sqlalchemy as sa
    from models.base import init_engine_and_session
    from models.schema import AuthThrottle
    Engine, SessionLocal = init_engine_and_session()
else:
    from models.db import get_db  # legacy SQLite path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auth_throttle (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL,
  ip TEXT NOT NULL,
  window_start TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_throttle_user_ip
  ON auth_throttle(username, ip);
"""


def init_throttle_schema():
    # On Postgres, Alembic manages the table; nothing to do.
    if USE_PG:
        return
    db = get_db()
    with db:
        for stmt in SCHEMA_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s)


def _now_iso():
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
    if USE_PG:
        with SessionLocal() as s:
            row = s.execute(
                sa.select(AuthThrottle.window_start,
                          AuthThrottle.fail_count, AuthThrottle.locked_until)
                .where(AuthThrottle.username == username, AuthThrottle.ip == ip)
            ).one_or_none()
            if not row:
                return {"fail_count": 0, "window_start": None, "locked_until": None}
            m = row._mapping
            return {
                "fail_count": int(m["fail_count"] or 0),
                "window_start": m["window_start"],
                "locked_until": m["locked_until"],
            }

    db = get_db()
    row = db.execute(
        "SELECT window_start, fail_count, locked_until FROM auth_throttle WHERE username=? AND ip=?",
        (username, ip)
    ).fetchone()
    if not row:
        return {"fail_count": 0, "window_start": None, "locked_until": None}
    return {
        "fail_count": row["fail_count"],
        "window_start": row["window_start"],
        "locked_until": row["locked_until"],
    }


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
    """Returns True if this failure *triggers* a new lock."""
    cfg = current_app.config if current_app else {}
    window_sec = window_sec or int(cfg.get("AUTH_THROTTLE_WINDOW_SEC", 60))
    max_fails = max_fails or int(cfg.get("AUTH_THROTTLE_MAX_FAILS", 5))
    lock_sec = lock_sec or int(cfg.get("AUTH_THROTTLE_LOCK_SEC", 300))

    now_dt = datetime.now(timezone.utc)
    now_iso = _now_iso()

    if USE_PG:
        with SessionLocal() as s:
            # Lock row if exists to avoid races
            rec = s.execute(
                sa.select(AuthThrottle).where(AuthThrottle.username ==
                                              username, AuthThrottle.ip == ip).with_for_update()
            ).scalar_one_or_none()

            if rec:
                ws = _parse_iso(rec.window_start)
                fc = int(rec.fail_count or 0)
                lu = _parse_iso(rec.locked_until)

                if not ws or (now_dt - ws).total_seconds() > window_sec:
                    ws = now_dt
                    fc = 0

                fc += 1
                locked_now = False
                locked_until_dt = lu
                if fc >= max_fails:
                    locked_until_dt = now_dt + timedelta(seconds=lock_sec)
                    locked_now = True

                rec.window_start = now_iso
                rec.fail_count = fc
                rec.locked_until = (
                    locked_until_dt.isoformat(
                        timespec="seconds").replace("+00:00", "Z")
                    if locked_until_dt else None
                )
                s.commit()
                return locked_now

            # first failure for this (user,ip)
            s.add(AuthThrottle(
                username=username, ip=ip,
                window_start=now_iso, fail_count=1, locked_until=None
            ))
            s.commit()
            return False

    # --- SQLite legacy path ---
    db = get_db()
    row = db.execute(
        "SELECT window_start, fail_count, locked_until FROM auth_throttle WHERE username=? AND ip=?",
        (username, ip)
    ).fetchone()

    if row:
        ws = _parse_iso(row["window_start"])
        fc = int(row["fail_count"] or 0)
        lu = _parse_iso(row["locked_until"])

        if not ws or (now_dt - ws).total_seconds() > window_sec:
            ws = now_dt
            fc = 0

        fc += 1

        locked_until = lu
        locked_now = False
        if fc >= max_fails:
            locked_until = now_dt + timedelta(seconds=lock_sec)
            locked_now = True

        with db:
            db.execute(
                """UPDATE auth_throttle
                   SET window_start=?, fail_count=?, locked_until=?
                   WHERE username=? AND ip=?""",
                (
                    now_iso,
                    fc,
                    (locked_until.isoformat(timespec="seconds").replace(
                        "+00:00", "Z") if locked_until else None),
                    username, ip,
                ),
            )
        return locked_now

    with db:
        db.execute(
            """INSERT INTO auth_throttle(username, ip, window_start, fail_count, locked_until)
               VALUES (?,?,?,?,?)""",
            (username, ip, now_iso, 1, None),
        )
    return False


def reset(username: str, ip: str):
    """Clears failures & lock for (user,ip) after successful login."""
    if USE_PG:
        with SessionLocal() as s:
            rec = s.execute(
                sa.select(AuthThrottle).where(AuthThrottle.username ==
                                              username, AuthThrottle.ip == ip).with_for_update()
            ).scalar_one_or_none()
            if rec:
                rec.window_start = _now_iso()
                rec.fail_count = 0
                rec.locked_until = None
                s.commit()
        return

    db = get_db()
    with db:
        db.execute(
            """UPDATE auth_throttle
               SET window_start=?, fail_count=0, locked_until=NULL
               WHERE username=? AND ip=?""",
            (_now_iso(), username, ip),
        )
