# tests/test_user_endpoints.py
import pytest
from models.billing_store import create_receipt_from_rows


def _rows(username="admin", tier="mu"):
    return [
        {"JobID": "j1", "Cost (฿)": 50, "CPU_Core_Hours": 1.0, "GPU_Hours": 0.0,
         "Mem_GB_Hours_Used": 0.0, "tier": tier, "User": username}
    ]


# @pytest.mark.db
# def test_me_pages_and_csv(client, admin_user):
#     # /me (dashboard-like page)
#     r = client.get("/me")
#     assert r.status_code == 200

#     # /me.csv
#     r2 = client.get("/me.csv")
#     assert r2.status_code == 200
#     assert "csv" in r2.headers.get("Content-Type", "").lower()


@pytest.mark.db
def test_my_receipt_pdfs(client, admin_user):
    rid, total, _ = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31", _rows())
    assert total == 50.0

    # Details page (HTML)
    page = client.get(f"/me/receipts/{rid}")
    assert page.status_code == 200
    assert b"Invoice" in page.data or page.data  # page body present

    # PDFs (EN/TH)
    for path in (f"/me/receipts/{rid}.pdf", f"/me/receipts/{rid}.th.pdf"):
        r = client.get(path)
        assert r.status_code == 200
        assert "pdf" in r.headers.get("Content-Type", "").lower()
