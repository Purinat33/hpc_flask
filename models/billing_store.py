# models/billing_store.py (Postgres / SQLAlchemy)
from services.org_info import ORG_INFO
from typing import Tuple
from models.schema import Receipt, Payment, PaymentEvent
from decimal import Decimal
import json
from services.datetimex import now_utc, APP_TZ
from sqlalchemy import select, delete
from zoneinfo import ZoneInfo
from typing import Iterable, Tuple, List
from datetime import date, datetime, time, timezone
import re

from models.base import session_scope
from models.schema import Receipt, ReceiptItem
from models import rates_store
from services.datetimex import now_utc
from calendar import monthrange
import os
from decimal import Decimal, ROUND_HALF_UP, getcontext
getcontext().prec = 28  # safe default


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    # never pass float directly; stringify first to avoid binary artifacts
    return Decimal(str(x or 0))


def _tax_cfg():
    enabled = os.getenv("BILLING_TAX_ENABLED", "0") == "1"
    label = os.getenv("BILLING_TAX_LABEL", "VAT")
    rate_pct = D(os.getenv("BILLING_TAX_RATE", "7.0") or 0)
    inclusive = os.getenv("BILLING_TAX_INCLUSIVE", "0") == "1"
    return enabled, label, rate_pct, inclusive


def _gen_invoice_no(rcpt: Receipt) -> str:
    # Use local month stamp + padded receipt id for readability & uniqueness
    local_start = rcpt.start.astimezone(_tz_from_app())
    yyyymm = local_start.strftime("%Y%m")
    return f"MUAI-INV-{yyyymm}-{rcpt.id:06d}"


def canonical_job_id(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "." in s:
        prefix = s.split(".", 1)[0]
        if re.fullmatch(r"\d+(?:_\d+)?", prefix):
            return prefix
        return s
    return s


def _now_utc() -> datetime:
    return now_utc()


def _local_ym(dt: datetime) -> str:
    return dt.astimezone(_tz_from_app()).strftime("%Y-%m")


def billed_job_ids() -> set[str]:
    with session_scope() as s:
        rows = s.execute(select(ReceiptItem.job_key)).all()
        return {r[0] for r in rows}


def list_receipts(username: str | None = None) -> list[dict]:
    def _money(x: Decimal | None) -> float:
        # UI still expects numbers; convert safely
        return float(D(x).quantize(Decimal("0.01")))

    with session_scope() as s:
        stmt = select(Receipt).order_by(Receipt.id.desc())
        if username:
            stmt = stmt.where(Receipt.username == username)
        rows = s.execute(stmt).scalars().all()
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "username": r.username,
                "start": r.start,
                "end": r.end,
                "total": _money(r.total),
                "status": r.status,
                "created_at": r.created_at,
                "paid_at": r.paid_at,
                "method": r.method,
                "tx_ref": r.tx_ref,
                # NEW:
                "invoice_no": r.invoice_no,
                "approved_by": r.approved_by,
                "approved_at": r.approved_at,
                "pricing_tier": r.pricing_tier,
                "rate_cpu": float(D(r.rate_cpu)),
                "rate_gpu": float(D(r.rate_gpu)),
                "rate_mem": float(D(r.rate_mem)),
                "rates_locked_at": r.rates_locked_at,
                "period_ym": _local_ym(r.start),
                "currency": r.currency or 'THB',
                "subtotal": _money(r.subtotal),
                "tax_label": r.tax_label,
                "tax_rate": float(D(r.tax_rate)),
                "tax_amount": _money(r.tax_amount),
            })
        return out


