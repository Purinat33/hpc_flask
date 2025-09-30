# tests/test_gl_posting_flow.py
import io
import zipfile
from datetime import datetime, timezone

import pandas as pd
import pytest

from models.base import session_scope
from models.schema import Receipt
from models.gl import AccountingPeriod, JournalBatch, GLEntry
from models.billing_store import create_receipt_from_rows
from services import accounting as acc
from services.gl_posting import (
    post_service_accrual_for_receipt,
    post_receipt_issued,
    post_receipt_paid,
    post_ecl_provision,
    close_period,
    reopen_period,
)
from services.accounting_export import run_formal_gl_export


def _dt(y, m, d, h=0, M=0, s=0):
    return datetime(y, m, d, h, M, s, tzinfo=timezone.utc)


@pytest.mark.db
def test_gl_postings_drcr_close_reopen_and_formal_export(client, admin_user):
    """
    Flow:
      R1: accrual (service Jan), issue (Jan), paid (Jan) → fully settled in Jan
      R2: accrual (service Jan) only → outstanding Contract Asset at Jan-end
      ECL (Jan) → allowance line(s) for CA (and AR in separate test)
      Close Jan → posting to Retained Earnings; Reopen → reversal batch
      Formal export (Jan) → zip payload + mark exported; second run is NOOP
    """
    # ---- Seed three receipts for January ----
    r1_id, _, _ = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31",
        [{"JobID": "A-1", "Cost (฿)": 100, "CPU_Core_Hours": 1.0, "GPU_Hours": 0.0,
          "Mem_GB_Hours_Used": 0.0, "tier": "mu", "User": "admin"}]
    )
    r2_id, _, _ = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31",
        [{"JobID": "A-2", "Cost (฿)": 200, "CPU_Core_Hours": 2.0, "GPU_Hours": 0.0,
          "Mem_GB_Hours_Used": 0.0, "tier": "mu", "User": "admin"}]
    )
    r3_id, _, _ = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31",
        [{"JobID": "A-3", "Cost (฿)": 150, "CPU_Core_Hours": 1.5, "GPU_Hours": 0.0,
          "Mem_GB_Hours_Used": 0.0, "tier": "mu", "User": "admin"}]
    )

    # Normalize service/issue/payment timestamps to January for determinism.
    with session_scope() as s:
        for rid, created, paid in [
            (r1_id, _dt(2025, 1, 12), _dt(2025, 1, 25)),
            (r2_id, _dt(2025, 1, 15), None),
            # this one will be issued but not paid
            (r3_id, _dt(2025, 1, 18), None),
        ]:
            r = s.get(Receipt, rid)
            r.created_at = created
            r.paid_at = paid
            if paid is not None:
                r.status = "paid"
        s.flush()

    actor = "pytest"

    # ---- Postings ----
    # R1: accrual → issue → payment
    assert post_service_accrual_for_receipt(r1_id, actor) is True
    assert post_receipt_issued(r1_id, actor) is True
    assert post_receipt_paid(r1_id, actor) is True

    # R2: accrual only (unbilled at period end)
    assert post_service_accrual_for_receipt(r2_id, actor) is True

    # R3: NO accrual, but issue in Jan (creates AR outstanding)
    assert post_receipt_issued(r3_id, actor) is True

    # ---- Journal (derived) sanity + Dr=Cr check for Jan window ----
    j = acc.derive_journal("2025-01-01", "2025-01-31")
    assert isinstance(j, pd.DataFrame)
    assert not j.empty
    assert round(float(j["debit"].sum()), 2) == round(
        float(j["credit"].sum()), 2)

    tb = acc.trial_balance(j)
    assert isinstance(tb, pd.DataFrame) and not tb.empty
    # Embedded attributes verify equality too
    assert tb.attrs.get("out_of_balance") == 0.0

    # Snapshot (balance sheet) should balance to ~zero check column
    bs = acc.balance_sheet(j)
    assert abs(float(bs.iloc[0]["Check(Assets - L-E)"])) < 0.01

    # ---- ECL provision at Jan end (should at least hit CA or AR) ----
    assert post_ecl_provision(2025, 1, actor, ar_due_days=30) in (True,)

    # ---- Close January ----
    assert close_period(2025, 1, actor) is True
    with session_scope() as s:
        p = s.query(AccountingPeriod).filter_by(year=2025, month=1).one()
        assert p.status == "closed"

    # Cannot post new accruals into a closed month
    assert post_service_accrual_for_receipt(r2_id, actor) is False

    # ---- Reopen January (creates a reversal batch in current month) ----
    assert reopen_period(2025, 1, actor) is True
    with session_scope() as s:
        p = s.query(AccountingPeriod).filter_by(year=2025, month=1).one()
        assert p.status == "open"
        # A reversal exists somewhere after reopen
        has_rev = s.query(JournalBatch).filter_by(
            kind="reversal").first() is not None
        assert has_rev

    # ---- Formal export (locks/exported_at; returns a ZIP) ----
    fname, blob = run_formal_gl_export("2025-01-01", "2025-01-31", actor)
    assert fname and blob
    z = zipfile.ZipFile(io.BytesIO(blob), "r")
    names = set(z.namelist())
    assert any(n.endswith(".csv") for n in names)
    assert any(n.startswith("manifest_run_") and n.endswith(".json")
               for n in names)
    assert any(n.startswith("signature_run_") and n.endswith(".txt")
               for n in names)

    # Running export again should be NOOP (already exported/locked)
    fname2, blob2 = run_formal_gl_export("2025-01-01", "2025-01-31", actor)
    assert fname2 is None and blob2 is None

    # GL persistence Dr=Cr for posted lines: sum across all posted entries equals
    with session_scope() as s:
        # Manual aggregate across posted entries
        dr = cr = 0.0
        for ln in s.query(GLEntry).all():
            dr += float(ln.debit or 0)
            cr += float(ln.credit or 0)
        assert round(dr, 2) == round(cr, 2)
