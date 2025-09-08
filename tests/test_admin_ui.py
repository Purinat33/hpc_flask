# tests/test_admin_ui.py
from __future__ import annotations
from datetime import date
from contextlib import contextmanager
import pandas as pd
import pytest
from flask import template_rendered
from tests.utils import login_admin
from models.rates_store import load_rates
from models.billing_store import (
    create_receipt_from_rows, list_receipts, get_receipt_with_items,
    mark_receipt_paid as mark_paid_fn, void_receipt,
)
from models.db import get_db


@contextmanager
def captured_templates(app):
    rec = []

    def receiver(sender, template, context, **extra):
        rec.append((template, context))
    template_rendered.connect(receiver, app)
    try:
        yield rec
    finally:
        template_rendered.disconnect(receiver, app)


def _df(*rows):
    cols = ["User", "JobID", "Elapsed", "TotalCPU", "ReqTRES", "End", "State"]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{
        "User": r.get("User", "alice"),
        "JobID": r["JobID"],
        "Elapsed": r.get("Elapsed", "01:00:00"),
        "TotalCPU": r.get("TotalCPU", "01:00:00"),
        "ReqTRES": r.get("ReqTRES", "cpu=1,mem=1G"),
        "End": r.get("End", date.today().isoformat() + "T00:00:00"),
        "State": r.get("State", "COMPLETED"),
    } for r in rows])


def test_admin_rates_section_and_update(client, app):
    login_admin(client)
    with captured_templates(app) as rec:
        r = client.get("/admin?section=rates&type=mu")
        assert r.status_code == 200
    _, ctx = rec[-1]
    assert set(ctx["current"].keys()) == {"cpu", "gpu", "mem"}

    # form update (valid)
    r2 = client.post(
        "/admin", data={"type": "gov", "cpu": "1.1", "gpu": "2.2", "mem": "3.3"})
    assert r2.status_code in (302, 303)
    assert load_rates()["gov"] == {"cpu": 1.1, "gpu": 2.2, "mem": 3.3}

    # invalid tier -> redirect to rates default (no crash)
    r3 = client.post(
        "/admin", data={"type": "nope", "cpu": "1", "gpu": "1", "mem": "1"})
    assert r3.status_code in (302, 303)

    # negative value blocked
    r4 = client.post(
        "/admin", data={"type": "mu", "cpu": "-1", "gpu": "1", "mem": "1"})
    assert r4.status_code in (302, 303)


def test_admin_usage_filters_billed_and_aggregates(client, app, monkeypatch):
    login_admin(client)
    df = _df({"User": "alice", "JobID": "BILLED.123"},
             {"User": "bob",   "JobID": "UNBILLED.1"},
             {"User": "alice", "JobID": "UNBILLED.2"})
    # make the filter exact on the full JobID to avoid any canonicalization surprises
    monkeypatch.setattr("controllers.admin.canonical_job_id", lambda s: s)
    monkeypatch.setattr("controllers.admin.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake-admin-source", []))
    monkeypatch.setattr("controllers.admin.billed_job_ids",
                        lambda: {"BILLED.123"})

    with captured_templates(app) as rec:
        r = client.get(
            f"/admin?section=usage&before={date.today().isoformat()}")
        assert r.status_code == 200
    _, ctx = rec[-1]
    rows = ctx["rows"]
    ids = {row["JobID"] for row in rows}
    assert ids == {"UNBILLED.1", "UNBILLED.2"}  # BILLED.123 filtered out
    assert isinstance(ctx["grand_total"], float)
    assert isinstance(ctx["tot_cpu"], float)


def test_admin_myusage_billed_view_sums_and_lists(client, app, monkeypatch):
    login_admin(client)

    def fake_items(username, status):
        base = {
            "start": "1970-01-01", "end": "1970-01-31",
            "job_id_display": "job-1", "cpu_core_hours": 1.0,
            "gpu_hours": 0.0, "mem_gb_hours": 2.0,
        }
        if status == "pending":
            return [
                dict(base, receipt_id=501, cost=10.0,
                     created_at="2025-01-01T00:00:00Z"),
                dict(base, receipt_id=502, cost=2.5,
                     created_at="2025-01-02T00:00:00Z"),
            ]
        return [
            dict(base, receipt_id=601, cost=1.0,
                 paid_at="2025-01-03T00:00:00Z"),
            dict(base, receipt_id=602, cost=4.0,
                 paid_at="2025-01-04T00:00:00Z"),
            dict(base, receipt_id=603, cost=0.5,
                 paid_at="2025-01-05T00:00:00Z"),
        ]

    monkeypatch.setattr(
        "controllers.admin.list_billed_items_for_user", fake_items)

    with captured_templates(app) as rec:
        r = client.get(
            f"/admin?section=myusage&view=billed&before={date.today().isoformat()}")
        assert r.status_code == 200
    _, ctx = rec[-1]
    assert ctx["sum_pending"] == pytest.approx(12.5)
    assert ctx["sum_paid"] == pytest.approx(5.5)
    # receipts lists exist (might be empty if none created yet, but keys exist)
    assert "my_pending_receipts" in ctx and "my_paid_receipts" in ctx


def test_admin_create_self_receipt_and_my_csv(client, app, monkeypatch):
    login_admin(client)
    df = _df({"User": "admin", "JobID": "ADMSELF.1"})
    monkeypatch.setattr("controllers.admin.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake-admin-self", []))
    monkeypatch.setattr("controllers.admin.billed_job_ids", lambda: set())

    r = client.post("/admin/my/receipt", data={"before": date.today().isoformat()},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "section=myusage" in r.headers["Location"] and "view=billed" in r.headers["Location"]

    r2 = client.get(f"/admin/my.csv?before={date.today().isoformat()}")
    assert r2.status_code == 200
    assert r2.mimetype == "text/csv"
    assert "ADMSELF.1" in r2.get_data(as_text=True)


def test_admin_mark_paid_endpoint_and_paid_csv(client, app):
    # make a pending receipt for alice
    rows = [{"JobID": "RCP1.1",
             "Cost (฿)": 3.0, "CPU_Core_Hours": 1, "GPU_Hours": 0, "Mem_GB_Hours": 1}]
    rid, *_ = create_receipt_from_rows("alice",
                                       "1970-01-01", "1970-01-31", rows)

    login_admin(client)
    # mark paid via endpoint
    r = client.post(f"/admin/receipts/{rid}/paid", follow_redirects=False)
    assert r.status_code in (302, 303)

    # verify in DB
    rec, _ = get_receipt_with_items(rid)
    assert rec["status"] == "paid" and rec["paid_at"]

    # csv export contains our receipt
    r2 = client.get("/admin/paid.csv")
    assert r2.status_code == 200 and r2.mimetype == "text/csv"
    body = r2.get_data(as_text=True)
    assert "paid_receipts_history.csv" in r2.headers.get(
        "Content-Disposition", "")
    assert f"{rid},alice," in body and ",paid," in body

    # void flow stays void (blocked from becoming paid)
    rid2, *_ = create_receipt_from_rows("alice",
                                        "1970-01-01", "1970-01-31", rows)
    void_receipt(rid2)
    r3 = client.post(f"/admin/receipts/{rid2}/paid", follow_redirects=False)
    assert r3.status_code in (302, 303)
    rec2, _ = get_receipt_with_items(rid2)
    assert rec2["status"] == "void"
    # and the helper returns False in this state
    assert mark_paid_fn(rid2, "admin") is False
