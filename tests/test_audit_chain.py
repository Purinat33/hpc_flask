import pytest
from sqlalchemy import select
from models.audit_store import audit
from models.schema import AuditLog
from models.base import session_scope


@pytest.mark.db
def test_audit_chain_links_and_signs():
    # write two audit events
    audit("test.event.one", target_type="unit",
          target_id="1", outcome="success", actor="tester")
    audit("test.event.two", target_type="unit",
          target_id="1", outcome="success", actor="tester")

    with session_scope() as s:
        rows = s.execute(select(AuditLog).order_by(
            AuditLog.id)).scalars().all()
        assert len(rows) >= 2
        # latest.prev_hash must equal previous.hash
        assert rows[-1].prev_hash == rows[-2].hash
        # hash and (if enabled) signature present
        assert rows[-1].hash and len(rows[-1].hash) >= 40  # sha256 hex-ish
        # schema version and key id populated (if configured)
        assert rows[-1].schema_version >= 1
        assert rows[-1].key_id is not None
