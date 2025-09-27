# services/gl_posting.py
from __future__ import annotations
from datetime import datetime, timezone
from typing import Iterable, Tuple
from models.audit_store import audit
from models.base import session_scope
from models.schema import Receipt
from models.gl import AccountingPeriod, JournalBatch, GLEntry
from services.accounting import _acc, _ACC
from models.billing_store import _tax_cfg
from sqlalchemy import select, func


def _split_net_vat(gross: float) -> tuple[float, float]:
    enabled, _label, rate_pct, _inclusive = _tax_cfg()
    r = float(rate_pct or 0.0) / 100.0
    if not enabled or r <= 0 or gross <= 0:
        return round(gross, 2), 0.0
    net = round(gross / (1.0 + r), 2)
    vat = round(gross - net, 2)
    return net, vat


def post_service_accrual_for_receipt(receipt_id: int, actor: str) -> bool:
    """
    Post service-month revenue into the month of r.end (or created_at/start fallback):
      Dr 1150 Contract Asset (net)
      Cr 4000 Service Revenue   (net)
    Idempotent per receipt_id.
    Refuses if the target month is closed.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            audit("gl.accrual.receipt.blocked", target_type="receipt", target_id=str(receipt_id),
                  status=404, outcome="blocked", extra={"reason": "not_found"})
            return False

        service_dt = r.end or r.created_at or r.start
        if not service_dt:
            audit("gl.accrual.receipt.blocked", target_type="receipt", target_id=str(r.id),
                  status=409, outcome="blocked", extra={"reason": "no_service_date"})
            return False

        if is_period_closed(service_dt):
            y, m = _ym(service_dt)
            audit("gl.accrual.receipt.blocked", target_type="receipt", target_id=str(r.id),
                  status=409, outcome="blocked",
                  extra={"reason": "period_closed", "period": f"{y}-{m:02d}"})
            return False

        y, m = _ym(service_dt)

        # idempotency: have we already posted an accrual for this receipt?
        exists = s.execute(
            select(JournalBatch.id).where(
                JournalBatch.source == "billing",
                JournalBatch.source_ref == f"R{r.id}",
                JournalBatch.kind == "accrual",
            ).limit(1)
        ).first()
        if exists:
            y, m = _ym(service_dt)
            audit("gl.accrual.receipt.noop", target_type="receipt", target_id=str(r.id),
                  status=304, outcome="noop", extra={"idempotent": True, "period": f"{y}-{m:02d}"})
            return True

        gross = float(r.total or 0.0)
        net, _vat = _split_net_vat(gross)
        if net <= 0:
            y, m = _ym(service_dt)
            audit("gl.accrual.receipt.noop", target_type="receipt", target_id=str(r.id),
                  status=304, outcome="noop", extra={"reason": "zero_net", "period": f"{y}-{m:02d}"})
            return True  # nothing to accrue

        b = JournalBatch(
            source="billing", source_ref=f"R{r.id}", kind="accrual",
            posted_at=now, posted_by=actor, period_year=y, period_month=m
        )
        s.add(b)
        s.flush()

        d_iso = service_dt
        memo = f"Revenue recognized for {r.username} (service period)"
        ref = f"R{r.id}"

        s.add(GLEntry(batch_id=b.id, date=d_iso, ref=ref, memo=memo,
                      account_id=_acc("Contract Asset (Unbilled A/R)"),
                      account_name=_ACC[_acc(
                          "Contract Asset (Unbilled A/R)")]["name"],
                      account_type=_ACC[_acc(
                          "Contract Asset (Unbilled A/R)")]["type"],
                      debit=net, credit=0, receipt_id=r.id))
        s.add(GLEntry(batch_id=b.id, date=d_iso, ref=ref, memo=memo,
                      account_id=_acc("Service Revenue"),
                      account_name=_ACC[_acc("Service Revenue")]["name"],
                      account_type=_ACC[_acc("Service Revenue")]["type"],
                      debit=0, credit=net, receipt_id=r.id))
        audit("gl.accrual.receipt.posted", target_type="receipt", target_id=str(r.id),
              status=200, outcome="success",
              extra={
                  "period": f"{y}-{m:02d}", "effective_date": service_dt.isoformat(),
                  "batch_id": b.id, "lines": 2, "net": net
        })
        return True


def post_service_accruals_for_period(year: int, month: int, actor: str) -> int:
    """
    Bulk-accrue all receipts whose service period END falls inside (year,month).
    Safe to run multiple times; skips if already posted or if period closed.
    """
    if is_period_closed(datetime(year, month, 1, tzinfo=timezone.utc)):
        audit("gl.accrual.period.blocked", target_type="period", target_id=f"{year}-{month:02d}",
              status=409, outcome="blocked", extra={"reason": "period_closed"})
        return 0
    from calendar import monthrange
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    last = datetime(year, month, monthrange(year, month)
                    [1], 23, 59, 59, tzinfo=timezone.utc)

    created = 0
    skipped = 0
    with session_scope() as s:
        # pull candidate receipts by service end
        rs = s.execute(
            select(Receipt.id).where(Receipt.end >= first, Receipt.end <= last)
        ).scalars().all()
    for rid in rs:
        ok = post_service_accrual_for_receipt(rid, actor)
        created += 1 if ok else 0
        skipped += 0 if ok else 1
    audit("gl.accrual.period.summary", target_type="period", target_id=f"{year}-{month:02d}",
          status=200 if skipped == 0 else 207, outcome="success" if skipped == 0 else "partial",
          extra={"created": created, "skipped": skipped})
    return created


def bootstrap_periods(actor: str) -> int:
    """
    Ensure AccountingPeriod rows exist for every month seen in receipts
    (service end, invoice created_at, and paid_at). Leaves status=open.
    Returns number of periods created.
    """
    created = 0
    with session_scope() as s:
        # months from end/created_at/paid_at
        months = set()
        for col in (Receipt.end, Receipt.created_at, Receipt.paid_at):
            rows = s.execute(select(func.date_trunc('month', col)).where(
                col.isnot(None)).distinct()).all()
            for (dt,) in rows:
                if dt:
                    months.add((dt.year, dt.month))
        for (y, m) in months:
            p = s.execute(select(AccountingPeriod).where(
                AccountingPeriod.year == y, AccountingPeriod.month == m
            )).scalars().one_or_none()
            if not p:
                s.add(AccountingPeriod(year=y, month=m, status="open",
                                       opened_at=datetime.now(timezone.utc),
                                       opened_by=actor))
                created += 1
    return created


def _ym(dt: datetime) -> Tuple[int, int]:
    # keep simple; periods are calendar months UTC here
    local = dt.astimezone(timezone.utc)
    return local.year, local.month


def _ensure_open_period(y: int, m: int, actor: str):
    with session_scope() as s:
        p = s.execute(
            select(AccountingPeriod).where(
                AccountingPeriod.year == y, AccountingPeriod.month == m)
        ).scalars().one_or_none()
        if not p:
            p = AccountingPeriod(year=y, month=m, status="open", opened_at=datetime.now(
                timezone.utc), opened_by=actor)
            s.add(p)
        return p.status


def is_period_closed(dt: datetime) -> bool:
    y, m = _ym(dt)
    with session_scope() as s:
        p = s.execute(
            select(AccountingPeriod.status).where(
                AccountingPeriod.year == y, AccountingPeriod.month == m)
        ).scalar_one_or_none()
        return (p == "closed")


def post_receipt_issued(receipt_id: int, actor: str) -> bool:
    """
    Dr 1100 A/R (gross); Cr 4000 Revenue (net); Cr 2100 VAT Output (vat, if any).
    Idempotent on (source='billing', source_ref=f'R{rid}', kind='issue').
    """
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            audit("gl.issue.blocked", target_type="receipt", target_id=str(receipt_id),
                  status=404, outcome="blocked", extra={"reason": "not_found"})
            return False

        eff_dt = (r.created_at or r.start or r.end)
        y, m = _ym(r.created_at or r.start)

        if is_period_closed(r.created_at or r.start):
            audit("gl.issue.blocked", target_type="receipt", target_id=str(r.id),
                  status=409, outcome="blocked",
                  extra={"reason": "period_closed", "period": f"{y}-{m:02d}"})
            return False  # conservative: don't post into closed period
        # idempotency
        exists = s.execute(
            select(JournalBatch.id).where(
                JournalBatch.source == "billing",
                JournalBatch.source_ref == f"R{r.id}",
                JournalBatch.kind == "issue",
            ).limit(1)
        ).first()
        if exists:
            audit("gl.issue.noop", target_type="receipt", target_id=str(r.id),
                  status=304, outcome="noop",
                  extra={"idempotent": True, "period": f"{y}-{m:02d}"})
            return True

        # amounts (gross->net,vat)
        gross = float(r.total or 0)
        if gross <= 0:
            audit("gl.issue.noop", target_type="receipt", target_id=str(r.id),
                  status=304, outcome="noop",
                  extra={"reason": "zero_amount", "period": f"{y}-{m:02d}"})
            return True
        enabled, _label, rate_pct, _inclusive = _tax_cfg()
        rrate = float(rate_pct or 0) / 100.0
        net = round(gross / (1.0 + rrate),
                    2) if (enabled and rrate > 0) else gross
        vat = round(gross - net, 2) if (enabled and rrate > 0) else 0.0

        b = JournalBatch(
            source="billing", source_ref=f"R{r.id}", kind="issue",
            posted_at=now, posted_by=actor,
            period_year=y, period_month=m,
        )
        s.add(b)
        s.flush()

        d_iso = (r.created_at or r.start or r.end)
        memo = f"Receipt issued for {r.username}"
        ref = f"R{r.id}"

        s.add(GLEntry(batch_id=b.id, date=d_iso, ref=ref, memo=memo,
                      account_id=_acc("Accounts Receivable"),
                      account_name=_ACC[_acc("Accounts Receivable")]["name"],
                      account_type=_ACC[_acc("Accounts Receivable")]["type"],
                      debit=gross, credit=0, receipt_id=r.id))
        s.add(GLEntry(batch_id=b.id, date=d_iso, ref=ref, memo=memo,
                      account_id=_acc("Service Revenue"),
                      account_name=_ACC[_acc("Service Revenue")]["name"],
                      account_type=_ACC[_acc("Service Revenue")]["type"],
                      debit=0, credit=net, receipt_id=r.id))
        if vat > 0:
            s.add(GLEntry(batch_id=b.id, date=d_iso, ref=ref, memo=memo,
                          account_id=_acc("VAT Output Payable"),
                          account_name=_ACC[_acc(
                              "VAT Output Payable")]["name"],
                          account_type=_ACC[_acc(
                              "VAT Output Payable")]["type"],
                          debit=0, credit=vat, receipt_id=r.id))
        audit("gl.issue.posted", target_type="receipt", target_id=str(r.id),
              status=200, outcome="success",
              extra={
                  "period": f"{y}-{m:02d}",
                  "effective_date": (eff_dt and eff_dt.isoformat()),
                  "batch_id": b.id, "lines": 2 + (1 if vat > 0 else 0),
                  "gross": gross, "net": net, "vat": vat, "idempotent": False
        })
        return True


def post_receipt_paid(receipt_id: int, actor: str) -> bool:
    """
    Dr 1000 Cash; Cr 1100 A/R (gross). Idempotent per receipt.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r or r.status != "paid" or not r.paid_at:
            audit("gl.payment.blocked", target_type="receipt", target_id=str(receipt_id),
                  status=409, outcome="blocked",
                  extra={"reason": "not_paid_or_missing_paid_at"})
            return False
        if is_period_closed(r.paid_at):
            y, m = _ym(r.paid_at)
            audit("gl.payment.blocked", target_type="receipt", target_id=str(r.id),
                  status=409, outcome="blocked",
                  extra={"reason": "period_closed", "period": f"{y}-{m:02d}"})
            return False
        y, m = _ym(r.paid_at)
        exists = s.execute(
            select(JournalBatch.id).where(
                JournalBatch.source == "billing",
                JournalBatch.source_ref == f"R{r.id}",
                JournalBatch.kind == "payment",
            ).limit(1)
        ).first()
        if exists:
            audit("gl.payment.noop", target_type="receipt", target_id=str(r.id),
                  status=304, outcome="noop",
                  extra={"idempotent": True, "period": f"{y}-{m:02d}"})
            return True

        gross = float(r.total or 0)
        if gross <= 0:
            audit("gl.payment.noop", target_type="receipt", target_id=str(r.id),
                  status=304, outcome="noop",
                  extra={"reason": "zero_amount", "period": f"{y}-{m:02d}"})
            return True

        b = JournalBatch(
            source="billing", source_ref=f"R{r.id}", kind="payment",
            posted_at=now, posted_by=actor,
            period_year=y, period_month=m,
        )
        s.add(b)
        s.flush()

        d_iso = r.paid_at
        memo = f"Receipt paid by {r.username}"
        ref = f"R{r.id}"
        # Cash
        s.add(GLEntry(batch_id=b.id, date=d_iso, ref=ref, memo=memo,
                      account_id=_acc("Cash/Bank"),
                      account_name=_ACC[_acc("Cash/Bank")]["name"],
                      account_type=_ACC[_acc("Cash/Bank")]["type"],
                      debit=gross, credit=0, receipt_id=r.id))
        # AR
        s.add(GLEntry(batch_id=b.id, date=d_iso, ref=ref, memo=memo,
                      account_id=_acc("Accounts Receivable"),
                      account_name=_ACC[_acc("Accounts Receivable")]["name"],
                      account_type=_ACC[_acc("Accounts Receivable")]["type"],
                      debit=0, credit=gross, receipt_id=r.id))
        audit("gl.payment.posted", target_type="receipt", target_id=str(r.id),
              status=200, outcome="success",
              extra={
                  "period": f"{y}-{m:02d}", "effective_date": r.paid_at.isoformat(),
                  "batch_id": b.id, "lines": 2, "gross": gross, "idempotent": False
        })
        return True