# models/billing_store.py
def get_receipt_with_items(receipt_id: int) -> tuple[dict, list[dict]]:
    def _money(x: Decimal | None) -> float:
        # UI still expects numbers; convert safely
        return float(D(x).quantize(Decimal("0.01")))

    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return {}, []
        items = s.execute(
            select(ReceiptItem).where(ReceiptItem.receipt_id ==
                                      receipt_id).order_by(ReceiptItem.job_id_display)
        ).scalars().all()
        return (
            {
                "id": r.id, "username": r.username, "start": r.start, "end": r.end,
                "status": r.status, "created_at": r.created_at, "paid_at": r.paid_at,
                "method": r.method, "tx_ref": r.tx_ref,
                "invoice_no": r.invoice_no, "approved_by": r.approved_by, "approved_at": r.approved_at,
                "pricing_tier": r.pricing_tier, "rate_cpu": float(D(r.rate_cpu)),
                "rate_gpu": float(D(r.rate_gpu)), "rate_mem": float(D(r.rate_mem)),
                "rates_locked_at": r.rates_locked_at,

                # NEW fields the template needs
                "currency": r.currency or 'THB',
                "subtotal": _money(r.subtotal),
                "tax_label": r.tax_label,
                "tax_rate": float(D(r.tax_rate)),
                "tax_amount": _money(r.tax_amount),
                "total": _money(r.total),        # keep total as gross
                "tax_inclusive": bool(r.tax_inclusive or 0),
            },
            [
                {
                    "receipt_id": i.receipt_id, "job_key": i.job_key, "job_id_display": i.job_id_display,
                    "cost": _money(i.cost), "cpu_core_hours": float(i.cpu_core_hours or 0),
                    "gpu_hours": float(i.gpu_hours or 0), "mem_gb_hours": float(i.mem_gb_hours or 0),
                } for i in items
            ],
        )


def _tz_from_app() -> ZoneInfo:
    # APP_TZ may be a string ("Asia/Bangkok") or a tzinfo (pytz/zoneinfo).
    tzname = getattr(APP_TZ, "key", None) or getattr(
        APP_TZ, "zone", None) or (APP_TZ if isinstance(APP_TZ, str) else "UTC")
    return ZoneInfo(str(tzname))


def _day_start_utc(d: date) -> datetime:
    # local day start → UTC
    tz = _tz_from_app()
    return datetime.combine(d, time(0, 0, 0), tzinfo=tz).astimezone(timezone.utc)


def _day_end_utc(d: date) -> datetime:
    # local day end → UTC (inclusive boundary)
    tz = _tz_from_app()
    return datetime.combine(d, time(23, 59, 59), tzinfo=tz).astimezone(timezone.utc)


