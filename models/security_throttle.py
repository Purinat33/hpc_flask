# models/security_throttle.py
from datetime import datetime, timedelta
from typing import Optional, Tuple
from flask import current_app
from models.db import get_db

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
    db = get_db()
    with db:
        for stmt in SCHEMA_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s)


def _now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1]
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def get_status(username: str, ip: str) -> dict:
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
        now = datetime.utcnow()
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

    db = get_db()
    now = datetime.utcnow()
    now_iso = _now_iso()

    row = db.execute(
        "SELECT window_start, fail_count, locked_until FROM auth_throttle WHERE username=? AND ip=?",
        (username, ip)
    ).fetchone()

    if row:
        ws = _parse_iso(row["window_start"])
        fc = int(row["fail_count"] or 0)
        lu = _parse_iso(row["locked_until"])

        # reset the window if expired
        if not ws or (now - ws).total_seconds() > window_sec:
            ws = now
            fc = 0

        fc += 1

        locked_until = lu
        locked_now = False
        if fc >= max_fails:
            locked_until = now + timedelta(seconds=lock_sec)
            locked_now = True

        with db:
            db.execute(
                """UPDATE auth_throttle
                   SET window_start=?, fail_count=?, locked_until=?
                   WHERE username=? AND ip=?""",
                (
                    now_iso,
                    fc,
                    (locked_until.isoformat(timespec="seconds") +
                     "Z") if locked_until else None,
                    username, ip,
                ),
            )
        return locked_now

    # first failure for this (user,ip)
    with db:
        db.execute(
            """INSERT INTO auth_throttle(username, ip, window_start, fail_count, locked_until)
               VALUES (?,?,?,?,?)""",
            (username, ip, now_iso, 1, None),
        )
    return False


def reset(username: str, ip: str):
    """Clears failures & lock for (user,ip) after successful login."""
    db = get_db()
    with db:
        db.execute(
            """UPDATE auth_throttle
               SET window_start=?, fail_count=0, locked_until=NULL
               WHERE username=? AND ip=?""",
            (_now_iso(), username, ip),
        )
