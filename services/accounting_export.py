# services/accounting_export.py
from __future__ import annotations
from typing import Tuple, Iterable, List
import csv
import io
from datetime import date
from models.billing_store import admin_list_receipts, get_receipt_with_items, _tax_cfg

# Simple chart-of-accounts (override later from DB/env if you want)
COA = {
    "cash":     {"id": 1000, "name": "Cash/Bank",                         "type": "ASSET"},
    "ar":       {"id": 1100, "name": "Accounts Receivable",               "type": "ASSET"},
    "unbilled": {"id": 1150, "name": "Contract Asset (Unbilled A/R)",     "type": "ASSET"},
    "rev":      {"id": 4000, "name": "Service Revenue",                   "type": "INCOME"},
    "vat":      {"id": 2100, "name": "VAT Output Payable",                "type": "LIABILITY"},
}


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
