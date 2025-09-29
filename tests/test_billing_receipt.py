import pandas as pd
from models.billing_store import create_receipt_from_rows, list_receipts, get_receipt_with_items, mark_receipt_paid
import pytest


def _fake_rows(username="alice", tier="mu"):
    # Mimic what compute_costs would produce minimally
    return [
        {"JobID": "12345", "Cost (฿)": 100, "CPU_Core_Hours": 10.0, "GPU_Hours": 0.0,
         "Mem_GB_Hours_Used": 0.0, "tier": tier, "User": username},
        {"JobID": "12346", "Cost (฿)": 50,  "CPU_Core_Hours":  5.0, "GPU_Hours": 0.0,
         "Mem_GB_Hours_Used": 0.0, "tier": tier, "User": username},
    ]


@pytest.mark.db
def test_create_and_fetch_receipt_roundtrip():
    rid, total, items = create_receipt_from_rows(
        "alice", "2025-01-01", "2025-01-31", _fake_rows())
    assert total == 150.0
    recs = list_receipts("alice")
    assert any(r["id"] == rid and r["status"] == "pending" for r in recs)

    hdr, lines = get_receipt_with_items(rid)
    assert hdr["total"] == 150.0
    assert len(lines) == 2
    assert hdr["invoice_no"].startswith("MUAI-INV-")


@pytest.mark.db
def test_mark_paid_is_idempotent(monkeypatch):
    rid, total, _ = create_receipt_from_rows(
        "alice", "2025-01-01", "2025-01-31", _fake_rows())
    assert total == 150.0

    ok1 = mark_receipt_paid(rid, actor="admin")
    ok2 = mark_receipt_paid(rid, actor="admin")
    assert ok1 is True and ok2 is True

    hdr, _ = get_receipt_with_items(rid)
    assert hdr["status"] == "paid"
    assert hdr["tx_ref"].startswith("payment:")
    assert hdr["method"] == "internal_admin"
    assert hdr["paid_at"] is not None