def reverse_receipt_postings(receipt_id: int, actor: str, kinds: Iterable[str] = ("payment",)) -> int:
    """
    Create reversal batches for existing 'issue'/'payment' postings of a receipt.
    Reversal date = now (must be in an open period).
    Returns number of reversal batches created.
    """
    now = datetime.now(timezone.utc)
    created = 0
    with session_scope() as s:
        # find batches to reverse
        batches = s.execute(
            select(JournalBatch).where(
                JournalBatch.source == "billing",
                JournalBatch.source_ref == f"R{receipt_id}",
                JournalBatch.kind.in_(list(kinds))
            )
        ).scalars().all()

        if not batches:
            audit("gl.reverse.noop", target_type="receipt", target_id=str(receipt_id),
                  status=304, outcome="noop", extra={"reason": "no_batches"})
            return 0

        y, m = _ym(now)
        status = _ensure_open_period(y, m, actor)
        if status != "open":
            audit("gl.reverse.blocked", target_type="receipt", target_id=str(receipt_id),
                  status=409, outcome="blocked", extra={"reason": "current_period_not_open"})
            return 0

        for b in batches:
            # fetch lines
            lines = s.execute(select(GLEntry).where(
                GLEntry.batch_id == b.id)).scalars().all()
            if not lines:
                continue
            # create reversal batch
            rb = JournalBatch(
                source="billing", source_ref=b.source_ref, kind="reversal",
                posted_at=now, posted_by=actor,
                period_year=y, period_month=m,
            )
            s.add(rb)
            s.flush()
            for ln in lines:
                s.add(GLEntry(
                    batch_id=rb.id, date=now, ref=(ln.ref or ""), memo=f"Reversal of {b.kind}: {ln.memo or ''}",
                    account_id=ln.account_id, account_name=ln.account_name, account_type=ln.account_type,
                    debit=float(ln.credit or 0), credit=float(ln.debit or 0),
                    receipt_id=ln.receipt_id,
                ))
            created += 1
            audit("gl.reverse.posted", target_type="batch", target_id=str(b.id),
                  status=200, outcome="success",
                  extra={"reversal_batch_id": rb.id, "period": f"{y}-{m:02d}", "lines": len(lines)})
    return created