def create_receipt_from_rows(username: str, start: str, end: str, rows: Iterable[dict]) -> Tuple[int, float, list[dict]]:
    now = _now_utc()
    rows = list(rows)
    inserted: List[dict] = []
    total = D(0)

    tier = next((str((r.get("tier") or "")).lower()
                for r in rows if r.get("tier")), "mu")
    snap = rates_store.get_rate_for_tier(tier)

    start_dt_utc = _day_start_utc(date.fromisoformat(start))
    end_dt_utc = _day_end_utc(date.fromisoformat(end))

    with session_scope() as s:
        r = Receipt(
            username=username,
            start=start_dt_utc, end=end_dt_utc,
            status="pending", created_at=now,
            pricing_tier=tier,
            rate_cpu=D(snap["cpu"]), rate_gpu=D(snap["gpu"]), rate_mem=D(snap["mem"]),
            rates_locked_at=now,
            currency="THB",                 # NEW default
            subtotal=D(0),                   # will set below
            tax_label=None, tax_rate=D(0), tax_amount=D(0),
            total=D(0),                      # will set below
        )
        s.add(r)
        s.flush()
        if not r.invoice_no:
            r.invoice_no = _gen_invoice_no(r)  # MUAI-INV-YYYYMM-XXXXXX
        s.add(r)

        for row in rows:
            job_key = canonical_job_id(str(row["JobID"]))
            cost = D(row.get("Cost (฿)", 0))
            item = ReceiptItem(
                receipt_id=r.id, job_key=job_key, job_id_display=str(
                    row["JobID"]),
                cost=cost,
                cpu_core_hours=float(row.get("CPU_Core_Hours", 0.0)),
                gpu_hours=float(row.get("GPU_Hours", 0.0)),
                mem_gb_hours=float(row.get("Mem_GB_Hours_Used", 0.0)),
            )
            s.add(item)
            total += cost
            inserted.append({
                "job_key": job_key, "job_id_display": item.job_id_display,
                "cost": float(cost), "cpu_core_hours": float(item.cpu_core_hours),
                "gpu_hours": float(item.gpu_hours), "mem_gb_hours": float(item.mem_gb_hours),
            })

        # === tax math ===
        subtotal_raw = (total).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)

        tax_enabled, tax_label, tax_rate_pct, tax_inclusive = _tax_cfg()
        tax_rate = tax_rate_pct  # keep percent as Decimal too
        if tax_enabled and tax_rate > 0:
            if tax_inclusive:
                tax_amount = (subtotal_raw - (subtotal_raw / (D(1) + tax_rate/100)))\
                    .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                subtotal = (subtotal_raw -
                            tax_amount).quantize(Decimal("0.01"))
                grand = subtotal_raw
            else:
                tax_amount = (subtotal_raw * (tax_rate/100)
                              ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                subtotal = subtotal_raw
                grand = (subtotal + tax_amount).quantize(Decimal("0.01"))
            r.tax_label = tax_label
            r.tax_rate = tax_rate.quantize(Decimal("0.01"))
            r.tax_amount = tax_amount
            r.tax_inclusive = bool(tax_inclusive)
        else:
            subtotal = subtotal_raw
            grand = subtotal
            r.tax_inclusive = False

        # persist amounts
        r.subtotal = subtotal
        r.total = grand  # **gross** total is the canonical amount
        s.add(r)

    # return grand if you want; existing callers can keep using the 2nd tuple element
    return r.id, float(r.total), inserted


def void_receipt(receipt_id: int):
    with session_scope() as s:
        # ON DELETE CASCADE will drop children if we delete the parent,
        # but old semantics were: delete items, then mark 'void'.
        s.execute(delete(ReceiptItem).where(
            ReceiptItem.receipt_id == receipt_id))
        r = s.get(Receipt, receipt_id)
        if r:
            r.status = "void"
            s.add(r)


def list_billed_items_for_user(username: str, status: str | None = None) -> list[dict]:
    def _money(x: Decimal | None) -> float:
        # UI still expects numbers; convert safely
        return float(D(x).quantize(Decimal("0.01")))

    with session_scope() as s:
        q = select(ReceiptItem, Receipt).join(Receipt, Receipt.id ==
                                              ReceiptItem.receipt_id).where(Receipt.username == username)
        if status in ("pending", "paid"):
            q = q.where(Receipt.status == status)
        q = q.order_by(Receipt.id.desc(), ReceiptItem.job_id_display)
        rows = s.execute(q).all()
        out = []
        for (i, r) in rows:
            out.append({
                "job_id_display": i.job_id_display, "job_key": i.job_key,
                "cost": _money(i.cost), "cpu_core_hours": float(i.cpu_core_hours),
                "gpu_hours": float(i.gpu_hours), "mem_gb_hours": float(i.mem_gb_hours),
                "receipt_id": r.id, "status": r.status, "start": r.start, "end": r.end,
                "created_at": r.created_at, "paid_at": r.paid_at
            })
        return out


def admin_list_receipts(status: str | None = None) -> list[dict]:
    def _money(x: Decimal | None) -> float:
        # UI still expects numbers; convert safely
        return float(D(x).quantize(Decimal("0.01")))

    with session_scope() as s:
        q = select(Receipt)
        if status in ("pending", "paid", "void"):
            q = q.where(Receipt.status == status)
        q = q.order_by(Receipt.created_at.desc(), Receipt.id.desc())

        rows = s.execute(q).scalars().all()
        out: list[dict] = []
        for r in rows:
            out.append({
                "id": r.id,
                "username": r.username,
                "start": r.start,
                "end": r.end,
                "status": r.status,
                "created_at": r.created_at,
                "paid_at": r.paid_at,
                "method": r.method,
                "tx_ref": r.tx_ref,
                "invoice_no": r.invoice_no,
                "approved_by": r.approved_by,
                "approved_at": r.approved_at,
                "pricing_tier": r.pricing_tier,
                "rate_cpu": float(D(r.rate_cpu)),
                "rate_gpu": float(D(r.rate_gpu)),
                "rate_mem": float(D(r.rate_mem)),
                "rates_locked_at": r.rates_locked_at,
                "currency": r.currency or "THB",
                "subtotal": _money(r.subtotal),
                "tax_label": r.tax_label,
                "tax_rate": float(D(r.tax_rate)),
                "tax_amount": _money(r.tax_amount),
                "total": _money(r.total),        # gross
                "period_ym": _local_ym(r.start),
                "tax_inclusive": bool(r.tax_inclusive),
            })
        return out


def mark_receipt_paid(receipt_id: int, actor: str) -> bool:
    """
    Mark a receipt as paid and create:
      - Payment (provider='internal_admin', status='succeeded', amount_cents)
      - PaymentEvent (event_type='admin.marked_paid')
    Idempotent: if the receipt is already 'paid', we'll NOOP (and optionally
    write a small PaymentEvent if no Payment exists).
    """
    now = datetime.now(timezone.utc)
    PROVIDER = "internal_admin"
    CURRENCY = "THB"

    with session_scope() as s:
        # Lock the receipt row (Postgres honors this)
        rcpt = (
            s.query(Receipt)
            .with_for_update()
            .filter(Receipt.id == receipt_id)
            .one_or_none()
        )
        if rcpt is None or rcpt.status == "void":
            return False

        # Helper to check if we already have a success payment for this receipt
        def _has_success_payment() -> bool:
            return s.execute(
                select(Payment.id).where(
                    Payment.receipt_id == receipt_id,
                    Payment.provider == PROVIDER,
                    Payment.status == "succeeded",
                ).limit(1)
            ).first() is not None

        if rcpt.status == "paid":
            if not _has_success_payment():
                # Write a lightweight event so we have an audit trace
                s.add(PaymentEvent(
                    provider=PROVIDER,
                    external_event_id=None,
                    payment_id=None,
                    event_type="admin.mark_paid_noop",
                    raw=json.dumps({
                        "receipt_id": receipt_id,
                        "actor": actor,
                        "reason": "already paid; no matching Payment found"
                    }),
                    signature_ok=1,
                    received_at=now,
                ))
            return True

        # Create a success Payment for the full amount
        total = D(rcpt.total or 0).quantize(Decimal("0.01"))
        amount_cents = int(
            (total * 100).to_integral_value(rounding=ROUND_HALF_UP))

        pay = Payment(
            provider=PROVIDER,
            receipt_id=receipt_id,
            username=rcpt.username,
            status="succeeded",
            currency=CURRENCY,
            amount_cents=amount_cents,
            external_payment_id=None,
            checkout_url=None,
            idempotency_key=None,
            created_at=now,
            updated_at=now,
        )
        s.add(pay)
        s.flush()  # get pay.id

        # Update the Receipt
        rcpt.status = "paid"
        rcpt.paid_at = now
        rcpt.method = PROVIDER          # optional but handy to show in UI
        rcpt.tx_ref = f"payment:{pay.id}"
        rcpt.approved_by = actor
        rcpt.approved_at = now
        if not rcpt.invoice_no:
            rcpt.invoice_no = _gen_invoice_no(rcpt)

        s.add(rcpt)

        # Create a PaymentEvent linked to this payment
        s.add(PaymentEvent(
            provider=PROVIDER,
            external_event_id=None,
            payment_id=pay.id,
            event_type="admin.marked_paid",
            raw=json.dumps({
                "receipt_id": receipt_id,
                "payment_id": pay.id,
                "actor": actor,
                "invoice_no": rcpt.invoice_no,
                "note": "Manual mark paid from admin UI"
            }),
            signature_ok=1,
            received_at=now,
        ))

        return True


def paid_receipts_csv():
    import io
    import csv
    rows = admin_list_receipts(status="paid")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "id", "username", "start", "end",
        "currency", "subtotal", "tax_label", "tax_rate_pct", "tax_amount", "total",
        "status", "created_at", "paid_at", "approved_by", "approved_at",
        "pricing_tier", "rate_cpu", "rate_gpu", "rate_mem", "rates_locked_at",
    ])
    for r in rows:
        w.writerow([
            r["id"], r["username"],
            r["start"].isoformat(), r["end"].isoformat(),
            r.get("currency", "THB"),
            f"{float(r.get('subtotal', 0)):,.2f}",
            r.get("tax_label") or "",
            f"{float(r.get('tax_rate', 0)):,.2f}",
            f"{float(r.get('tax_amount', 0)):,.2f}",
            f"{float(r.get('total', 0)):,.2f}",
            r["status"],
            r["created_at"].isoformat(),
            (r["paid_at"].isoformat().replace(
                "+00:00", "Z") if r["paid_at"] else ""),
            r.get("approved_by", ""),
            (r["approved_at"].isoformat().replace(
                "+00:00", "Z") if r.get("approved_at") else ""),
            r.get("pricing_tier", ""),
            r.get("rate_cpu", ""), r.get(
                "rate_gpu", ""), r.get("rate_mem", ""),
            r["rates_locked_at"].isoformat(),
        ])
    out.seek(0)
    return ("paid_receipts_history.csv", out.read())


