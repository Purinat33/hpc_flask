# models/payments_store.py
"""
Payments store: keeps payment intents and webhook events and finalizes receipts
transactionally after verified provider success.

Design choices:
- Amounts are stored as integer minor units ("cents") to avoid floats.
- We verify provider webhooks (signature + idempotency) and only then
  update the receipt as 'paid', within a single transaction.
- We record the raw webhook payload for auditing/reconciliation.
"""

from __future__ import annotations
import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from flask import current_app
from models.db import get_db
from models.billing_store import mark_paid

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS payments (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  provider              TEXT NOT NULL,            -- 'stripe' | 'omise' | 'paypal' | 'dummy' ...
  receipt_id            INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  username              TEXT NOT NULL,            -- who pays
  status                TEXT NOT NULL DEFAULT 'pending',  -- pending|succeeded|failed|canceled
  currency              TEXT NOT NULL,
  amount_cents          INTEGER NOT NULL,         -- total in minor units
  external_payment_id   TEXT,                     -- provider's charge/session/payment id
  checkout_url          TEXT,                     -- provider-hosted checkout URL (if any)
  idempotency_key       TEXT,                     -- our key passed to provider when creating
  created_at            TEXT NOT NULL,            -- ISO8601Z
  updated_at            TEXT NOT NULL,            -- ISO8601Z
  UNIQUE(external_payment_id)
);

CREATE TABLE IF NOT EXISTS payment_events (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  provider          TEXT NOT NULL,
  external_event_id TEXT,           -- unique id from provider (if provided)
  payment_id        INTEGER,        -- our local FK -> payments.id (nullable until we can resolve)
  event_type        TEXT NOT NULL,  -- 'payment.succeeded', etc.
  raw               TEXT NOT NULL,  -- raw JSON (as text)
  signature_ok      INTEGER NOT NULL DEFAULT 0,
  received_at       TEXT NOT NULL,  -- ISO8601Z
  UNIQUE(provider, external_event_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_receipt ON payments(receipt_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
"""


def init_payments_schema():
    db = get_db()
    with db:
        for stmt in SCHEMA_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                db.execute(s)


# ---------- helpers ----------

def _to_cents(amount: Decimal) -> int:
    # 2 decimal places rounding half up
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_receipt(receipt_id: int) -> Optional[dict]:
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

    db = get_db()
    now = _now_iso()
    with db:
        pid = db.execute(
            """INSERT INTO payments(provider, receipt_id, username, status, currency, amount_cents,
                                    created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (provider, receipt_id, username, "pending",
             currency, amount_cents, now, now)
        ).lastrowid
    return pid, amount_cents


def attach_provider_checkout(payment_id: int, external_payment_id: str, checkout_url: Optional[str], idempotency_key: Optional[str]) -> None:
    db = get_db()
    with db:
        db.execute(
            "UPDATE payments SET external_payment_id=?, checkout_url=?, idempotency_key=?, updated_at=? WHERE id=?",
            (external_payment_id, checkout_url,
             idempotency_key, _now_iso(), payment_id)
        )


def get_payment_by_external_id(external_payment_id: str) -> Optional[dict]:
    if not external_payment_id:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM payments WHERE external_payment_id=?",
                     (external_payment_id,)).fetchone()
    return dict(row) if row else None


def get_payment(payment_id: int) -> Optional[dict]:
    db = get_db()
    row = db.execute("SELECT * FROM payments WHERE id=?",
                     (payment_id,)).fetchone()
    return dict(row) if row else None


def record_webhook_event(provider: str, external_event_id: Optional[str], event_type: str,
                         raw_payload: dict, signature_ok: bool, payment_id: Optional[int] = None) -> int:
    """
    Store incoming webhook for auditing/idempotency. Returns local event id.
    Duplicate provider+external_event_id is ignored.
    """
    db = get_db()
    now = _now_iso()
    raw_text = json.dumps(raw_payload, ensure_ascii=False,
                          separators=(",", ":"))
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
        # Same external_event_id already recorded -> idempotent
        cur = db.execute(
            "SELECT id FROM payment_events WHERE provider=? AND external_event_id=?",
            (provider, external_event_id)
        )
        row = cur.fetchone()
        return int(row["id"]) if row else 0


def finalize_success_if_amount_matches(external_payment_id: str, amount_cents: int, currency: str, provider: str) -> bool:
    """
    Finish the payment and mark receipt paid if:
      - we can find the local payment by external id
      - currency matches
      - amount exactly matches the receipt total (in cents)
    Returns True if the receipt was marked paid (or already paid), False otherwise.
    """
    db = get_db()
    p = get_payment_by_external_id(external_payment_id)
    if not p:
        return False

    # strict integrity checks
    if p["currency"].upper() != currency.upper():
        return False
    if int(p["amount_cents"]) != int(amount_cents):
        return False

    # in one transaction: update local payment + flip receipt to paid
    with db:
        # idempotent: if already succeeded, return True
        cur = db.execute(
            "SELECT status, receipt_id FROM payments WHERE id=?", (p["id"],))
        row = cur.fetchone()
        if not row:
            return False
        if row["status"] == "succeeded":
            return True

        # mark payment
        db.execute(
            "UPDATE payments SET status='succeeded', updated_at=? WHERE id=?", (_now_iso(), p["id"]))

        # mark receipt
        # method = 'auto:<provider>' and tx_ref = external_payment_id for traceability
        # record how it was paid and the provider reference
        mark_paid(row["receipt_id"],
                  method=f"auto:{provider}", tx_ref=external_payment_id)

    return True
