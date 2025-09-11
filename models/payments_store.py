# models/payments_store.py (Postgres / SQLAlchemy)
from __future__ import annotations
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from models.base import session_scope
from models.schema import Payment, PaymentEvent, Receipt
from models.billing_store import mark_paid


def _to_cents(amount: Decimal) -> int:
    return int((amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100))


def _from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_receipt(receipt_id: int) -> Optional[dict]:
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return None
        return {
            "id": r.id, "username": r.username, "total": float(r.total),
            "status": r.status, "created_at": r.created_at
        }


def create_payment_for_receipt(provider: str, receipt_id: int, username: str, currency: str) -> Tuple[int, int]:
    r = load_receipt(receipt_id)
    if not r:
        raise ValueError("Receipt not found")
    if r["status"] != "pending":
        raise ValueError(
            f"Receipt status is {r['status']}; only 'pending' can be paid")

    amount_cents = _to_cents(Decimal(str(r["total"])))
    with session_scope() as s:
        p = Payment(
            provider=provider, receipt_id=receipt_id, username=username,
            status="pending", currency=currency, amount_cents=amount_cents,
            external_payment_id=None, checkout_url=None, idempotency_key=None,
            created_at=_now_iso(), updated_at=_now_iso(),
        )
        s.add(p)
        s.flush()
        return p.id, amount_cents


def attach_provider_checkout(payment_id: int, external_payment_id: str, checkout_url: Optional[str], idempotency_key: Optional[str]) -> None:
    with session_scope() as s:
        p = s.get(Payment, payment_id)
        if not p:
            return
        p.external_payment_id = external_payment_id
        p.checkout_url = checkout_url
        p.idempotency_key = idempotency_key
        p.updated_at = _now_iso()
        s.add(p)


def get_payment_by_external_id(external_payment_id: str) -> Optional[dict]:
    if not external_payment_id:
        return None
    with session_scope() as s:
        p = s.execute(select(Payment).where(
            Payment.external_payment_id == external_payment_id)).scalars().first()
        if not p:
            return None
        return {c: getattr(p, c) for c in ("id", "provider", "receipt_id", "username", "status", "currency", "amount_cents", "external_payment_id", "checkout_url", "idempotency_key", "created_at", "updated_at")}


def get_payment(payment_id: int) -> Optional[dict]:
    with session_scope() as s:
        p = s.get(Payment, payment_id)
        if not p:
            return None
        return {c: getattr(p, c) for c in ("id", "provider", "receipt_id", "username", "status", "currency", "amount_cents", "external_payment_id", "checkout_url", "idempotency_key", "created_at", "updated_at")}


def record_webhook_event(provider: str, external_event_id: Optional[str], event_type: str,
                         raw_payload: dict, signature_ok: bool, payment_id: Optional[int] = None) -> int:
    now = _now_iso()
    raw_text = json.dumps(raw_payload, ensure_ascii=False,
                          separators=(",", ":"))
    with session_scope() as s:
        try:
            e = PaymentEvent(
                provider=provider, external_event_id=external_event_id, payment_id=payment_id,
                event_type=event_type, raw=raw_text, signature_ok=1 if signature_ok else 0, received_at=now
            )
            s.add(e)
            s.flush()
            return e.id
        except IntegrityError:
            s.rollback()
            row = s.execute(
                select(PaymentEvent.id).where(
                    (PaymentEvent.provider == provider) & (
                        PaymentEvent.external_event_id == external_event_id)
                )
            ).first()
            return int(row[0]) if row else 0


def finalize_success_if_amount_matches(external_payment_id: str, amount_cents: int, currency: str, provider: str) -> bool:
    p = get_payment_by_external_id(external_payment_id)
    if not p:
        return False
    if p["currency"].upper() != currency.upper():
        return False
    if int(p["amount_cents"]) != int(amount_cents):
        return False

    with session_scope() as s:
        payment = s.get(Payment, p["id"])
        if not payment:
            return False
        if payment.status == "succeeded":
            return True
        if payment.status != "pending":
            return False

        rec = s.get(Receipt, payment.receipt_id)
        if not rec or rec.username != payment.username:
            return False

        payment.status = "succeeded"
        payment.updated_at = _now_iso()
        s.add(payment)

    # mark the receipt as paid (separate session; same result)
    mark_paid(payment.receipt_id,
              method=f"auto:{provider}", tx_ref=external_payment_id)
    return True


def get_latest_payment_for_receipt(receipt_id: int) -> Optional[dict]:
    with session_scope() as s:
        p = s.execute(select(Payment).where(Payment.receipt_id == receipt_id).order_by(
            Payment.id.desc()).limit(1)).scalars().first()
        if not p:
            return None
        return {c: getattr(p, c) for c in ("id", "provider", "receipt_id", "username", "status", "currency", "amount_cents", "external_payment_id", "checkout_url", "idempotency_key", "created_at", "updated_at")}
