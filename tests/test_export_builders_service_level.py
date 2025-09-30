import csv
import io
import pytest
from datetime import datetime, timezone

from models.base import session_scope
from models.schema import Receipt
from services.accounting_export import (
    build_general_ledger_csv, build_xero_sales_csv, build_xero_bank_csv
)


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.db
def test_csv_builders_with_minimal_data():
    # Seed one paid receipt inside the window so all three builders have rows.
    with session_scope() as s:
        r = Receipt(
            username="admin", total=150.0,
            start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
            pricing_tier="mu",
            rate_cpu=0, rate_gpu=0, rate_mem=0,
            rates_locked_at=_dt(2025, 1, 10),
            created_at=_dt(2025, 1, 10), paid_at=_dt(2025, 1, 20),
            status="paid",
        )
        s.add(r)
        s.flush()

    # General ledger
    g_name, g_csv = build_general_ledger_csv("2025-01-01", "2025-01-31")
    assert g_name.endswith(".csv") and g_csv
    reader = csv.reader(io.StringIO(g_csv))
    hdr = next(reader)
    assert "account_id" in hdr and "debit" in hdr and "credit" in hdr
    # Should have at least service/issue/payment lines
    rows = list(reader)
    assert len(rows) >= 4

    # Xero bank (cash)
    b_name, b_csv = build_xero_bank_csv("2025-01-01", "2025-01-31")
    assert b_name.endswith(".csv") and b_csv
    b_hdr = next(csv.reader(io.StringIO(b_csv)))
    assert b_hdr[:2] == ["Date", "Amount"]

    # Xero sales (invoices)
    s_name, s_csv = build_xero_sales_csv("2025-01-01", "2025-01-31")
    assert s_name.endswith(".csv") and s_csv
    s_hdr = next(csv.reader(io.StringIO(s_csv)))
    assert "InvoiceNumber" in s_hdr and "AccountCode" in s_hdr