def revert_receipt_to_pending(receipt_id: int, actor: str, reason: str | None = None) -> Tuple[bool, str]:
    """
    Revert a PAID receipt back to PENDING.
    - Cancels any internal_admin succeeded Payment(s)
    - Refuses if any non-internal succeeded Payment exists
    - Refuses if downstream locks are set (GL export / eTax / customer-sent / etax_status not draft/none)
    - Clears paid fields on Receipt
    - Keeps invoice_no
    - Emits PaymentEvent(s)
    Returns (ok, message).
    """
    now = datetime.now(timezone.utc)
    PROVIDER_INTERNAL = "internal_admin"

    with session_scope() as s:
        rcpt = (
            s.query(Receipt)
            .with_for_update()
            .filter(Receipt.id == receipt_id)
            .one_or_none()
        )
        if rcpt is None:
            return False, "receipt_not_found"
        if rcpt.status == "void":
            s.add(PaymentEvent(
                provider=PROVIDER_INTERNAL,
                external_event_id=None,
                payment_id=None,
                event_type="admin.revert_noop_void",
                raw=json.dumps({"receipt_id": receipt_id, "actor": actor}),
                signature_ok=1, received_at=now,
            ))
            return False, "already_void"
        if rcpt.status == "pending":
            s.add(PaymentEvent(
                provider=PROVIDER_INTERNAL,
                external_event_id=None,
                payment_id=None,
                event_type="admin.revert_noop_pending",
                raw=json.dumps({"receipt_id": receipt_id, "actor": actor}),
                signature_ok=1, received_at=now,
            ))
            return True, "already_pending"

        # NEW: block if the payment month is closed
        from services.gl_posting import is_period_closed, reverse_receipt_postings
        paid_dt = rcpt.paid_at or rcpt.created_at
        if paid_dt and is_period_closed(paid_dt):
            s.add(PaymentEvent(provider=PROVIDER_INTERNAL, external_event_id=None, payment_id=None,
                               event_type="admin.revert_blocked_closed_period",
                               raw=json.dumps(
                                   {"receipt_id": receipt_id, "actor": actor, "reason": reason}),
                               signature_ok=1, received_at=now))
            return False, "period_closed"

        # --- NEW: downstream lock guard (optional fields are read if present) ---
        exported_to_gl_at = getattr(rcpt, "exported_to_gl_at", None)
        etax_submitted_at = getattr(rcpt, "etax_submitted_at", None)
        etax_status = str(getattr(rcpt, "etax_status", "") or "").lower()
        customer_sent_at = getattr(rcpt, "customer_sent_at", None)
        downstream_locked = (
            (exported_to_gl_at is not None)
            or (etax_submitted_at is not None)
            or (customer_sent_at is not None)
            or (etax_status not in {"", "draft", "none"})
        )
        if downstream_locked:
            s.add(PaymentEvent(
                provider=PROVIDER_INTERNAL,
                external_event_id=None,
                payment_id=None,
                event_type="admin.revert_blocked_downstream_lock",
                raw=json.dumps({
                    "receipt_id": receipt_id,
                    "actor": actor,
                    "reason": reason,
                    "exported_to_gl_at": (exported_to_gl_at.isoformat().replace("+00:00", "Z") if exported_to_gl_at else None),
                    "etax_submitted_at": (etax_submitted_at.isoformat().replace("+00:00", "Z") if etax_submitted_at else None),
                    "etax_status": etax_status or None,
                    "customer_sent_at": (customer_sent_at.isoformat().replace("+00:00", "Z") if customer_sent_at else None),
                }),
                signature_ok=1, received_at=now,
            ))
            return False, "downstream_locks_present"
        # --- /NEW ---

        # must be 'paid' here
        succeeded = s.execute(
            select(Payment).where(
                Payment.receipt_id == receipt_id,
                Payment.status == "succeeded",
            )
        ).scalars().all()

        # Disallow revert if there is a real external collection
        external_succeeded = [
            p for p in succeeded if p.provider != PROVIDER_INTERNAL]
        if external_succeeded:
            s.add(PaymentEvent(
                provider=PROVIDER_INTERNAL,
                external_event_id=None,
                payment_id=None,
                event_type="admin.revert_blocked_external_payment",
                raw=json.dumps({
                    "receipt_id": receipt_id,
                    "actor": actor,
                    "reason": reason,
                    "external_providers": sorted({p.provider for p in external_succeeded}),
                    "payment_ids": [p.id for p in external_succeeded],
                }),
                signature_ok=1, received_at=now,
            ))
            return False, "has_external_succeeded_payment"

        # Cancel internal_admin succeeded payments (if any)
        for p in succeeded:
            p.status = "canceled"
            p.updated_at = now
            s.add(p)
            s.add(PaymentEvent(
                provider=PROVIDER_INTERNAL,
                external_event_id=None,
                payment_id=p.id,
                event_type="admin.payment_canceled",
                raw=json.dumps({
                    "receipt_id": receipt_id,
                    "payment_id": p.id,
                    "amount_cents": p.amount_cents,
                    "currency": p.currency,
                    "actor": actor,
                    "reason": reason,
                }),
                signature_ok=1, received_at=now,
            ))

        # Revert receipt fields
        prev = {
            "paid_at": (rcpt.paid_at.isoformat().replace("+00:00", "Z") if rcpt.paid_at else None),
            "method": rcpt.method,
            "tx_ref": rcpt.tx_ref,
            "approved_by": rcpt.approved_by,
            "approved_at": (rcpt.approved_at.isoformat().replace("+00:00", "Z") if rcpt.approved_at else None),
            "invoice_no": rcpt.invoice_no,
        }
        rcpt.status = "pending"
        rcpt.paid_at = None
        rcpt.method = None
        rcpt.tx_ref = None
        rcpt.approved_by = None
        rcpt.approved_at = None
        s.add(rcpt)

        s.add(PaymentEvent(
            provider=PROVIDER_INTERNAL,
            external_event_id=None,
            payment_id=None,
            event_type="admin.receipt_reverted_to_pending",
            raw=json.dumps({
                "receipt_id": receipt_id,
                "actor": actor,
                "reason": reason,
                "previous": prev,
            }),
            signature_ok=1, received_at=now,
        ))
        try:
            reverse_receipt_postings(receipt_id, actor, kinds=("payment",))
        except Exception:
            pass

        return True, "ok"


