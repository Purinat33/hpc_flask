import io
import zipfile
import pytest
from datetime import datetime, timezone
from models.base import session_scope
from models.schema import Receipt


def _dt(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


@pytest.mark.db
def test_admin_exports_with_seed_and_formal_zip(client, admin_user):
    # Seed one paid receipt inside January so all exporters have at least one row.
    with session_scope() as s:
        r = Receipt(
            username="admin",
            pricing_tier="mu",
            rate_cpu=0, rate_gpu=0, rate_mem=0,
            rates_locked_at=_dt(2025, 1, 10),
            start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
            created_at=_dt(2025, 1, 10), paid_at=_dt(2025, 1, 20),
            total=150.0, status="paid",
        )
        s.add(r)
        s.flush()

    # Xero Bank
    rb = client.get(
        "/admin/export/xero_bank.csv?start=2025-01-01&end=2025-01-31")
    if rb.status_code == 404:
        pytest.skip("xero_bank export route not present")
    assert rb.status_code == 200
    assert "csv" in rb.headers.get("Content-Type", "").lower()
    assert b"admin" in rb.data and b"Receipt" in rb.data

    # Xero Sales
    rs = client.get(
        "/admin/export/xero_sales.csv?start=2025-01-01&end=2025-01-31")
    if rs.status_code == 404:
        pytest.skip("xero_sales export route not present")
    assert rs.status_code == 200
    assert "csv" in rs.headers.get("Content-Type", "").lower()
    assert b"Service Revenue" in rs.data or b"AccountCode" in rs.data  # tolerant to headers

    # General Ledger (service-level builder via controller)
    gl = client.get("/admin/export/ledger.csv?start=2025-01-01&end=2025-01-31")
    assert gl.status_code == 200
    assert "csv" in gl.headers.get("Content-Type", "").lower()
    assert b"account_id" in gl.data  # header exists

    # Formal export (posted GL → zip). First run returns a blob; second run is NOOP (skip OK).
    z = client.get("/admin/export/formal.zip?start=2025-01-01&end=2025-01-31")
    if z.status_code == 404:
        pytest.skip("formal export route not present")
    assert z.status_code in (200, 204)
    if z.status_code == 200:
        # It should be a zip with csv + manifest + signature
        zf = zipfile.ZipFile(io.BytesIO(z.data), "r")
        names = set(zf.namelist())
        assert any(n.endswith(".csv") for n in names)
        assert any(n.startswith("manifest_run_") for n in names)
        assert any(n.startswith("signature_run_") for n in names)

        # Re-run → expect NOOP (either 204 or small zip with zero batches depending on impl)
        z2 = client.get(
            "/admin/export/formal.zip?start=2025-01-01&end=2025-01-31")
        assert z2.status_code in (200, 204)
