# models/payments_store.py
"""
Payments store: keeps payment intents and webhook events and finalizes receipts.
- Amounts stored in integer minor units ("cents") to avoid floats.
- Postgres path uses SQLAlchemy; SQLite path uses the legacy helper.
"""

from __future__ import annotations
import os
import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

USE_PG = bool(os.getenv("DATABASE_URL"))

if USE_PG:
    import sqlalchemy as sa
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
    from models.base import init_engine_and_session
    from models.schema import Payment, PaymentEvent, Receipt
    Engine, SessionLocal = init_engine_and_session()
else:
    # legacy SQLite
    from flask import current_app
    from models.db import get_db
    from models.billing_store import mark_paid  # legacy marks receipt paid


# ---------- helpers ----------
def _to_cents(amount: Decimal) -> int:
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# PG helper
def _pg_row_to_dict(p: Payment) -> dict:
    return dict(
        id=p.id,
        provider=p.provider,
        receipt_id=p.receipt_id,
        username=p.username,
        status=p.status,
        currency=p.currency,
        amount_cents=p.amount_cents,
        external_payment_id=p.external_payment_id,
        checkout_url=p.checkout_url,
        idempotency_key=p.idempotency_key,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


# ---------- schema init (SQLite only) ----------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS payments (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  provider              TEXT NOT NULL,
  receipt_id            INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  username              TEXT NOT NULL,
  status                TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','succeeded','failed','canceled')),
  currency              TEXT NOT NULL CHECK(length(currency)=3),
  amount_cents          INTEGER NOT NULL CHECK(amount_cents >= 0),
  external_payment_id   TEXT,
  checkout_url          TEXT,
  idempotency_key       TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  UNIQUE(external_payment_id)
);