def close_period(year: int, month: int, actor: str) -> bool:
    """
    Close an open period by zeroing INCOME and EXPENSE into 3000 Retained Earnings.
    Generates a single 'closing' batch. No effect if already closed.
    """
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        p = s.execute(
            select(AccountingPeriod).where(AccountingPeriod.year ==
                                           year, AccountingPeriod.month == month)
        ).scalars().one_or_none()
        if not p:
            p = AccountingPeriod(year=year, month=month,
                                 status="open", opened_at=now, opened_by=actor)
            s.add(p)
            s.flush()
        if p.status == "closed":
            audit("period.close.noop", target_type="period", target_id=f"{year}-{month:02d}",
                  status=304, outcome="noop")
            return True

        # aggregate balances from posted GL (exclude prior 'closing')
        lines = s.execute(
            select(GLEntry).join(JournalBatch, GLEntry.batch_id == JournalBatch.id).where(
                JournalBatch.period_year == year,
                JournalBatch.period_month == month,
                JournalBatch.kind != "closing"
            )
        ).scalars().all()

        if not lines:
            # still allow closing an empty period
            p.status = "closed"
            p.closed_at = now
            p.closed_by = actor
            s.add(p)
            return True

        # compute balances by account type
        sums = {}
        for ln in lines:
            key = ln.account_id
            sums.setdefault(key, {"name": ln.account_name,
                            "type": ln.account_type, "dr": 0.0, "cr": 0.0})
            sums[key]["dr"] += float(ln.debit or 0)
            sums[key]["cr"] += float(ln.credit or 0)

        # closing batch
        cb = JournalBatch(
            source="billing", source_ref=f"CLOSE-{year}-{month:02d}", kind="closing",
            posted_at=now, posted_by=actor, period_year=year, period_month=month
        )
        s.add(cb)
        s.flush()

        re_acct = _acc("Retained Earnings")
        re_name = _ACC[re_acct]["name"]
        re_type = _ACC[re_acct]["type"]
        total_to_re = 0.0

        for acct, agg in sums.items():
            t = agg["type"]
            bal = (agg["cr"] - agg["dr"]) if t in ("LIABILITY",
                                                   "EQUITY", "INCOME") else (agg["dr"] - agg["cr"])
            if t == "INCOME" and abs(bal) > 0.005:
                # income has credit balance → debit it to zero
                s.add(GLEntry(batch_id=cb.id, date=now, ref=f"CL-{year}{month:02d}",
                              memo="Close INCOME to Retained Earnings",
                              account_id=acct, account_name=agg["name"], account_type=t,
                              debit=abs(bal), credit=0))
                total_to_re += abs(bal)  # RE will be credited
            if t == "EXPENSE" and abs(bal) > 0.005:
                # expense has debit balance → credit it to zero
                s.add(GLEntry(batch_id=cb.id, date=now, ref=f"CL-{year}{month:02d}",
                              memo="Close EXPENSE to Retained Earnings",
                              account_id=acct, account_name=agg["name"], account_type=t,
                              debit=0, credit=abs(bal)))
                total_to_re -= abs(bal)  # RE will be debited

        # offset to retained earnings
        if abs(total_to_re) > 0.005:
            if total_to_re > 0:
                # net income → credit RE
                s.add(GLEntry(batch_id=cb.id, date=now, ref=f"CL-{year}{month:02d}",
                              memo="Close to Retained Earnings",
                              account_id=re_acct, account_name=re_name, account_type=re_type,
                              debit=0, credit=abs(total_to_re)))
            else:
                # net loss → debit RE
                s.add(GLEntry(batch_id=cb.id, date=now, ref=f"CL-{year}{month:02d}",
                              memo="Close to Retained Earnings",
                              account_id=re_acct, account_name=re_name, account_type=re_type,
                              debit=abs(total_to_re), credit=0))

        p.status = "closed"
        p.closed_at = now
        p.closed_by = actor
        s.add(p)
        try:
            audit("period.close.posted", target_type="period",
                  target_id=f"{year}-{month:02d}", outcome="success", status=200,
                  extra={"batch_id": cb.id,
                         "net_to_retained_earnings": round(total_to_re, 2)})
        except Exception:
            pass
        return True


