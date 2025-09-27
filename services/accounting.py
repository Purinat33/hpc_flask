# services/accounting.py
from __future__ import annotations
from typing import List, Dict
from datetime import datetime, date
import pandas as pd

from models.billing_store import admin_list_receipts
from models.billing_store import _tax_cfg
# ---- Chart of accounts (IDs are strings for portability) ----
# Account types: ASSET, LIABILITY, EQUITY, INCOME, EXPENSE


def chart_of_accounts() -> List[Dict]:
    return [
        {"id": "1000", "name": "Cash/Bank",            "type": "ASSET"},
        {"id": "1100", "name": "Accounts Receivable",  "type": "ASSET"},
        {"id": "1150",
            "name": "Contract Asset (Unbilled A/R)",     "type": "ASSET"},
        {"id": "2000", "name": "Unearned Revenue",
            "type": "LIABILITY"},  # reserved
        {"id": "2100", "name": "VAT Output Payable", "type": "LIABILITY"},
        {"id": "3000", "name": "Retained Earnings",    "type": "EQUITY"},
        {"id": "4000", "name": "Service Revenue",      "type": "INCOME"},
        {"id": "5000", "name": "Cost of Service",
            "type": "EXPENSE"},     # reserved
    ]


# Quick lookup helpers
_ACC = {a["id"]: a for a in chart_of_accounts()}
_ACC_BY_NAME = {a["name"]: a for a in chart_of_accounts()}


def _entry_service_delivery(r: dict) -> List[Dict]:
    """
    Book revenue in the service month (use period end as the service date).
    Dr 1150 Contract Asset (net) ; Cr 4000 Service Revenue (net)
    """
    total = float(r.get("total") or 0.0)
    if total <= 0:
        return []
    # Use the service period end as the recognition date
    d_iso = _to_date_iso(r.get("end") or r.get("created_at") or r.get("start"))
    ref = f"R{r['id']}"
    memo = f"Revenue recognized for {r.get('username', '?')} (service period)"

    net, _vat = _split_vat(total)  # VAT is not revenue; don’t book it here
    if net <= 0:
        return []
    return [
        _mk_line(d_iso, ref, memo, _acc(
            "Contract Asset (Unbilled A/R)"), debit=net),
        _mk_line(d_iso, ref, memo, _acc(
            "Service Revenue"),                 credit=net),
    ]


def _acc(name: str) -> str:
    return _ACC_BY_NAME[name]["id"]

# ---- Journal model (derived, not persisted) ----
# One journal "entry" has multiple "lines": (date, ref, memo, account_id, debit, credit)


def _mk_line(d: str, ref: str, memo: str, account_id: str, debit: float = 0.0, credit: float = 0.0) -> Dict:
    return {
        "date": d, "ref": ref, "memo": memo,
        "account_id": account_id,
        "account_name": _ACC[account_id]["name"],
        "account_type": _ACC[account_id]["type"],
        "debit": round(float(debit or 0.0), 2),
        "credit": round(float(credit or 0.0), 2),
    }


def _entry_receipt_issue(r: dict) -> List[Dict]:
    """
    On invoice creation (your 'pending' state):
    Dr 1100 Accounts Receivable (gross)
      Cr 1150 Contract Asset (net)
      Cr 2100 VAT Output Payable (vat, if any)
    """
    total = float(r.get("total") or 0.0)
    if total <= 0:
        return []
    d_iso = _to_date_iso(r.get("created_at") or r.get("start") or r.get("end"))
    ref = f"R{r['id']}"
    memo = f"Invoice issued for {r.get('username', '?')}"

    net, vat = _split_vat(total)
    lines = [
        _mk_line(d_iso, ref, memo, _acc(
            "Accounts Receivable"),               debit=total),
        _mk_line(d_iso, ref, memo, _acc(
            "Contract Asset (Unbilled A/R)"),     credit=net),
    ]
    if vat > 0:
        lines.append(_mk_line(d_iso, ref, memo, _acc(
            "VAT Output Payable"),   credit=vat))
    return lines


def _entry_receipt_paid(r: dict) -> List[Dict]:
    """
    When receipt is marked 'paid':
    Dr 1000 Cash/Bank; Cr 1100 A/R
    """
    if r.get("status") != "paid":
        return []
    total = float(r.get("total") or 0.0)
    if total <= 0:
        return []
    d = (r.get("paid_at") or r.get("created_at") or r.get("end"))
    d_iso = _to_date_iso(d)
    ref = f"R{r['id']}"
    memo = f"Receipt paid by {r.get('username', '?')}"
    return [
        _mk_line(d_iso, ref, memo, _acc("Cash/Bank"),            debit=total),
        _mk_line(d_iso, ref, memo, _acc("Accounts Receivable"),  credit=total),
    ]


def _to_date_iso(x) -> str:
    if isinstance(x, datetime):
        return x.date().isoformat()
    if isinstance(x, date):
        return x.isoformat()
    try:
        return pd.to_datetime(x, utc=True).date().isoformat()
    except Exception:
        return date.today().isoformat()


