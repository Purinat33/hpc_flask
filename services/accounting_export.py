# services/accounting_export.py
from __future__ import annotations
from typing import Tuple
import csv
import io
from models.billing_store import admin_list_receipts, _tax_cfg
from sqlalchemy import select, update
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import io
import csv
import json
from models.base import session_scope
from models.gl import JournalBatch, GLEntry, ExportRun, ExportRunBatch
from models.audit_store import APP_SECRET as EXPORT_SECRET, SIGNING_KEY_ID
from models.audit_store import audit


# Simple chart-of-accounts (override later from DB/env if you want)
COA = {
    "cash":     {"id": 1000, "name": "Cash/Bank",                         "type": "ASSET"},
    "ar":       {"id": 1100, "name": "Accounts Receivable",               "type": "ASSET"},
    "unbilled": {"id": 1150, "name": "Contract Asset (Unbilled A/R)",     "type": "ASSET"},
    "rev":      {"id": 4000, "name": "Service Revenue",                   "type": "INCOME"},
    "vat":      {"id": 2100, "name": "VAT Output Payable",                "type": "LIABILITY"},
    "allow_ar":  {"id": 1290, "name": "Allowance for ECL - Trade receivables", "type": "ASSET"},
    "allow_ca":  {"id": 1291, "name": "Allowance for ECL - Contract assets",   "type": "ASSET"},
    "ecl_exp":   {"id": 6090, "name": "Impairment loss (ECL)",                 "type": "EXPENSE"},
}


def _utc(d):  # 'YYYY-MM-DD' -> aware range
    from datetime import datetime, timedelta, timezone
    s = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
    e = s.replace(day=s.day) + timedelta(hours=23, minutes=59, seconds=59)
    return s, e


def run_formal_gl_export(start: str, end: str, actor: str, kind: str = "posted_gl_csv") -> tuple[str, bytes] | tuple[None, None]:
    s_utc, e_utc = _utc(start)[0], _utc(end)[1]
    with session_scope() as s:
        # 1) create run (running)
        run = ExportRun(
            kind=kind, status="running", actor=actor,
            criteria={"start": start, "end": end},
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        s.flush()  # run.id

        # 2) lock & pick eligible batches
        batch_ids = s.execute(
            select(JournalBatch.id)
            .where(
                JournalBatch.exported_at.is_(None),
                JournalBatch.kind.in_(
                    ["accrual", "issue", "payment", "impairment"]),
                GLEntry.batch_id == JournalBatch.id,   # ensures there are lines
                GLEntry.date >= s_utc, GLEntry.date <= e_utc,
            )
            .with_for_update(skip_locked=True)
        ).scalars().all()

        if not batch_ids:
            run.status = "noop"
            run.finished_at = datetime.now(timezone.utc)
            s.flush()
            audit("export.formal.finish", target_type="window", target_id=f"{start}:{end}",
                  outcome="success", status=200, extra={"noop": True, "run_id": run.id})
            return None, None

        # 3) fetch lines (deterministic order)
        rows = s.execute(
            select(
                GLEntry.date, GLEntry.ref, GLEntry.memo,
                GLEntry.account_id, GLEntry.account_name, GLEntry.account_type,
                GLEntry.debit, GLEntry.credit, GLEntry.batch_id, GLEntry.seq_in_batch,
                GLEntry.external_txn_id
            ).where(GLEntry.batch_id.in_(batch_ids))
             .order_by(GLEntry.batch_id, GLEntry.seq_in_batch, GLEntry.id)
        ).all()

        # 4) build CSV bytes
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["date", "ref", "memo", "account_id", "account_name", "account_type",
                   "debit", "credit", "batch_id", "line_seq", "external_txn_id"])
        for r in rows:
            w.writerow([
                r.date.date().isoformat(), r.ref, r.memo, r.account_id, r.account_name, r.account_type,
                float(r.debit or 0), float(
                    r.credit or 0), r.batch_id, int(r.seq_in_batch or 0),
                r.external_txn_id or f"B{r.batch_id:08d}-L{int(r.seq_in_batch or 0):05d}",
            ])
        csv_bytes = out.getvalue().encode("utf-8")

        # 5) manifest + hashes + signature
        fhash = sha256(csv_bytes).hexdigest()
        manifest = {
            "run_id": run.id,
            "kind": kind,
            "criteria": run.criteria,
            "batch_count": len(set(batch_ids)),
            "line_count": len(rows),
            "file_sha256": fhash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "key_id": SIGNING_KEY_ID,
        }
        mjson = json.dumps(manifest, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
        mhash = sha256(mjson).hexdigest()
        sig = hmac.new(EXPORT_SECRET, fhash.encode(
            "utf-8"), digestmod="sha256").hexdigest()

        # 6) mark batches as exported & link them to the run
        now = datetime.now(timezone.utc)
        for i, bid in enumerate(sorted(set(batch_ids)), start=1):
            s.add(ExportRunBatch(run_id=run.id, batch_id=bid, seq=i))
            s.execute(
                update(JournalBatch)
                .where(JournalBatch.id == bid)
                .values(exported_at=now, export_run_id=run.id, export_seq=i)
            )

        # 7) finalize run
        run.file_sha256 = fhash
        run.file_size = len(csv_bytes)
        run.manifest_sha256 = mhash
        run.signature = sig
        run.key_id = SIGNING_KEY_ID
        run.status = "success"
        run.finished_at = now
        s.flush()

        audit("export.formal.finish", target_type="window", target_id=f"{start}:{end}",
              outcome="success", status=200, extra={"run_id": run.id, "batches": len(batch_ids), "lines": len(rows)})

        # 8) return a ZIP containing CSV + MANIFEST + SIGNATURE
        import zipfile
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"gl_export_run_{run.id}.csv", csv_bytes)
            z.writestr(f"manifest_run_{run.id}.json", mjson)
            z.writestr(
                f"signature_run_{run.id}.txt", f"key_id={SIGNING_KEY_ID}\nsha256={fhash}\nsignature={sig}\n")
        mem.seek(0)

        fname = f"gl_export_run_{run.id}_{start}_to_{end}.zip"
        return fname, mem.read()


