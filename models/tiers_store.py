# models/tiers_store.py
from datetime import datetime, timezone
from sqlalchemy import select, delete
from typing import Dict, Iterable
from models.base import session_scope
from models.schema import UserTierOverride


def _now():
    return datetime.now(timezone.utc)


def load_overrides_dict() -> Dict[str, str]:
    """Return {username_lower: tier} for fast lookups."""
    with session_scope() as s:
        rows = s.execute(select(UserTierOverride)).scalars().all()
        return {r.username.strip().lower(): r.tier for r in rows}


def upsert_override(username: str, tier: str) -> None:
    u = (username or "").strip()
    if not u or tier not in {"mu", "gov", "private"}:
        return
    with session_scope() as s:
        row = s.get(UserTierOverride, u)
        if row:
            row.tier = tier
            row.updated_at = _now()
            s.add(row)
        else:
            s.add(UserTierOverride(username=u, tier=tier, updated_at=_now()))


def bulk_save(overrides: Iterable[tuple[str, str]]) -> None:
    with session_scope() as s:
        for (u, t) in overrides:
            u = (u or "").strip()
            if not u or t not in {"mu", "gov", "private"}:
                continue
            row = s.get(UserTierOverride, u)
            if row:
                row.tier = t
                row.updated_at = _now()
            else:
                s.add(UserTierOverride(username=u, tier=t, updated_at=_now()))


def clear_override(username: str) -> None:
    with session_scope() as s:
        s.execute(delete(UserTierOverride).where(
            UserTierOverride.username == username))
