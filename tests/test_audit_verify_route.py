# tests/test_audit_verify_route.py
import pytest
from models.audit_store import audit


@pytest.mark.db
def test_admin_audit_verify_json(client, admin_user):
    # Create a couple of audit events
    audit("test.event.alpha", target_type="unit", target_id="1",
          outcome="success", status=200, actor="tester")
    audit("test.event.beta",  target_type="unit", target_id="1",
          outcome="success", status=200, actor="tester")

    # Verify via route
    r = client.get("/admin/audit.verify.json?limit=10")
    assert r.status_code in (200, 409)  # normally 200 unless chain broken
    data = r.get_json()
    assert "ok" in data
    assert isinstance(data.get("checked", 0), int)
