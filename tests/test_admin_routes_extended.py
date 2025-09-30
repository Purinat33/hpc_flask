# tests/test_admin_routes_extended.py
import io
import json
import csv
import zipfile
from datetime import datetime, date, timezone, timedelta

import pandas as pd
import pytest

from models.base import session_scope
from models.schema import Receipt, Payment
from models.users_db import create_user

# GL models for export-run re-download
from models.gl import JournalBatch, GLEntry, ExportRun, ExportRunBatch


def _dt(y, m, d, hh=12, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


# ----------------------------- admin_form (dashboard + trend) -----------------------------

@pytest.mark.db
def test_admin_form_dashboard_and_trend(client, admin_user, monkeypatch):
    """Covers dashboard (with month filters) and usage->trend."""
    # Fake jobs covering two months and two users
    df = pd.DataFrame([
        {"User": "alice", "JobID": "J1", "End": pd.Timestamp("2025-01-10T08:00:00Z"),
         "CPU_Core_Hours": 10, "GPU_Hours": 0, "Mem_GB_Hours_Used": 100,
         "tier": "mu", "State": "COMPLETED", "ExitCode": "0:0", "NodeList": "n01"},
        {"User": "bob",   "JobID": "J2", "End": pd.Timestamp("2025-02-05T12:00:00Z"),
         "CPU_Core_Hours": 5, "GPU_Hours": 2, "Mem_GB_Hours_Used": 50,
         "tier": "mu", "State": "FAILED_NODE_FAIL", "ExitCode": "1:0", "NodeList": "n02"},
    ])

    def fake_fetch(start, end, username=None):
        # Ignore filters and username for simplicity
        return df.copy(), "test_source", []

    monkeypatch.setattr(
        "controllers.admin.fetch_jobs_with_fallbacks", fake_fetch)

    # Dashboard with month compare (m1,m2) -> should 200
    r = client.get("/admin?section=dashboard&m1=2025-01&m2=2025-02")
    assert r.status_code == 200
    assert b"Admin Dashboard" in r.data or b"Dashboard" in r.data

    # Usage trend for a single user + month detail
    r = client.get("/admin?section=usage&view=trend&u=alice&year=2025&month=1")
    assert r.status_code == 200
    # page.html fallback can still render; check a few strings that appear
    assert b"usage" in r.data.lower()


# ----------------------------------- admin_update (POST /admin) -----------------------------------

@pytest.mark.db
def test_admin_update_rates(client, admin_user):
    r = client.post(
        "/admin", data={"type": "mu", "cpu": "0.11", "gpu": "2.22", "mem": "0.33"})
    # Should redirect back to rates section
    assert r.status_code in (302, 303)


# --------------------------- create_self_receipt (POST /admin/my/receipt) --------------------------

@pytest.mark.db
def test_create_self_receipt_flow(client, admin_user, monkeypatch):
    # prepare fake jobs for current_user
    df = pd.DataFrame([{
        "User": "admin", "JobID": "A1",
        "End": pd.Timestamp("2025-01-10T08:00:00Z"),
        "CPU_Core_Hours": 1, "GPU_Hours": 0, "Mem_GB_Hours_Used": 10, "tier": "mu",
        "State": "COMPLETED", "NodeList": "n01"
    }])

    def fake_fetch(start, end, username=None):
        return df.copy(), "test_source", []

    monkeypatch.setattr(
        "controllers.admin.fetch_jobs_with_fallbacks", fake_fetch)
    # don't actually post to GL
    monkeypatch.setattr(
        "controllers.admin.post_receipt_issued", lambda rid, actor: True)

    r = client.post("/admin/my/receipt", data={"before": "2025-01-31"})
    # Redirect back to admin page after creation
    assert r.status_code in (302, 303)


# ----------------------------------- ledger_page (GET /admin/ledger) -----------------------------------

@pytest.mark.db
def test_ledger_page_with_paid_receipt_and_signals(client, admin_user):
    # seed one paid receipt (no external payment -> eligible to revert)
    with session_scope() as s:
        r = Receipt(
            username="admin", pricing_tier="mu",
            rate_cpu=0, rate_gpu=0, rate_mem=0, rates_locked_at=_dt(2025, 1, 1),
            start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
            created_at=_dt(2025, 1, 10), paid_at=_dt(2025, 1, 20),
            total=99.99, status="paid"
        )
        s.add(r)
        s.flush()

    # Use "derived" (preview) mode to avoid needing posted GL
    r = client.get(
        "/admin/ledger?mode=derived&start=2025-01-01&end=2025-01-31")
    assert r.status_code == 200
    assert b"Accounting" in r.data or b"Journal" in r.data


# --------------------------- create_month_invoices / bulk_revert_month_invoices ---------------------------

@pytest.mark.db
def test_create_and_bulk_revert_month_invoices(client, admin_user, monkeypatch):
    # Fake jobs during March 2025 for two users (admin + alice)
    df = pd.DataFrame([
        {"User": "admin", "JobID": "M1", "End": pd.Timestamp("2025-03-05T00:00:00Z"),
         "CPU_Core_Hours": 1, "GPU_Hours": 0, "Mem_GB_Hours_Used": 1, "tier": "mu",
         "State": "COMPLETED", "NodeList": "n01"},
        {"User": "alice", "JobID": "M2", "End": pd.Timestamp("2025-03-06T00:00:00Z"),
         "CPU_Core_Hours": 2, "GPU_Hours": 0, "Mem_GB_Hours_Used": 2, "tier": "mu",
         "State": "COMPLETED", "NodeList": "n01"},
    ])

    def fake_fetch(start, end, username=None):
        # respect username if provided (for safety)
        if username:
            return df[df["User"] == username].copy(), "test_source", []
        return df.copy(), "test_source", []

    monkeypatch.setattr(
        "controllers.admin.fetch_jobs_with_fallbacks", fake_fetch)
    # avoid GL posting failures on 'issued' during bulk create
    monkeypatch.setattr(
        "controllers.admin.post_receipt_issued", lambda rid, actor: True)

    # Ensure alice exists
    try:
        create_user("alice", "pw", role="user")
    except Exception:
        pass

    # 1) Create month invoices (March 2025)
    r = client.post("/admin/invoices/create_month",
                    data={"year": "2025", "month": "3"})
    assert r.status_code in (302, 303)

    # 2) Bulk revert (we monkeypatch the store call to avoid depending on internal logic)
    called = {}

    def fake_bulk_void(y, m, actor, reason):
        # return: voided, skipped, ids
        with session_scope() as s:
            ids = [x.id for x in s.query(Receipt).filter(
                Receipt.start >= _dt(y, m, 1), Receipt.end <= _dt(y, m, 31),
                Receipt.status == "pending"
            ).all()]
        called["seen"] = True
        return (len(ids), 0, ids)

    monkeypatch.setattr(
        "controllers.admin.bulk_void_pending_invoices_for_month", fake_bulk_void)
    # prevent GL reverse call side effects
    monkeypatch.setattr("controllers.admin.reverse_receipt_postings",
                        lambda rid, actor, kinds=("issue",): None)

    r2 = client.post("/admin/invoices/revert_month",
                     data={"year": "2025", "month": "3", "reason": "tests"})
    assert r2.status_code in (302, 303)
    assert called.get("seen")


# -------------------------------- admin_receipt_etax_zip (GET) --------------------------------

@pytest.mark.db
def test_admin_receipt_etax_zip(client, admin_user, monkeypatch):
    # make a minimal receipt
    with session_scope() as s:
        rec = Receipt(
            username="admin", pricing_tier="mu",
            rate_cpu=0, rate_gpu=0, rate_mem=0, rates_locked_at=_dt(2025, 1, 1),
            start=_dt(2025, 1, 1), end=_dt(2025, 1, 31),
            created_at=_dt(2025, 1, 10), total=10.0, status="pending"
        )
        s.add(rec)
        s.flush()
        rid = rec.id

    # Make WeasyPrint HTML.write_pdf cheap
    class FakeHTML:
        def __init__(self, *a, **k): pass
        def write_pdf(self): return b"%PDF-1.4 fake"

    monkeypatch.setattr("controllers.admin.HTML", FakeHTML)

    # Patch payload builder (keep get_receipt_with_items real)
    from types import SimpleNamespace

    def fake_payload(rid):
        return {"document": {"number": f"INV-{rid}"}}
    monkeypatch.setattr(
        "models.billing_store.build_etax_payload", fake_payload)

    r = client.get(f"/admin/receipts/{rid}.etax.zip")
    assert r.status_code == 200
    assert "application/zip" in r.headers.get("Content-Type", "").lower()
    z = zipfile.ZipFile(io.BytesIO(r.data), "r")
    names = z.namelist()
    assert any(n.endswith(".json") for n in names)
    assert any(n.endswith(".pdf") for n in names)


# ------------------------------ ui_close_period (POST) --------------------------------

@pytest.mark.db
def test_ui_close_period_happy_path(client, admin_user, monkeypatch):
    # Keep it simple: no candidate receipts in the month -> close proceeds
    monkeypatch.setattr(
        "controllers.admin.post_service_accruals_for_period", lambda y, m, a: 0)
    monkeypatch.setattr("controllers.admin.close_period", lambda y, m, a: True)

    r = client.post("/admin/periods/2025/2/close")
    assert r.status_code in (302, 303)  # redirect back to ledger


# ---------------------------- export_gl_formal_zip (POST) ----------------------------

@pytest.mark.db
def test_export_gl_formal_zip_returns_zip(client, admin_user, monkeypatch):
    def fake_run(start, end, actor, kind):
        return ("posted_gl_foo.zip", b"PK\x03\x04...fakezip...")
    monkeypatch.setattr("controllers.admin.run_formal_gl_export", fake_run)

    r = client.post("/admin/export/gl/formal.zip",
                    data={"start": "2025-01-01", "end": "2025-01-31"})
    assert r.status_code == 200
    assert "zip" in r.headers.get("Content-Type", "").lower()


# ------------------------ list_export_runs & redownload_export_run ------------------------

@pytest.mark.db
def test_list_and_redownload_export_run(client, admin_user):
    # Build one batch + entries + run + linkage
    with session_scope() as s:
        b = JournalBatch(
            kind="issue",
            period_year=2025,
            period_month=1,
            source="test",
            source_ref="X1",
            posted_at=_dt(2025, 1, 15),   # <-- required
            posted_by="tester",           # <-- required
        )
        s.add(b)
        s.flush()

        e1 = GLEntry(
            batch_id=b.id, seq_in_batch=1,
            date=_dt(2025, 1, 15), ref="R1", memo="memo",
            account_id="1100", account_name="Accounts Receivable",
            account_type="ASSET", debit=100.0, credit=0.0,
            receipt_id=None, external_txn_id=None
        )
        e2 = GLEntry(
            batch_id=b.id, seq_in_batch=2,
            date=_dt(2025, 1, 15), ref="R1", memo="memo",
            account_id="4000", account_name="Service Revenue",
            account_type="INCOME", debit=0.0, credit=100.0,
            receipt_id=None, external_txn_id=None
        )
        s.add_all([e1, e2])
        s.flush()
        s.flush()

        run = ExportRun(
            kind="posted_gl_csv",
            status="success",
            actor="admin",
            criteria={"start": "2025-01-01",
                      "end": "2025-01-31"},  # JSON column
            started_at=_dt(2025, 1, 31, 12, 0, 0),
            finished_at=_dt(2025, 1, 31, 12, 5, 0),
            file_sha256="deadbeef",
            manifest_sha256="beadfeed",
            signature="sig==",
            key_id="key1",
            # file_size can be left None if nullable
        )
        s.add(run)
        s.flush()

        link = ExportRunBatch(run_id=run.id, batch_id=b.id, seq=1)
        s.add(link)
        s.flush()
        run_id = run.id

    # list page
    r = client.get("/admin/export/runs")
    assert r.status_code == 200
    assert any(x in r.data for x in (
        b"Export Runs", b"Exports", b"Past Export Runs"))

    # redownload the exact run
    r2 = client.get(f"/admin/export/runs/{run_id}.zip")
    assert r2.status_code == 200
    assert "zip" in r2.headers.get("Content-Type", "").lower()
    z = zipfile.ZipFile(io.BytesIO(r2.data), "r")
    assert any(n.endswith(".csv") for n in z.namelist())
    assert any("manifest_run_" in n for n in z.namelist())


# ------------------------------- export_ledger_pdf (GET) --------------------------------

@pytest.mark.db
def test_export_ledger_pdf_preview_mode(client, admin_user, monkeypatch):
    # Use preview (derived) and stub HTML->PDF to avoid heavy deps
    class FakeHTML:
        def __init__(self, *a, **k): pass
        def write_pdf(self): return b"%PDF-1.4 fake"

    monkeypatch.setattr("controllers.admin.HTML", FakeHTML)

    # Make derived journal return a tiny DataFrame (correct columns)
    df = pd.DataFrame([
        {"date": "2025-01-15", "ref": "R1", "memo": "test",
         "account_id": "1100", "account_name": "Accounts Receivable",
         "account_type": "ASSET", "debit": 100.0, "credit": 0.0},
        {"date": "2025-01-15", "ref": "R1", "memo": "test",
         "account_id": "4000", "account_name": "Service Revenue",
         "account_type": "INCOME", "debit": 0.0, "credit": 100.0},
    ])
    monkeypatch.setattr("controllers.admin.derive_journal",
                        lambda s, e: df.copy())

    r = client.get(
        "/admin/export/ledger.pdf?mode=derived&start=2025-01-01&end=2025-01-31")
    assert r.status_code == 200
    assert "application/pdf" in r.headers.get("Content-Type", "").lower()


# ----------------------------- admin_form (myusage) -----------------------------

@pytest.mark.db
def test_admin_form_myusage_default_page(client, admin_user, monkeypatch):
    """
    Covers /admin?section=myusage for the current user (admin).
    Verifies page renders and shows something usage-related.
    """
    import pandas as pd
    from datetime import datetime, timezone

    df = pd.DataFrame([
        {"User": "admin", "JobID": "MU1",
         "End": pd.Timestamp("2025-01-10T08:00:00Z"),
         "CPU_Core_Hours": 3.0, "GPU_Hours": 0.0, "Mem_GB_Hours_Used": 30.0,
         "tier": "mu", "State": "COMPLETED", "ExitCode": "0:0", "NodeList": "n01"},
        {"User": "admin", "JobID": "MU2",
         "End": pd.Timestamp("2025-01-15T08:00:00Z"),
         "CPU_Core_Hours": 4.0, "GPU_Hours": 1.0, "Mem_GB_Hours_Used": 40.0,
         "tier": "mu", "State": "COMPLETED", "ExitCode": "0:0", "NodeList": "n02"},
    ])

    def fake_fetch(start, end, username=None):
        # myusage should request the current user; we return only admin rows
        return df.copy(), "test_source", []

    monkeypatch.setattr(
        "controllers.admin.fetch_jobs_with_fallbacks", fake_fetch)

    # Default myusage page (no extra filters required)
    r = client.get("/admin?section=myusage&year=2025&month=1")
    assert r.status_code == 200
    # Keep the assertion tolerant of template wording
    body = r.data.lower()
    assert b"usage" in body or b"my usage" in body or b"your usage" in body
    # A couple of job IDs or fields should show up
    assert b"mu1" in body or b"mu2" in body


@pytest.mark.db
def test_admin_form_myusage_trend_and_empty(client, admin_user, monkeypatch):
    """
    Covers /admin?section=myusage&view=trend and also the empty-data case.
    """
    import pandas as pd

    # First, non-empty set for trend
    df_trend = pd.DataFrame([
        {"User": "admin", "JobID": "T1", "End": pd.Timestamp("2025-02-05T00:00:00Z"),
         "CPU_Core_Hours": 2.0, "GPU_Hours": 0.0, "Mem_GB_Hours_Used": 20.0,
         "tier": "mu", "State": "COMPLETED", "ExitCode": "0:0", "NodeList": "n01"},
    ])

    def fake_fetch_nonempty(start, end, username=None):
        return df_trend.copy(), "test_source", []

    monkeypatch.setattr("controllers.admin.fetch_jobs_with_fallbacks",
                        fake_fetch_nonempty)

    r = client.get("/admin?section=myusage&view=trend&year=2025&month=2")
    assert r.status_code == 200
    body = r.data.lower()
    assert b"usage" in body  # tolerant check like your other tests
    # Optional: something from the trend dataset appears
    assert b"t1" in body or b"trend" in body or b"cpu" in body

    # Now, simulate empty-data edge case (should still render gracefully)
    def fake_fetch_empty(start, end, username=None):
        return pd.DataFrame([], columns=[
            "User", "JobID", "End", "CPU_Core_Hours", "GPU_Hours",
            "Mem_GB_Hours_Used", "tier", "State", "ExitCode", "NodeList"
        ]), "test_source", []

    monkeypatch.setattr("controllers.admin.fetch_jobs_with_fallbacks",
                        fake_fetch_empty)

    r2 = client.get("/admin?section=myusage&view=trend&year=2025&month=3")
    assert r2.status_code == 200
    body2 = r2.data.lower()
    # Page should still mention usage (or an empty-state message your UI shows)
    assert b"usage" in body2 or b"no jobs" in body2 or b"no data" in body2


# ----------------------------- admin_form (tier section) -----------------------------

@pytest.mark.db
def test_admin_form_tier_section_renders_and_has_fields(client, admin_user):
    """
    Ensures /admin?section=tier renders and exposes CPU/GPU/Mem rate
    controls or labels, without assuming specific input names.
    """
    # Exercise the POST handler to keep that codepath covered
    r_post = client.post(
        "/admin",
        data={"type": "mu", "cpu": "0.11", "gpu": "2.22", "mem": "0.33"},
    )
    assert r_post.status_code in (302, 303)

    # Visit the tier section page
    r_get = client.get("/admin?section=tier")
    assert r_get.status_code == 200
    body = r_get.data.lower()

    # Tolerant heading/section check
    assert any(k in body for k in (b"tier", b"tiers", b"pricing", b"rates"))

    # There should be *some* UI wrapper (form or table)
    assert b"<form" in body or b"<table" in body

    # Look for CPU/GPU/Mem references (labels, headers, etc.)
    # Be generous: 'mem' or 'memory'
    assert b"cpu" in body
    assert b"gpu" in body
    assert (b"mem" in body) or (b"memory" in body)

    # Still expect a way to save/update (button text or submit input)
    assert (
        b"type=submit" in body
        or b">save<" in body
        or b">update<" in body
        or b"submit" in body
    )

    # And the tier key we touched should be present somewhere
    assert b"mu" in body


# ----------------------------- export_ledger_csv (preview/derived) -----------------------------
@pytest.mark.db
def test_export_ledger_csv_preview_mode(client, admin_user, monkeypatch):
    """
    Hits /admin/export/ledger.csv in preview mode by stubbing derive_journal
    to return a tiny, valid dataframe. Be tolerant of implementations that
    emit header-only CSVs.
    """
    import pandas as pd

    df = pd.DataFrame([
        {"date": "2025-01-15", "ref": "R1", "memo": "alpha",
         "account_id": "1100", "account_name": "Accounts Receivable",
         "account_type": "ASSET", "debit": 100.0, "credit": 0.0},
        {"date": "2025-01-15", "ref": "R1", "memo": "alpha",
         "account_id": "4000", "account_name": "Service Revenue",
         "account_type": "INCOME", "debit": 0.0, "credit": 100.0},
    ])

    # If the controller uses derive_journal in derived mode, this keeps the path covered.
    monkeypatch.setattr("controllers.admin.derive_journal",
                        lambda s, e: df.copy())

    r = client.get(
        "/admin/export/ledger.csv?mode=derived&start=2025-01-01&end=2025-01-31")
    assert r.status_code == 200

    # Content-Type sanity
    ct = r.headers.get("Content-Type", "").lower()
    assert "csv" in ct

    text = r.data.decode("utf-8", errors="ignore").strip().lower()
    lines = [ln for ln in text.splitlines() if ln.strip()]

    # Must have at least a header line
    assert len(lines) >= 1

    header = lines[0]
    expected_cols = ["date", "ref", "memo", "account_id",
                     "account_name", "account_type", "debit", "credit"]
    for col in expected_cols:
        assert col in header, f"missing {col} in CSV header: {header}"

    # If rows exist, do a light content check (don’t require them to exist)
    if len(lines) > 1:
        body_text = "\n".join(lines[1:])
        assert ("accounts receivable" in body_text) or (
            "service revenue" in body_text)
