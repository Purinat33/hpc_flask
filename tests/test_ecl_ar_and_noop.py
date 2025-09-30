import io
import zipfile
import pandas as pd
import pytest
from datetime import datetime, timezone

from models.base import session_scope
from models.schema import Receipt
from models.gl import JournalBatch, GLEntry, AccountingPeriod
from services.gl_posting import (
    post_receipt_issued, post_ecl_provision, close_period, reopen_period
)
from services import accounting as acc


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.db
def test_ecl_on_ar_current_bucket_and_lines_created():
    # Seed one issued (unpaid) receipt in Jan → outstanding AR = gross
    with session_scope() as s:
        r = Receipt(
            username="admin", total=200.0,
            pricing_tier="mu",  # REQUIRED by schema (NOT NULL)
            rate_cpu=0, rate_gpu=0, rate_mem=0,
            rates_locked_at=_dt(2025, 1, 10),
            start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
            created_at=_dt(2025, 1, 10), status="pending",
        )
        s.add(r)
        s.flush()
        rid = r.id

    assert post_receipt_issued(rid, actor="pytest") is True

    # Force a visible provision rate for AR; zero for CA so we isolate the effect.
    rates = {
        "ar": {"current": 0.10, "1-30": 0.10, "31-60": 0.10, "61-90": 0.10, "90+": 0.10},
        "ca": {"current": 0.00, "1-30": 0.00, "31-60": 0.00, "61-90": 0.00, "90+": 0.00},
    }
    assert post_ecl_provision(
        2025, 1, "pytest", rates=rates, ar_due_days=0) is True

    # Validate impairment batch + lines
    ALW_AR = acc._acc("Allowance for ECL - Trade receivables")
    ECL_EXP = acc._acc("Impairment loss (ECL)")

    with session_scope() as s:
        b = (s.query(JournalBatch)
             .filter_by(kind="impairment", period_year=2025, period_month=1)
             .order_by(JournalBatch.id.desc()).first())
        assert b is not None

        lines = s.query(GLEntry).filter_by(batch_id=b.id).all()
        # two lines: Dr ECL expense, Cr allowance (AR)
        assert any(ln.account_id == ECL_EXP and float(
            ln.debit or 0) > 0 for ln in lines)
        assert any(ln.account_id == ALW_AR and float(
            ln.credit or 0) > 0 for ln in lines)

        # Amount equals 10% of outstanding AR (gross)
        dr = sum(float(ln.debit or 0)
                 for ln in lines if ln.account_id == ECL_EXP)
        cr = sum(float(ln.credit or 0)
                 for ln in lines if ln.account_id == ALW_AR)
        expected = round(200.0 * 0.10, 2)
        assert round(dr, 2) == expected and round(cr, 2) == expected


@pytest.mark.db
def test_ecl_noop_when_no_outstanding_and_zero_delta():
    # Close empty Feb, run ECL → should still return True (noop path) and not error
    # (We don't inspect audit; we just assert it doesn't attempt to post when nothing to do.)
    assert post_ecl_provision(
        2025, 2, "pytest", rates=None, ar_due_days=30) is True
    # And closing still works on an empty period
    assert close_period(2025, 2, "pytest") is True
    assert reopen_period(2025, 2, "pytest") is True
