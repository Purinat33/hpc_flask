# models/rates_store.py  (DB-backed, PG/SQLite dual-mode)
import os
import json
from datetime import datetime, timezone

USE_PG = bool(os.getenv("DATABASE_URL"))

DEFAULT_RATES = {
    "mu":      {"cpu": 1.0,  "gpu": 5.0,   "mem": 0.5},
    "gov":     {"cpu": 3.0,  "gpu": 10.0,  "mem": 1.0},
    "private": {"cpu": 5.0,  "gpu": 100.0, "mem": 2.0},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


if USE_PG:
    # --- Postgres / SQLAlchemy path ---
    import sqlalchemy as sa
    from models.base import init_engine_and_session
    from models.schema import Rate
    Engine, SessionLocal = init_engine_and_session()
else:
    # --- Legacy SQLite path ---
    from models.db import get_db


# -----------------------------
# Schema init + seeding
# -----------------------------
def _ensure_schema_and_seed():
    """
    On PG: make sure default tiers exist. (Alembic owns the table.)
    On SQLite: create table if needed + seed defaults (legacy behavior).
    """
    if USE_PG:
        with SessionLocal() as s:
            # seed any missing tiers with defaults
            existing = {t for (t,) in s.execute(sa.select(Rate.tier)).all()}
            for tier, r in DEFAULT_RATES.items():
                if tier not in existing:
                    s.add(Rate(
                        tier=tier,
                        cpu=float(r["cpu"]),
                        gpu=float(r["gpu"]),
                        mem=float(r["mem"]),
                        updated_at=_now_iso(),
                    ))
            s.commit()
        return

    # --- SQLite (legacy) ---
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS rates(
          tier TEXT PRIMARY KEY,
          cpu  REAL NOT NULL,
          gpu  REAL NOT NULL,
          mem  REAL NOT NULL,
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    for tier, r in DEFAULT_RATES.items():
        db.execute(
            "INSERT OR IGNORE INTO rates(tier,cpu,gpu,mem) VALUES (?,?,?,?)",
            (tier, r["cpu"], r["gpu"], r["mem"])
        )
    db.commit()


def _maybe_import_legacy_file():
    """One-time import if a legacy RATES_FILE exists (json with tiers)."""
    path = os.environ.get("RATES_FILE")
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        # write into DB
        save_rates({k.lower(): v for k, v in data.items()})
        # optionally rename so we don't re-import
        try:
            os.replace(path, path + ".migrated")
        except Exception:
            pass
    except Exception:
        # ignore import errors; defaults/DB rows still exist
        pass


# -----------------------------
# Public API
# -----------------------------
def load_rates() -> dict:
    _ensure_schema_and_seed()
    _maybe_import_legacy_file()

    if USE_PG:
        with SessionLocal() as s:
            rows = s.execute(
                sa.select(Rate.tier, Rate.cpu, Rate.gpu, Rate.mem)).all()
            out = DEFAULT_RATES.copy()
            for tier, cpu, gpu, mem in rows:
                out[tier] = {"cpu": float(cpu), "gpu": float(
                    gpu), "mem": float(mem)}
            return out

    db = get_db()
    rows = db.execute("SELECT tier, cpu, gpu, mem FROM rates").fetchall()
    out = DEFAULT_RATES.copy()
    for row in rows:
        out[row["tier"]] = {"cpu": float(row["cpu"]),
                            "gpu": float(row["gpu"]),
                            "mem": float(row["mem"])}
    return out


def save_rates(rates: dict) -> None:
    """Upsert all provided tiers atomically."""
    _ensure_schema_and_seed()
    clean = {k.lower(): v for k, v in (rates or {}).items()}

    if USE_PG:
        with SessionLocal() as s:
            for tier, r in clean.items():
                cpu = float(r["cpu"])
                gpu = float(r["gpu"])
                mem = float(r["mem"])
                obj = s.get(Rate, tier)
                if obj:
                    obj.cpu = cpu
                    obj.gpu = gpu
                    obj.mem = mem
                    obj.updated_at = _now_iso()
                else:
                    s.add(Rate(
                        tier=tier, cpu=cpu, gpu=gpu, mem=mem, updated_at=_now_iso()
                    ))
            s.commit()
        return

    db = get_db()
    for tier, r in clean.items():
        cpu = float(r["cpu"])
        gpu = float(r["gpu"])
        mem = float(r["mem"])
        db.execute("""
          INSERT INTO rates(tier,cpu,gpu,mem,updated_at)
          VALUES (?,?,?,?,datetime('now'))
          ON CONFLICT(tier) DO UPDATE SET
            cpu=excluded.cpu, gpu=excluded.gpu, mem=excluded.mem, updated_at=datetime('now')
        """, (tier, cpu, gpu, mem))
    db.commit()


def get_rate_for_tier(tier: str) -> dict:
    return load_rates().get((tier or "mu").lower(), DEFAULT_RATES["mu"])
