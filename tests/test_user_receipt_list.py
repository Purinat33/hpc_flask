import pytest
from models.users_db import create_user
from models.billing_store import create_receipt_from_rows


def _rows(username="user1", tier="mu"):
    return [{"JobID": "x1", "Cost (฿)": 12.5, "CPU_Core_Hours": 0.5, "GPU_Hours": 0.0,
             "Mem_GB_Hours_Used": 0.0, "tier": tier, "User": username}]


@pytest.mark.db
def test_receipts_list_page_and_filters_for_normal_user(client):
    # Create a normal user and a receipt for them
    try:
        create_user("user1", "pw", role="user")
    except Exception:
        pass

    rid, total, _ = create_receipt_from_rows(
        "user1", "2025-01-01", "2025-01-31", _rows())
    assert total == 12.5

    # Login as user1 (normal user, not admin)
    client.post("/login", data={"username": "user1",
                "password": "pw"}, follow_redirects=True)

    # List page
    page = client.get("/me/receipts")
    assert page.status_code in (200, 304)

    # With date filters
    page2 = client.get("/me/receipts?start=2025-01-01&end=2025-01-31")
    assert page2.status_code in (200, 304)

    # Logout
    client.post("/logout", follow_redirects=True)