def _month_bounds_local_utc(y: int, m: int) -> tuple[datetime, datetime]:
    first = date(y, m, 1)
    last = date(y, m, monthrange(y, m)[1])
    return _day_start_utc(first), _day_end_utc(last)


def bulk_void_pending_invoices_for_month(year: int, month: int, actor: str, reason: str | None = None) -> tuple[int, int, list[int]]:
    start_utc, end_utc = _month_bounds_local_utc(year, month)
    voided = 0
    skipped = 0
    voided_ids: list[int] = []
    now = datetime.now(timezone.utc)

    with session_scope() as s:
        q = (
            s.query(Receipt)
             .with_for_update(of=Receipt)
             .filter(Receipt.start >= start_utc, Receipt.end <= end_utc)
        )
        for r in q.all():
            if r.status == "pending":
                s.execute(delete(ReceiptItem).where(
                    ReceiptItem.receipt_id == r.id))
                r.status = "void"
                s.add(r)
                s.add(PaymentEvent(
                    provider="internal_admin",
                    external_event_id=None, payment_id=None,
                    event_type="admin.receipt_voided_pending",
                    raw=json.dumps({"receipt_id": r.id, "username": r.username,
                                   "actor": actor, "reason": reason, "year": year, "month": month}),
                    signature_ok=1, received_at=now,
                ))
                voided += 1
                voided_ids.append(r.id)
            else:
                skipped += 1

        s.add(PaymentEvent(
            provider="internal_admin",
            external_event_id=None, payment_id=None,
            event_type="admin.bulk_void_month_completed",
            raw=json.dumps({"year": year, "month": month, "actor": actor,
                           "reason": reason, "voided": voided, "skipped": skipped}),
            signature_ok=1, received_at=now,
        ))

    return voided, skipped, voided_ids