def reopen_period(year: int, month: int, actor: str) -> bool:
    """
    Reopen a closed period by deleting (via reversal) the closing batch.
    (Conservative: keeps an audit trail by adding a reversal, not hard-deleting.)
    """
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        p = s.execute(
            select(AccountingPeriod).where(AccountingPeriod.year ==
                                           year, AccountingPeriod.month == month)
        ).scalars().one_or_none()
        if not p or p.status != "closed":
            return False

        # find closing batch
        b = s.execute(
            select(JournalBatch).where(
                JournalBatch.period_year == year,
                JournalBatch.period_month == month,
                JournalBatch.kind == "closing"
            )
        ).scalars().one_or_none()
        if not b:
            p.status = "open"
            p.closed_at = None
            p.closed_by = None
            s.add(p)
            return True

        # reverse closing lines into current month
        curr_y, curr_m = now.year, now.month
        rb = JournalBatch(
            source="billing", source_ref=f"UNCL-{year}-{month:02d}", kind="reversal",
            posted_at=now, posted_by=actor, period_year=curr_y, period_month=curr_m
        )
        s.add(rb)
        s.flush()
        lines = s.execute(select(GLEntry).where(
            GLEntry.batch_id == b.id)).scalars().all()
        for ln in lines:
            s.add(GLEntry(
                batch_id=rb.id, date=now, ref=f"UNCL-{year}{month:02d}",
                memo=f"Reverse closing: {ln.memo or ''}",
                account_id=ln.account_id, account_name=ln.account_name, account_type=ln.account_type,
                debit=float(ln.credit or 0), credit=float(ln.debit or 0),
            ))
        p.status = "open"
        p.closed_at = None
        p.closed_by = None
        s.add(p)
        try:
            audit("period.reopen.posted", target_type="period", target_id=f"{year}-{month:02d}",
                  status=200, outcome="success",
                  extra={"reversal_batch_id": rb.id, "lines": len(lines)})
        except Exception:
            pass
        return True
