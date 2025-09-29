# tests/test_admin_periods_endpoints.py
import pytest


@pytest.mark.db
def test_bootstrap_close_reopen_period(client, admin_user):
    # Bootstrap periods (POST)
    r0 = client.post("/admin/periods/bootstrap",
                     data={}, follow_redirects=False)
    assert r0.status_code in (302, 303)

    # Close 2025-01
    r1 = client.post("/admin/periods/2025-1/close",
                     data={}, follow_redirects=False)
    assert r1.status_code in (302, 303)

    # Reopen 2025-01
    r2 = client.post("/admin/periods/2025-1/reopen",
                     data={}, follow_redirects=False)
    assert r2.status_code in (302, 303)
