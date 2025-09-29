import pytest


@pytest.mark.db
def test_ledger_pdf_export(client, admin_user):
    r = client.get(
        "/admin/export/ledger_th.pdf?start=2025-01-01&end=2025-01-31&mode=derived")
    assert r.status_code == 200  # since we’re logged in now
    assert "pdf" in r.headers.get("Content-Type", "").lower()