def build_etax_payload(receipt_id: int) -> dict:
    """Return a stable, compliance-ready JSON snapshot for this receipt."""
    rec, items = get_receipt_with_items(receipt_id)
    if not rec:
        return {}

    org = ORG_INFO() or {}
    # aggregate lines exactly like the PDF
    cpu_qty = sum(i["cpu_core_hours"] for i in items)
    gpu_qty = sum(i["gpu_hours"] for i in items)
    mem_qty = sum(i["mem_gb_hours"] for i in items)

    cpu_amt = round(cpu_qty * rec["rate_cpu"], 2)
    gpu_amt = round(gpu_qty * rec["rate_gpu"], 2)
    mem_amt = round(mem_qty * rec["rate_mem"], 2)

    doc_no = rec.get("invoice_no")

    payload = {
        "version": "etax-export-1",
        "document": {
            "kind": "TAX_INVOICE",               # consumer can map to RD doc type
            "number": doc_no,
            "issue_date": (rec["created_at"].isoformat() if rec.get("created_at") else None),
            "currency": rec.get("currency", "THB"),
            "tax": {
                "label": rec.get("tax_label") or "VAT",
                "rate_pct": rec.get("tax_rate", 0.0),
                "amount": rec.get("tax_amount", 0.0),
                "inclusive": bool(rec.get("tax_inclusive")),
            },
            "amounts": {
                "subtotal": rec.get("subtotal", 0.0),
                "total": rec.get("total", 0.0),
            },
            "period": {
                "start": rec["start"].isoformat(),
                "end": rec["end"].isoformat(),
            },
            "status": rec.get("status"),
        },
        "seller": {
            "name": org.get("name"),
            "tax_id": org.get("tax_id"),              # RD needs this
            # optional; "0" for HQ
            "branch": org.get("branch_no", '0'),
            "address": {
                "line1": org.get("address_line1"),
                "line2": org.get("address_line2"),
                "city": org.get("city"),
                "postcode": org.get("postcode"),
                "country": org.get("country"),
            },
            "email": org.get("email"),
            "phone": org.get("phone"),
        },
        "buyer": {
            # we only store username today
            "code": rec.get("username"),
            # leave tax_id/email/company to be enriched by compliance ops, if needed
        },
        "lines": [
            {"sku": "CPU", "description": "CPU core-hours",
             "quantity": round(cpu_qty, 2), "unit": "core-hour",
             "unit_price": rec["rate_cpu"], "amount": round(cpu_amt, 2)},
            {"sku": "GPU", "description": "GPU hours",
             "quantity": round(gpu_qty, 2), "unit": "hour",
             "unit_price": rec["rate_gpu"], "amount": round(gpu_amt, 2)},
            {"sku": "MEM", "description": "Memory GB-hours (used)",
             "quantity": round(mem_qty, 2), "unit": "GB-hour",
             "unit_price": rec["rate_mem"], "amount": round(mem_amt, 2)},
        ],
        "meta": {
            "receipt_id": rec["id"],
            "pricing_tier": rec.get("pricing_tier"),
            "rates_locked_at": rec.get("rates_locked_at").isoformat() if rec.get("rates_locked_at") else None,
        }
    }
    return payload
