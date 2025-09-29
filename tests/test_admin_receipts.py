# tests/test_admin_receipts.py
import pytest
from models.billing_store import create_receipt_from_rows


def _rows(username="admin", tier="mu"):
    return [
        {"JobID": "r1", "Cost (฿)": 100, "CPU_Core_Hours": 2.0, "GPU_Hours": 0.0,
         "Mem_GB_Hours_Used": 0.0, "tier": tier, "User": username}
    ]


@pytest.mark.db
def test_admin_receipt_pdfs_and_mark_paid(client, admin_user):
    rid, total, items = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31", _rows())
    assert total == 100.0

    # Admin PDFs (Thai and EN)
    for path in (f"/admin/receipts/{rid}.pdf", f"/admin/receipts/{rid}.th.pdf"):
        r = client.get(path)
        assert r.status_code == 200
        assert "pdf" in r.headers.get("Content-Type", "").lower()

    # Mark paid
    r = client.post(f"/admin/receipts/{rid}/paid",
                    data={}, follow_redirects=False)
    assert r.status_code in (302, 303)

    # Revert to pending (requires POST)
    r2 = client.post(
        f"/admin/receipts/{rid}/revert", data={"reason": "test"}, follow_redirects=False)
    assert r2.status_code in (302, 303)
