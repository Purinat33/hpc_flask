# services/accounting_export.py
from __future__ import annotations
from typing import Tuple, Iterable, List
import csv
import io
from datetime import date
from models.billing_store import admin_list_receipts, get_receipt_with_items

# Simple chart-of-accounts (override later from DB/env if you want)
COA = {
    "cash": {"id": 1000, "name": "Cash/Bank",            "type": "ASSET"},
    "ar":   {"id": 1100, "name": "Accounts Receivable",  "type": "ASSET"},
    "rev":  {"id": 4000, "name": "Service Revenue",      "type": "INCOME"},
}


def _iso(d) -> str:
    # accept None/naive; return YYYY-MM-DD or ""
    try:
        return (d.date() if hasattr(d, "date") else d).isoformat()
    except Exception:
        return ""


def build_general_ledger_csv(start: str, end: str) -> Tuple[str, str]:
    """
    General Journal (double-entry) covering all receipts whose created_at/paid_at fall within [start,end] local dates.
    One row per journal line.
    Columns match your sample to keep it simple for Excel/imports.
    """
    # collect both pending and paid; we’ll filter rows by date on the fly
    pending = admin_list_receipts(status="pending") or []
    paid = admin_list_receipts(status="paid") or []
    all_rs = pending + paid

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "ref", "memo", "account_id",
               "account_name", "account_type", "debit", "credit"])

    def in_window(dstr: str) -> bool:
        return bool(dstr) and (start <= dstr <= end)

    for r in all_rs:
        rid = r["id"]
        ref = f"R{rid}"
        user = r["username"]
        total = float(r["total"] or 0.0)

        # 1) At issuance (when the receipt was created): Dr AR / Cr Service Revenue
        issue_d = _iso(r.get("created_at"))
        if in_window(issue_d) and total > 0:
            w.writerow([issue_d, ref, f"Receipt issued for {user}",
                        COA["ar"]["id"], COA["ar"]["name"], COA["ar"]["type"], f"{total:.2f}", "0.00"])
            w.writerow([issue_d, ref, f"Receipt issued for {user}",
                        COA["rev"]["id"], COA["rev"]["name"], COA["rev"]["type"], "0.00", f"{total:.2f}"])

        # 2) When paid: Dr Cash / Cr AR
        paid_d = _iso(r.get("paid_at"))
        if r["status"] == "paid" and in_window(paid_d) and total > 0:
            w.writerow([paid_d, ref, f"Receipt paid by {user}",
                        COA["cash"]["id"], COA["cash"]["name"], COA["cash"]["type"], f"{total:.2f}", "0.00"])
            w.writerow([paid_d, ref, f"Receipt paid by {user}",
                        COA["ar"]["id"],   COA["ar"]["name"],   COA["ar"]["type"],   "0.00", f"{total:.2f}"])

    out.seek(0)
    fname = f"general_ledger_{start}_to_{end}.csv"
    return fname, out.read()


def build_xero_bank_csv(start: str, end: str) -> Tuple[str, str]:
    """
    Xero 'Bank Statement' CSV for *paid* receipts (cash inflows).
    Columns per Xero help: Date, Amount, Payee, Description, Reference (others optional/ignored).
    Positive Amount = money received.
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
    Supports importing pending or paid (choose status via route).
    Columns subset commonly accepted by Xero:
      ContactName,InvoiceNumber,InvoiceDate,DueDate,Description,Quantity,UnitAmount,AccountCode,TaxType
    NOTE: If you prefer to import only *pending* as AR, call the route that passes status=pending.
    """
    pending = admin_list_receipts(status="pending") or []
    paid = admin_list_receipts(status="paid") or []
    all_rs = pending + paid

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ContactName", "InvoiceNumber", "InvoiceDate", "DueDate",
               "Description", "Quantity", "UnitAmount", "AccountCode", "TaxType"])

    for r in all_rs:
        # choose the 'invoice date' as created_at; due date = created_at + 30d (simple default)
        inv_dt = _iso(r.get("created_at"))
        if not (start <= inv_dt <= end):
            continue
        rid = r["id"]
        user = r["username"]
        amt = float(r["total"] or 0.0)
        # let Xero’s defaults apply, or derive if you want: (created_at + 30 days)
        due = ""
        w.writerow([user, f"R{rid}", inv_dt, due, f"HPC usage for R{rid}",
                   "1", f"{amt:.2f}", COA["rev"]["id"], "NONE"])

    out.seek(0)
    fname = f"xero_sales_{start}_to_{end}.csv"
    return fname, out.read()