def _iso(d) -> str:
    # accept None/naive; return YYYY-MM-DD or ""
    try:
        return (d.date() if hasattr(d, "date") else d).isoformat()
    except Exception:
        return ""


def _split_vat(gross: float) -> tuple[float, float]:
    """
    Return (net, vat) given a gross total and current VAT config.
    Mirrors services/accounting._split_vat: treat `total` as gross
    regardless of inclusive/added UI; gross / (1+r) = net; remainder = VAT.
    """
    enabled, _label, rate_pct, _inclusive = _tax_cfg()
    r = float(rate_pct or 0.0) / 100.0
    if not enabled or r <= 0 or gross <= 0:
        return round(gross, 2), 0.0
    net = round(gross / (1.0 + r), 2)
    vat = round(gross - net, 2)
    return net, vat


def build_general_ledger_csv(start: str, end: str) -> Tuple[str, str]:
    pending = admin_list_receipts(status="pending") or []
    paid = admin_list_receipts(status="paid") or []
    all_rs = pending + paid

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "ref", "memo", "account_id",
               "account_name", "account_type", "debit", "credit"])

    def in_window(dstr: str) -> bool:
        return bool(dstr) and (start <= dstr <= end)

    def _iso(d):  # keep your helper
        try:
            return (d.date() if hasattr(d, "date") else d).isoformat()
        except Exception:
            return ""

    for r in all_rs:
        rid = r["id"]
        user = r["username"]
        gross = float(r["total"] or 0.0)
        net, vat = _split_vat(gross)

        service_d = _iso(r.get("end") or r.get("created_at") or r.get("start"))
        issue_d = _iso(r.get("created_at"))
        paid_d = _iso(r.get("paid_at"))

        # 1) SERVICE MONTH (revenue)
        if gross > 0 and in_window(service_d) and net > 0:
            w.writerow([service_d, f"R{rid}", f"Revenue recognized for {user}",
                        COA["unbilled"]["id"], COA["unbilled"]["name"], COA["unbilled"]["type"], f"{net:.2f}", "0.00"])
            w.writerow([service_d, f"R{rid}", f"Revenue recognized for {user}",
                        COA["rev"]["id"],      COA["rev"]["name"],      COA["rev"]["type"],      "0.00",     f"{net:.2f}"])

        # 2) INVOICE ISSUED (reclass + VAT)
        if gross > 0 and in_window(issue_d):
            w.writerow([issue_d,   f"R{rid}", f"Invoice issued for {user}",
                        COA["ar"]["id"],      COA["ar"]["name"],      COA["ar"]["type"],      f"{gross:.2f}", "0.00"])
            if net > 0:
                w.writerow([issue_d, f"R{rid}", f"Invoice issued for {user}",
                            COA["unbilled"]["id"], COA["unbilled"]["name"], COA["unbilled"]["type"], "0.00", f"{net:.2f}"])
            if vat > 0:
                w.writerow([issue_d, f"R{rid}", f"Invoice issued for {user}",
                            COA["vat"]["id"],      COA["vat"]["name"],      COA["vat"]["type"],      "0.00", f"{vat:.2f}"])

        # 3) CASH COLLECTED (paid)
        if r["status"] == "paid" and gross > 0 and in_window(paid_d):
            w.writerow([paid_d,    f"R{rid}", f"Receipt paid by {user}",
                        COA["cash"]["id"],     COA["cash"]["name"],     COA["cash"]["type"],     f"{gross:.2f}", "0.00"])
            w.writerow([paid_d,    f"R{rid}", f"Receipt paid by {user}",
                        COA["ar"]["id"],       COA["ar"]["name"],       COA["ar"]["type"],       "0.00",        f"{gross:.2f}"])

    out.seek(0)
    fname = f"general_ledger_{start}_to_{end}.csv"
    return fname, out.read()


