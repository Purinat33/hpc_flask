import datetime as dt

import pytest
from services.gl_posting import (
    post_service_accrual_for_receipt, post_receipt_issued, post_receipt_paid, bootstrap_periods
)
from models.billing_store import create_receipt_from_rows, mark_receipt_paid
from models.gl import JournalBatch, GLEntry
from models.base import session_scope


def _mk_receipt():
    rid, _, _ = create_receipt_from_rows("alice", "2025-01-01", "2025-01-31", [
        {"JobID": "1", "Cost (฿)": 107.00, "CPU_Core_Hours": 1, "GPU_Hours": 0,
         "Mem_GB_Hours_Used": 0, "tier": "mu", "User": "alice"}
    ])
    return rid


@pytest.mark.db
def test_accrual_issue_payment_flow():
    rid = _mk_receipt()
    assert post_service_accrual_for_receipt(rid, actor="admin") is True
    assert post_receipt_issued(rid, actor="admin") is True

    # Mark paid (this writes Payment + flips Receipt)
    assert mark_receipt_paid(rid, actor="admin") is True
    # Then post cash application
    assert post_receipt_paid(rid, actor="admin") is True

    # Assert batches/lines exist
    with session_scope() as s:
        kinds = [k for (k,) in s.query(JournalBatch.kind).all()]
        assert set(kinds) >= {"accrual", "issue", "payment"}
        lines = s.query(GLEntry).all()
        assert any(l.account_name.startswith("Cash/Bank") for l in lines)
        assert any(l.account_name.startswith("Accounts Receivable")
                   for l in lines)