CREATE TABLE IF NOT EXISTS payment_events (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  provider          TEXT NOT NULL,
  external_event_id TEXT,
  payment_id        INTEGER,
  event_type        TEXT NOT NULL,
  raw               TEXT NOT NULL,
  signature_ok      INTEGER NOT NULL DEFAULT 0 CHECK(signature_ok IN (0,1)),
  received_at       TEXT NOT NULL,
  UNIQUE(provider, external_event_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_receipt ON payments(receipt_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_payments_idem
  ON payments(provider, idempotency_key) WHERE idempotency_key IS NOT NULL;
"""


def init_payments_schema():
    if USE_PG:
        return  # PG is managed by Alembic
    db = get_db()
    with db:
        for stmt in SCHEMA_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s)


# ---------- read helpers ----------
def load_receipt(receipt_id: int) -> Optional[dict]:
    if USE_PG:
        with SessionLocal() as s:
            r = s.get(Receipt, receipt_id)
            if not r:
                return None
            return dict(
                id=r.id, username=r.username, start=r.start, end=r.end,
                total=r.total, status=r.status, created_at=r.created_at,
                paid_at=r.paid_at, method=r.method, tx_ref=r.tx_ref
            )
    db = get_db()
    row = db.execute("SELECT * FROM receipts WHERE id=?",
                     (receipt_id,)).fetchone()
    return dict(row) if row else None


# ---------- core API used by controllers/payments.py ----------
def create_payment_for_receipt(provider: str, receipt_id: int, username: str, currency: str) -> Tuple[int, int]:
    """
    Create a local payment intent for a receipt. Returns (payment_id, amount_cents).
    Raises if receipt not found or not pending.
    """
    r = load_receipt(receipt_id)
    if not r:
        raise ValueError("Receipt not found")
    if r["status"] != "pending":
        raise ValueError(
            f"Receipt status is {r['status']}; only 'pending' can be paid")

    amount = Decimal(str(r["total"]))
    amount_cents = _to_cents(amount)
    now = _now_iso()

    if USE_PG:
        with SessionLocal() as s:
            p = Payment(
                provider=provider, receipt_id=receipt_id, username=username,
                status="pending", currency=currency, amount_cents=amount_cents,
                external_payment_id=None, checkout_url=None, idempotency_key=None,
                created_at=now, updated_at=now
            )
            s.add(p)
            s.commit()
            return p.id, amount_cents

    db = get_db()
    with db:
        pid = db.execute(
            """INSERT INTO payments(provider, receipt_id, username, status, currency, amount_cents, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (provider, receipt_id, username, "pending",
             currency, amount_cents, now, now)
        ).lastrowid
    return pid, amount_cents


def attach_provider_checkout(payment_id: int, external_payment_id: str, checkout_url: Optional[str], idempotency_key: Optional[str]) -> None:
    now = _now_iso()
    if USE_PG:
        with SessionLocal() as s:
            p = s.get(Payment, payment_id)
            if not p:
                return
            p.external_payment_id = external_payment_id
            p.checkout_url = checkout_url
            p.idempotency_key = idempotency_key
            p.updated_at = now
            s.commit()
        return

    db = get_db()
    with db:
        db.execute(
            "UPDATE payments SET external_payment_id=?, checkout_url=?, idempotency_key=?, updated_at=? WHERE id=?",
            (external_payment_id, checkout_url, idempotency_key, now, payment_id)
        )


def get_payment_by_external_id(external_payment_id: str) -> Optional[dict]:
    if not external_payment_id:
        return None
    if USE_PG:
        with SessionLocal() as s:
            p = s.execute(
                sa.select(Payment).where(
                    Payment.external_payment_id == external_payment_id)
            ).scalar_one_or_none()
            return _pg_row_to_dict(p) if p else None
    db = get_db()
    row = db.execute("SELECT * FROM payments WHERE external_payment_id=?",
                     (external_payment_id,)).fetchone()
    return dict(row) if row else None


def get_payment(payment_id: int) -> Optional[dict]:
    if USE_PG:
        with SessionLocal() as s:
            p = s.get(Payment, payment_id)
            return _pg_row_to_dict(p) if p else None
    db = get_db()
    row = db.execute("SELECT * FROM payments WHERE id=?",
                     (payment_id,)).fetchone()
    return dict(row) if row else None


def record_webhook_event(provider: str, external_event_id: Optional[str], event_type: str,
                         raw_payload: dict, signature_ok: bool, payment_id: Optional[int] = None) -> int:
    """
    Store incoming webhook for auditing/idempotency. Returns local event id.
    Duplicate provider+external_event_id is tolerated (idempotent).
    """
    now = _now_iso()
    raw_text = json.dumps(raw_payload, ensure_ascii=False,
                          separators=(",", ":"))

    if USE_PG:
        with SessionLocal() as s:
            try:
                ev = PaymentEvent(
                    provider=provider,
                    external_event_id=external_event_id,
                    payment_id=payment_id,
                    event_type=event_type,
                    raw=raw_text,
                    signature_ok=1 if signature_ok else 0,
                    received_at=now,
                )
                s.add(ev)
                s.commit()
                return ev.id
            except IntegrityError:
                s.rollback()
                # conflict on unique (provider, external_event_id)
                row = s.execute(
                    sa.select(PaymentEvent.id)
                    .where(PaymentEvent.provider == provider)
                    .where(PaymentEvent.external_event_id == external_event_id)
                ).scalar_one_or_none()
                return int(row or 0)

    db = get_db()
    try:
        with db:
            eid = db.execute(
                """INSERT INTO payment_events(provider, external_event_id, payment_id, event_type, raw, signature_ok, received_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (provider, external_event_id, payment_id,
                 event_type, raw_text, 1 if signature_ok else 0, now)
            ).lastrowid
        return eid
    except sqlite3.IntegrityError:
        cur = db.execute(
            "SELECT id FROM payment_events WHERE provider=? AND external_event_id=?",
            (provider, external_event_id)
        )
        row = cur.fetchone()
        return int(row["id"]) if row else 0


def finalize_success_if_amount_matches(external_payment_id: str, amount_cents: int, currency: str, provider: str) -> bool:
    """
    Transition payment->succeeded and receipt->paid atomically (PG) or within a transaction (SQLite).
    """
    if USE_PG:
        now = _now_iso()
        with SessionLocal() as s:
            # Lock the payment row to avoid races
            p = s.execute(
                sa.select(Payment).where(Payment.external_payment_id ==
                                         external_payment_id).with_for_update()
            ).scalar_one_or_none()
            if not p:
                return False
            if p.currency.upper() != currency.upper():
                return False
            if int(p.amount_cents) != int(amount_cents):
                return False
            if p.status == "succeeded":
                return True
            if p.status != "pending":
                return False

            r = s.get(Receipt, p.receipt_id)
            if not r or r.username != p.username:
                return False

            # Update both within the same transaction
            p.status = "succeeded"
            p.updated_at = now

            r.status = "paid"
            r.paid_at = now
            r.method = f"auto:{provider}"
            r.tx_ref = external_payment_id

            try:
                s.commit()
                return True
            except SQLAlchemyError:
                s.rollback()
                return False

    # --- SQLite legacy path ---
    p = get_payment_by_external_id(external_payment_id)
    if not p:
        return False
    if p["currency"].upper() != currency.upper():
        return False
    if int(p["amount_cents"]) != int(amount_cents):
        return False

    db = get_db()
    with db:
        row = db.execute(
            "SELECT status, receipt_id, username FROM payments WHERE id=?", (p["id"],)).fetchone()
        if not row:
            return False
        if row["status"] == "succeeded":
            return True
        if row["status"] != "pending":
            return False
        rec = db.execute("SELECT username FROM receipts WHERE id=?",
                         (row["receipt_id"],)).fetchone()
        if not rec or rec["username"] != p["username"]:
            return False
        db.execute(
            "UPDATE payments SET status='succeeded', updated_at=? WHERE id=?", (_now_iso(), p["id"]))
        mark_paid(row["receipt_id"],
                  method=f"auto:{provider}", tx_ref=external_payment_id)
    return True


def get_latest_payment_for_receipt(receipt_id: int) -> Optional[dict]:
    if USE_PG:
        with SessionLocal() as s:
            p = s.execute(
                sa.select(Payment).where(Payment.receipt_id ==
                                         receipt_id).order_by(Payment.id.desc()).limit(1)
            ).scalar_one_or_none()
            return _pg_row_to_dict(p) if p else None
    db = get_db()
    row = db.execute(
        "SELECT * FROM payments WHERE receipt_id=? ORDER BY id DESC LIMIT 1",
        (receipt_id,)
    ).fetchone()
    return dict(row) if row else None
