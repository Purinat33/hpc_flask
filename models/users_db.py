# models/users_db.py
import os
from datetime import datetime, timezone
from typing import Optional, Dict
from werkzeug.security import check_password_hash, generate_password_hash

USE_PG = bool(os.getenv("DATABASE_URL"))

if USE_PG:
    from models.base import init_engine_and_session
    from models.schema import User
    Engine, SessionLocal = init_engine_and_session()
else:
    import sqlite3
    from flask import current_app, g


# ---- Flask integration (kept for compatibility) ----
def init_app(app):
    if not USE_PG:
        app.teardown_appcontext(close_users_db)


def get_users_db():
    if USE_PG:
        raise RuntimeError(
            "get_users_db() should not be used when DATABASE_URL is set")
    if "users_db" not in g:
        db_path = os.getenv("USERS_DB") or os.path.join(
            current_app.instance_path, "users.sqlite3")
        g.users_db = sqlite3.connect(db_path)
        g.users_db.row_factory = sqlite3.Row
        g.users_db.execute("PRAGMA foreign_keys = ON")
    return g.users_db


def close_users_db(_=None):
    if USE_PG:
        return
    db = g.pop("users_db", None)
    if db is not None:
        db.close()


def init_users_db():
    if USE_PG:
        return  # PG schema managed by Alembic
    db = get_users_db()
    db.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','user')),
        created_at TEXT NOT NULL
    )""")
    db.commit()


# ---- API used by controllers/auth.py ----
def verify_password(username: str, plain_password: str) -> bool:
    """
    Return True if `plain_password` is correct for `username`.
    Works for both PG (SQLAlchemy) and legacy SQLite.
    """
    if not username:
        return False

    if USE_PG:
        with SessionLocal() as s:
            u = s.get(User, username)
            if not u:
                return False
            try:
                return check_password_hash(u.password_hash or "", plain_password or "")
            except Exception:
                return False

    # SQLite path
    db = get_users_db()
    row = db.execute(
        "SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return False
    try:
        return check_password_hash(row["password_hash"] or "", plain_password or "")
    except Exception:
        return False


def get_user(username: str) -> Optional[Dict]:
    if USE_PG:
        with SessionLocal() as s:
            u = s.get(User, username)
            if not u:
                return None
            return dict(
                username=u.username,
                password_hash=u.password_hash,
                role=u.role,
                created_at=u.created_at,
            )
    db = get_users_db()
    row = db.execute("SELECT * FROM users WHERE username=?",
                     (username,)).fetchone()
    return dict(row) if row else None


def create_user(username: str, password: str, role: str = "user") -> bool:
    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    if USE_PG:
        with SessionLocal() as s:
            if s.get(User, username):
                return False
            s.add(User(username=username,
                       password_hash=generate_password_hash(password),
                       role=role,
                       created_at=now))
            s.commit()
            return True
    db = get_users_db()
    try:
        with db:
            db.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                (username, generate_password_hash(password), role, now),
            )
        return True
    except Exception:
        return False