def build_xero_bank_csv(start: str, end: str) -> Tuple[str, str]:
    """
    Xero 'Bank Statement' CSV for *paid* receipts (cash inflows).
    Columns per Xero help: Date, Amount, Payee, Description, Reference (others optional/ignored).
    Positive Amount = money received (gross).
    """
    rows = admin_list_receipts(status="paid") or []

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Amount", "Payee", "Description", "Reference"])

    for r in rows:
        paid_d = _iso(r.get("paid_at"))
        if not (start <= paid_d <= end):
            continue
        amt = float(r["total"] or 0.0)
        rid = r["id"]
        user = r["username"]
        w.writerow([paid_d, f"{amt:.2f}", user,
                   f"Receipt {rid} paid by {user}", f"R{rid}"])

    out.seek(0)
    fname = f"xero_bank_{start}_to_{end}.csv"
    return fname, out.read()


def build_xero_sales_csv(start: str, end: str) -> Tuple[str, str]:
    """
    Xero 'Sales Invoices' CSV (minimal fields).
    We emit one line per receipt (quantity=1) into AccountCode=4000 (Service Revenue).
    VAT-aware: UnitAmount is **net** if VAT enabled; TaxType is set accordingly (else NONE).
    Columns subset commonly accepted by Xero:
      ContactName,InvoiceNumber,InvoiceDate,DueDate,Description,Quantity,UnitAmount,AccountCode,TaxType
    """
    pending = admin_list_receipts(status="pending") or []
    paid = admin_list_receipts(status="paid") or []
    all_rs = pending + paid

    # Choose a TaxType for your Xero org (make configurable if needed)
    enabled, _label, rate_pct, _inclusive = _tax_cfg()
    XERO_TAXTYPE = "OUTPUT" if (
        enabled and float(rate_pct or 0) > 0) else "NONE"

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ContactName", "InvoiceNumber", "InvoiceDate", "DueDate",
               "Description", "Quantity", "UnitAmount", "AccountCode", "TaxType"])

    for r in all_rs:
        # choose the 'invoice date' as created_at; due date left blank (Xero default terms apply)
        inv_dt = _iso(r.get("created_at"))
        if not (start <= inv_dt <= end):
            continue
        rid = r["id"]
        user = r["username"]
        gross = float(r["total"] or 0.0)
        net, _vat = _split_vat(gross)
        due = ""
        w.writerow([user, f"R{rid}", inv_dt, due, f"HPC usage for R{rid}",
                   "1", f"{net:.2f}", COA["rev"]["id"], XERO_TAXTYPE])

    out.seek(0)
    fname = f"xero_sales_{start}_to_{end}.csv"
    return fname, out.read()
