# models/users_db.py (Postgres / SQLAlchemy)
from __future__ import annotations
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
