# models/users_db.py (Postgres / SQLAlchemy)
from __future__ import annotations
import re
from typing import Optional, Iterable
from datetime import datetime, timezone
from typing import Optional

from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import select
from models.base import session_scope
from models.schema import User


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_user(username: str) -> Optional[dict]:
    if not username:
        return None
    with session_scope() as s:
        u = s.get(User, username)
        if not u:
            return None
        return {
            "username": u.username,
            "password_hash": u.password_hash,
            "role": u.role,
            "created_at": u.created_at,
        }


def create_user(username: str, password: str, role: str = "user") -> bool:
    if not username or not password:
        return False
    with session_scope() as s:
        if s.get(User, username):
            return False
        s.add(User(
            username=username,
            password_hash=generate_password_hash(password),
            role=role,
            created_at=_now_iso(),
        ))
    return True


def verify_password(username: str, password: str) -> bool:
    if not username:
        return False
    with session_scope() as s:
        u = s.get(User, username)
        return bool(u and check_password_hash(u.password_hash, password))


USERNAME_RX = re.compile(r"^[a-z0-9._-]{3,40}$")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def list_users(limit: int = 1000) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(User.username, User.role, User.created_at).order_by(
                User.username).limit(limit)
        ).all()
        return [{"username": r.username, "role": r.role, "created_at": r.created_at} for r in rows]


def create_user(username: str, password: str, role: str = "user") -> bool:
    if not username or not password or role not in {"user", "admin"}:
        return False
    if not USERNAME_RX.match(username.strip().lower()):
        return False
    with session_scope() as s:
        if s.get(User, username):
            return False
        s.add(User(
            username=username.strip(),
            password_hash=generate_password_hash(password),
            role=role,
            created_at=_now_utc(),    # was ISO string before
        ))
    return True


def update_password(username: str, new_password: str) -> None:
    """Set a new password hash for the given user."""
    if not new_password or len(new_password) < 8:
        raise ValueError("Password too short")
    with session_scope() as s:
        u = s.get(User, username)
        if not u:
            raise LookupError("User not found")
        u.password_hash = generate_password_hash(new_password)
