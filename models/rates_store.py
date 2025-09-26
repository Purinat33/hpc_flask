# models/rates_store.py
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select
from models.base import session_scope
from models.schema import Rate

DEFAULT_RATES = {
    "mu":      {"cpu": 1.0,  "gpu": 5.0,   "mem": 0.5},
    "gov":     {"cpu": 3.0,  "gpu": 10.0,  "mem": 1.0},
    "private": {"cpu": 5.0,  "gpu": 100.0, "mem": 2.0},
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _D(x) -> Decimal:
    # safe conversion avoiding float binary artifacts
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _seed_missing():
    with session_scope() as s:
        existing = {r.tier for r in s.execute(select(Rate)).scalars().all()}
        for tier, r in DEFAULT_RATES.items():
            if tier not in existing:
                s.add(Rate(
                    tier=tier,
                    cpu=_D(r["cpu"]), gpu=_D(r["gpu"]), mem=_D(r["mem"]),
                    updated_at=_now_utc(),
                ))


def load_rates() -> dict:
    _seed_missing()
    with session_scope() as s:
        rows = s.execute(select(Rate)).scalars().all()
        out = DEFAULT_RATES.copy()
        for r in rows:
            out[r.tier] = {"cpu": float(r.cpu), "gpu": float(
                r.gpu), "mem": float(r.mem)}
        return out


def save_rates(rates: dict) -> None:
    clean = {(k or "").lower(): v for k, v in (rates or {}).items()}
    now = _now_utc()
    with session_scope() as s:
        for tier, r in clean.items():
            obj = s.get(Rate, tier)
            if not obj:
                obj = Rate(
                    tier=tier,
                    cpu=_D(r["cpu"]), gpu=_D(r["gpu"]), mem=_D(r["mem"]),
                    updated_at=now,
                )
            else:
                obj.cpu = _D(r["cpu"])
                obj.gpu = _D(r["gpu"])
                obj.mem = _D(r["mem"])
                obj.updated_at = now
            s.add(obj)


def get_rate_for_tier(tier: str) -> dict:
    return load_rates().get((tier or "mu").lower(), DEFAULT_RATES["mu"])
