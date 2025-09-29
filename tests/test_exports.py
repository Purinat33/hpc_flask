# tests/test_exports.py
import pytest


@pytest.mark.db
def test_ledger_pdf_export(client, admin_user):
    r = client.get(
        "/admin/export/ledger_th.pdf?start=2025-01-01&end=2025-01-31&mode=derived")
    assert r.status_code == 200
    assert "pdf" in r.headers.get("Content-Type", "").lower()


@pytest.mark.db
def test_accounting_exports_xero(client, admin_user):
    # bank CSV
    r1 = client.get(
        "/admin/export/xero_bank.csv?start=2025-01-01&end=2025-01-31")
    assert r1.status_code == 200
    assert "csv" in r1.headers.get("Content-Type", "").lower()
    assert r1.data  # header-only is fine when DB empty

    # sales CSV
    r2 = client.get(
        "/admin/export/xero_sales.csv?start=2025-01-01&end=2025-01-31")
    assert r2.status_code == 200
    assert "csv" in r2.headers.get("Content-Type", "").lower()
    assert r2.data


@pytest.mark.db
def test_ledger_csv_export(client, admin_user):
    r = client.get("/admin/export/ledger.csv?start=2025-01-01&end=2025-01-31")
    assert r.status_code == 200
    assert "csv" in r.headers.get("Content-Type", "").lower()
    assert r.data