def _split_vat(gross: float) -> tuple[float, float]:
    enabled, _label, rate_pct, _inclusive = _tax_cfg()
    r = float(rate_pct or 0.0) / 100.0
    if not enabled or r <= 0 or gross <= 0:
        return round(gross, 2), 0.0
    # works for both 'Included' and 'Added' totals
    net = round(gross / (1.0 + r), 2)
    vat = round(gross - net, 2)
    return net, vat


def derive_journal(start: str, end: str) -> pd.DataFrame:
    """
    Build a journal from receipts, with IFRS/TFRS/GAAP-like timing:
      1) Service month (period end):   Dr 1150 ; Cr 4000   [revenue]
      2) Invoice created:              Dr 1100 ; Cr 1150 ; (Cr 2100 VAT)
      3) Cash collected (if paid):     Dr 1000 ; Cr 1100
    """
    rows = admin_list_receipts(status=None)  # all
    lines: List[Dict] = []
    for r in rows:
        lines.extend(_entry_service_delivery(r))  # <-- revenue timing here
        lines.extend(_entry_receipt_issue(r))     # <-- reclass to A/R + VAT
        lines.extend(_entry_receipt_paid(r))      # <-- cash settlement

    if not lines:
        return pd.DataFrame(columns=[
            "date", "ref", "memo", "account_id", "account_name",
            "account_type", "debit", "credit"
        ])

    j = pd.DataFrame(lines)
    j = j[(j["date"] >= start) & (j["date"] <= end)].copy()
    j.sort_values(by=["date", "ref", "account_id"], inplace=True)
    return j.reset_index(drop=True)

# ---- Reports derived from the journal ----


def trial_balance(journal: pd.DataFrame) -> pd.DataFrame:
    """
    Sum debits/credits per account and compute ending balance by account type.
    Balance sign convention:
      ASSET/EXPENSE: balance = debits - credits
      LIABILITY/EQUITY/INCOME: balance = credits - debits
    """
    if journal.empty:
        return pd.DataFrame(columns=["account_id", "account_name", "account_type", "debits", "credits", "balance"])

    g = journal.groupby(["account_id", "account_name", "account_type"], dropna=False).agg(
        debits=("debit", "sum"),
        credits=("credit", "sum")
    ).reset_index()

    def _balance(row):
        t = row["account_type"]
        if t in ("ASSET", "EXPENSE"):
            return row["debits"] - row["credits"]
        return row["credits"] - row["debits"]

    g["balance"] = g.apply(_balance, axis=1).round(2)
    # TB check (should be zero): sum(debit) == sum(credit)
    g.attrs["sum_debits"] = round(float(journal["debit"].sum()), 2)
    g.attrs["sum_credits"] = round(float(journal["credit"].sum()), 2)
    g.attrs["out_of_balance"] = round(
        g.attrs["sum_debits"] - g.attrs["sum_credits"], 2)
    return g


def income_statement(journal: pd.DataFrame) -> pd.DataFrame:
    """
    Simple P&L from journal (derived only).
    Revenue: 4000 Service Revenue (credits - debits)
    Expenses: 5000 Cost of Service (debits - credits) [reserved]
    """
    if journal.empty:
        return pd.DataFrame([{"Revenue": 0.0, "Expenses": 0.0, "Net_Income": 0.0}])

    tb = trial_balance(journal)
    # already (credits - debits)
    rev = tb[tb["account_type"] == "INCOME"]["balance"].sum()
    # for EXPENSE we computed debits - credits
    exp = tb[tb["account_type"] == "EXPENSE"]["balance"].sum()
    # tb.balance for EXPENSE is debits-credits, so it’s already positive if expense > 0
    revenue = round(float(rev), 2)
    expenses = round(float(exp), 2)
    net = round(revenue - expenses, 2)
    return pd.DataFrame([{"Revenue": revenue, "Expenses": expenses, "Net_Income": net}])


def balance_sheet(journal: pd.DataFrame) -> pd.DataFrame:
    """
    Snapshot-style: Assets vs Liabilities+Equity from trial balance balances.
    (This is a simplified view without retained earnings rollforward.)
    """
    tb = trial_balance(journal)
    assets = tb[tb["account_type"] == "ASSET"]["balance"].sum()
    liab = tb[tb["account_type"] == "LIABILITY"]["balance"].sum()
    equity = tb[tb["account_type"] == "EQUITY"]["balance"].sum()
    income = tb[tb["account_type"] == "INCOME"]["balance"].sum()
    exp = tb[tb["account_type"] == "EXPENSE"]["balance"].sum()

    # Fold P&L into equity for snapshot: Equity + (Income - Expense)
    equity_adj = equity + (income - exp)
    return pd.DataFrame([{
        "Assets": round(float(assets), 2),
        "Liabilities": round(float(liab), 2),
        "Equity_Including_PnL": round(float(equity_adj), 2),
        "Check(Assets - L-E)": round(float(assets - (liab + equity_adj)), 2)
    }])
