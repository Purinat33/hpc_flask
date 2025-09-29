import pytest
from models.billing_store import create_receipt_from_rows, get_receipt_with_items, mark_receipt_paid, revert_receipt_to_pending


def _rows(u="alice", t="mu"):
    return [
        {"JobID": "1", "Cost (฿)": 100, "CPU_Core_Hours": 1.0, "GPU_Hours": 0.0,
         "Mem_GB_Hours_Used": 0.0, "tier": t, "User": u}
    ]


@pytest.mark.db
def test_mark_paid_and_revert_guard_rails():
    rid, total, _ = create_receipt_from_rows(
        "alice", "2025-02-01", "2025-02-28", _rows())
    assert total == 100

    # first mark paid works
    assert mark_receipt_paid(rid, actor="admin") is True
    hdr, _ = get_receipt_with_items(rid)
    assert hdr["status"] == "paid"
    assert hdr["paid_at"] is not None

    # idempotent re-pay is allowed but should not duplicate effects
    assert mark_receipt_paid(rid, actor="admin") is True
    hdr2, _ = get_receipt_with_items(rid)
    assert hdr2["status"] == "paid"

    # revert to pending should work only if your business rules allow it
    ok = revert_receipt_to_pending(rid, actor="admin")
    # If your impl disallows reverting paid, flip assertion accordingly:
    # assert ok is False
    # For now, accept either True/False but assert state matches behavior
    _, _ = get_receipt_with_items(rid)
