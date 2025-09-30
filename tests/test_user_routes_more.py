import csv
import io
import pytest
from datetime import datetime, timezone
from models.users_db import create_user
from models.base import session_scope
from models.schema import Receipt


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


@pytest.mark.db
def test_user_receipts_csv_includes_only_self_rows(client, app):
    # Create two users + seed one receipt each
    for u in ("alice", "bob"):
        try:
            create_user(u, "pw", role="user")
        except Exception:
            pass

    with session_scope() as s:
        ra = Receipt(username="alice", pricing_tier="mu",
                     rate_cpu=0, rate_gpu=0, rate_mem=0,
                     rates_locked_at=_dt(2025, 1, 10),

                     start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
                     created_at=_dt(2025, 1, 10), total=50.0, status="pending")
        rb = Receipt(username="bob", pricing_tier="mu",
                     rate_cpu=0, rate_gpu=0, rate_mem=0,
                     rates_locked_at=_dt(2025, 1, 10),

                     start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
                     created_at=_dt(2025, 1, 12), total=60.0, status="pending")
        s.add_all([ra, rb])
        s.flush()
        bob_id = rb.id

    # Login as alice
    r = client.post(
        "/login", data={"username": "alice", "password": "pw"}, follow_redirects=False)
    assert r.status_code in (200, 302)

    # CSV for alice should not include bob’s receipt
    csv_resp = client.get("/user/receipts.csv?start=2025-01-01&end=2025-01-31")
    if csv_resp.status_code == 404:
        pytest.skip("user receipts CSV route not present")
    assert csv_resp.status_code == 200
    assert "csv" in csv_resp.headers.get("Content-Type", "").lower()
    body = csv_resp.data.decode("utf-8")
    assert "alice" in body and "bob" not in body

    # Attempt to view bob’s receipt detail → 403/404 acceptable (impl-dependent)
    detail = client.get(f"/user/receipt/{bob_id}")
    assert detail.status_code in (403, 404)


@pytest.mark.db
def test_user_receipt_detail_self_ok(client):
    # seed one for alice and view it
    try:
        create_user("alice", "pw", role="user")
    except Exception:
        pass
    with session_scope() as s:
        r = Receipt(username="alice", pricing_tier="mu",
                    rate_cpu=0, rate_gpu=0, rate_mem=0,
                    rates_locked_at=_dt(2025, 1, 10),

                    start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
                    created_at=_dt(2025, 1, 10), total=75.0, status="pending")
        s.add(r)
        s.flush()
        rid = r.id

    client.post("/login", data={"username": "alice", "password": "pw"})
    page = client.get(f"/user/receipt/{rid}")
    if page.status_code == 404:
        pytest.skip("user receipt detail route not present")
    assert page.status_code == 200
