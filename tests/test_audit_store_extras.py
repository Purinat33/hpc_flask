# tests/test_audit_store_extras.py
from __future__ import annotations
import json
from sqlalchemy import select
from models.audit_store import audit, list_audit, export_csv
from models.base import SessionLocal
from models.schema import AuditLog


def _last_audit_row():
    with SessionLocal() as s:
        return s.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        ).scalar_one()


def test_audit_without_request_context_and_with_actor_override(app):
    with app.app_context():
        audit("unit.noctx", target="x=1", status=201,
              extra={"k": 1}, actor="robot")
        row = _last_audit_row()
        assert row.action == "unit.noctx"
        assert row.actor == "robot"  # override respected
        assert row.method is None and row.path is None and row.ip is None
        # extra stored as JSON text
        extra = json.loads(row.extra or "{}")
        assert extra == {"k": 1}


def test_audit_with_request_context_captures_method_and_path(app):
    with app.test_request_context("/foo?bar=baz", method="GET", headers={"User-Agent": "pytest-UA"}):
        audit("unit.ctx", target="foo", status=200)

    row = _last_audit_row()
    assert row.action == "unit.ctx"
    assert row.method == "GET"
    assert row.path == "/foo"


def test_list_audit_limit_and_export_csv(app):
    with app.app_context():
        # seed a couple rows
        audit("seed.1")
        audit("seed.2")

        # limit works
        rows = list_audit(limit=1)
        assert len(rows) == 1

        # csv export
        fname, csv_text = export_csv()
        assert fname == "audit_export.csv"
        lines = csv_text.strip().splitlines()
        assert lines[0].startswith("id,ts,actor,ip,ua,method,path,action")
        # includes at least one of our seeds
        assert any("seed." in line for line in lines[1:])
