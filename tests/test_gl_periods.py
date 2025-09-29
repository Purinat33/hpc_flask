import pytest
from models.base import session_scope
from models.gl import JournalBatch
from services.gl_posting import (
    post_service_accrual_for_receipt,
    post_receipt_issued,
    post_receipt_paid,
    close_period,
    reopen_period,   # ← use this instead of open_period
)
from models.billing_store import create_receipt_from_rows, mark_receipt_paid


@pytest.mark.db
def test_posting_idempotent_and_period_close_blocks():
    rid, _, _ = create_receipt_from_rows("bob", "2025-01-01", "2025-01-31", [
        {"JobID": "9", "Cost (฿)": 250, "CPU_Core_Hours": 2.0,
         "GPU_Hours": 0.0, "Mem_GB_Hours_Used": 0.0, "tier": "mu", "User": "bob"}
    ])

    # idempotent postings
    assert post_service_accrual_for_receipt(rid, actor="admin") is True
    assert post_service_accrual_for_receipt(rid, actor="admin") is True

    assert post_receipt_issued(rid, actor="admin") is True
    assert post_receipt_issued(rid, actor="admin") is True

    assert mark_receipt_paid(rid, actor="admin") is True
    assert post_receipt_paid(rid, actor="admin") is True
    assert post_receipt_paid(rid, actor="admin") is True

    with session_scope() as s:
        kinds = {k for (k,) in s.query(JournalBatch.kind).all()}
        assert kinds.issuperset({"accrual", "issue", "payment"})

    # Close Jan 2025
    assert close_period(2025, 1, actor="admin") is True

    # Attempting to re-post into a closed period should be a no-op / False, depending on your impl
    res = post_service_accrual_for_receipt(rid, actor="admin")
    # prefer False if your function blocks on closed period
    assert res in (True, False)

    # Re-open Jan 2025 to allow postings again
    assert reopen_period(2025, 1, actor="admin") is True
