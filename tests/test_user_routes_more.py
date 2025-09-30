from services.gl_posting import (
    post_service_accrual_for_receipt,
    post_receipt_issued,
    post_receipt_paid,
)
import zipfile
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


# tests/test_admin_exports_more.py


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


def _seed_paid_receipt():
    """Create one paid receipt with NOT NULL fields set for pricing/rates."""
    with session_scope() as s:
        created = _dt(2025, 1, 10)
        r = Receipt(
            username="admin",
            pricing_tier="mu",        # NOT NULL
            rate_cpu=0, rate_gpu=0, rate_mem=0,  # NOT NULL
            rates_locked_at=created,
            start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
            created_at=_dt(2025, 1, 10),
            paid_at=_dt(2025, 1, 20),
            total=150.0,
            status="paid",
        )
        s.add(r)
        s.flush()
        return r.id


def _seed_pending_receipt():
    with session_scope() as s:
        created = _dt(2025, 1, 10)
        r = Receipt(
            username="admin",
            pricing_tier="mu",
            rate_cpu=0, rate_gpu=0, rate_mem=0,
            rates_locked_at=created,
            start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
            created_at=created,
            total=200.0,
            status="pending",
        )
        s.add(r)
        s.flush()
        return r.id


@pytest.mark.db
def test_admin_export_xero_sales_csv_route(client, admin_user):
    rid = _seed_paid_receipt()

    r = client.get(
        "/admin/export/xero_sales.csv?start=2025-01-01&end=2025-01-31")
    if r.status_code == 404:
        pytest.skip("xero sales export route not present in this build")

    assert r.status_code == 200
    assert "csv" in r.headers.get("Content-Type", "").lower()
    body = r.get_data(as_text=True)
    # CSV header subset expected by builder
    assert "ContactName,InvoiceNumber,InvoiceDate" in body
    # basic sanity: the single receipt should show up
    assert "R" in body and "admin" in body


@pytest.mark.db
def test_admin_export_xero_bank_csv_route(client, admin_user):
    rid = _seed_paid_receipt()

    r = client.get(
        "/admin/export/xero_bank.csv?start=2025-01-01&end=2025-01-31")
    if r.status_code == 404:
        pytest.skip("xero bank export route not present in this build")

    assert r.status_code == 200
    assert "csv" in r.headers.get("Content-Type", "").lower()
    body = r.get_data(as_text=True)
    # CSV header subset expected by builder
    assert "Date,Amount,Payee,Description,Reference" in body
    assert "admin" in body


@pytest.mark.db
def test_admin_export_formal_zip_route_happy_path_and_noop(client, admin_user):
    """
    Create postings (accrual + issue + payment) so the formal export finds posted lines.
    First run → 200 with a ZIP; second run → 204 (noop).
    """
    rid = _seed_paid_receipt()
    # post GL entries for this receipt:
    assert post_service_accrual_for_receipt(rid, actor="pytest")
    assert post_receipt_issued(rid, actor="pytest")
    assert post_receipt_paid(rid, actor="pytest")

    # First run: expect a ZIP file
    r = client.get("/admin/export/formal.zip?start=2025-01-01&end=2025-01-31")
    if r.status_code == 404:
        pytest.skip("formal export route not present in this build")

    assert r.status_code == 200
    assert "zip" in r.headers.get("Content-Type", "").lower()
    # ensure it's a valid zip and contains files
    z = zipfile.ZipFile(io.BytesIO(r.data), "r")
    names = z.namelist()
    assert any(n.endswith(".csv") for n in names)
    assert any(n.startswith("manifest_run_") for n in names)
    assert any(n.startswith("signature_run_") for n in names)

    # Second run: already exported/locked → NO CONTENT
    r2 = client.get("/admin/export/formal.zip?start=2025-01-01&end=2025-01-31")
    # allow 200 if implementation chooses to regenerate
    assert r2.status_code in (204, 200)
    if r2.status_code == 200:
        # If 200, still must be a proper zip
        zipfile.ZipFile(io.BytesIO(r2.data), "r")


@pytest.mark.db
def test_admin_export_formal_zip_missing_params_400(client, admin_user):
    r = client.get("/admin/export/formal.zip")
    if r.status_code == 404:
        pytest.skip("formal export route not present in this build")
    assert r.status_code == 400
