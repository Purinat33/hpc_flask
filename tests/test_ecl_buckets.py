# tests/test_ecl_buckets.py
from datetime import datetime, timezone
import pytest

from models.base import session_scope
from models.schema import Receipt
from models.gl import GLEntry, JournalBatch
from models.billing_store import create_receipt_from_rows
from services.gl_posting import (
    post_service_accrual_for_receipt,
    post_receipt_issued,
    post_ecl_provision,
)


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


@pytest.mark.db
def test_ecl_posts_for_ar_and_contract_asset(admin_user):
    """
    Make two open exposures as of Jan-31:
      - CA exposure: accrual only (unbilled) → allowance for CA
      - AR exposure: issued but not paid → allowance for AR
    Then run ECL and verify an impairment batch exists with 2+ lines.
    """
    # CA exposure (accrual-only)
    ca_id, _, _ = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31",
        [{"JobID": "CA", "Cost (฿)": 300, "CPU_Core_Hours": 3.0, "GPU_Hours": 0.0,
          "Mem_GB_Hours_Used": 0.0, "tier": "mu", "User": "admin"}]
    )
    # AR exposure (issued only)
    ar_id, _, _ = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31",
        [{"JobID": "AR", "Cost (฿)": 400, "CPU_Core_Hours": 4.0, "GPU_Hours": 0.0,
          "Mem_GB_Hours_Used": 0.0, "tier": "mu", "User": "admin"}]
    )

    with session_scope() as s:
        for rid, created in [(ca_id, _dt(2025, 1, 10)), (ar_id, _dt(2025, 1, 20))]:
            r = s.get(Receipt, rid)
            r.created_at = created
        s.flush()

    assert post_service_accrual_for_receipt(ca_id, "pytest") is True
    assert post_receipt_issued(ar_id, "pytest") is True

    # Run ECL at month end — should produce an impairment batch
    assert post_ecl_provision(2025, 1, "pytest", ar_due_days=0) is True

    # Verify impairment batch exists with at least 2 lines
    with session_scope() as s:
        b = s.query(JournalBatch).filter(
            JournalBatch.kind == "impairment").first()
        assert b is not None
        lines = s.query(GLEntry).filter(GLEntry.batch_id == b.id).all()
        # At least 2 lines: expense + allowance (and possibly 4 if both AR & CA)
        assert len(lines) >= 2
