# tests/test_user_endpoints.py
import pytest
from models.users_db import create_user
from models.billing_store import create_receipt_from_rows


def _rows(username="admin", tier="mu"):
    return [
        {"JobID": "j1", "Cost (฿)": 50, "CPU_Core_Hours": 1.0, "GPU_Hours": 0.0,
         "Mem_GB_Hours_Used": 0.0, "tier": tier, "User": username}
    ]


@pytest.mark.db
def test_me_pages_and_csv(client, tmp_path):
    # create & login as a NON-admin
    try:
        create_user("ciuser", "secret", role="user")
    except Exception:
        pass
    client.post("/login", data={"username": "ciuser",
                "password": "secret"}, follow_redirects=True)

    # provide a tiny fallback CSV so /me.csv doesn't need slurm/sacct
    p = tmp_path / "test.csv"
    p.write_text(
        "End|User|JobID|Elapsed|TotalCPU|CPUTimeRAW|ReqTRES|AllocTRES|AveRSS|State\n"
        "2025-01-15T12:00:00+07:00|ciuser|123|01:00:00|01:00:00|3600|cpu=1,mem=4G|cpu=1,mem=4G|1G|COMPLETED\n",
        encoding="utf-8"
    )
    client.application.config["FALLBACK_CSV"] = str(p)

    # /me should be 200 (not admin, so no redirect)
    r = client.get("/me")
    assert r.status_code == 200

    # /me.csv should be 200 and CSV
    r2 = client.get("/me.csv")
    assert r2.status_code == 200
    assert "csv" in r2.headers.get("Content-Type", "").lower()
    assert r2.data


@pytest.mark.db
def test_my_receipt_pdfs(client, admin_user):
    # create a receipt for admin and hit the user-facing PDFs & page
    rid, total, _ = create_receipt_from_rows(
        "admin", "2025-01-01", "2025-01-31", _rows())
    assert total == 50.0

    page = client.get(f"/me/receipts/{rid}")
    assert page.status_code == 200
    assert page.data  # some body returned

    for path in (f"/me/receipts/{rid}.pdf", f"/me/receipts/{rid}.th.pdf"):
        r = client.get(path)
        assert r.status_code == 200
        assert "pdf" in r.headers.get("Content-Type", "").lower()
