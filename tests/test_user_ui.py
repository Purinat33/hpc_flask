# tests/test_user_ui.py
from __future__ import annotations
import io
from contextlib import contextmanager
from datetime import date, timedelta
import pandas as pd
import pytest
from flask import template_rendered
from tests.utils import login_user, login_admin
from models.billing_store import list_receipts, get_receipt_with_items
from models.db import get_db


# Helper to capture Jinja context without parsing HTML
@contextmanager
def captured_templates(app):
    recorded: list[tuple] = []

    def receiver(sender, template, context, **extra):
        recorded.append((template, context))

    template_rendered.connect(receiver, app)
    try:
        yield recorded
    finally:
        template_rendered.disconnect(receiver, app)


def _df(*rows):
    cols = ["User", "JobID", "Elapsed", "TotalCPU", "ReqTRES", "End", "State"]
    if not rows:
        # return an empty DF with the right schema so user.create_receipt can access JobID safely
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


def test_me_redirects_admin_to_admin_page(client):
    login_admin(client)
    r = client.get("/me", follow_redirects=False)
    # user.py redirects admins off /me to the admin dashboard
    assert r.status_code in (302, 303)
    assert "/admin" in r.headers["Location"]


def test_my_usage_detail_hides_billed_jobs(client, app, monkeypatch):
    login_user(client, "alice", "alice")

    # one billed, one unbilled
    df = _df({"JobID": "J1"}, {"JobID": "J2"})
    # monkeypatch *inside the user controller* (it imported the symbol)
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids",
                        lambda: {"J2"})  # only J2 is already billed

    with captured_templates(app) as rec:
        r = client.get(f"/me?view=detail&before={date.today().isoformat()}")
        assert r.status_code == 200

    # Grab context passed to template
    _, ctx = rec[-1]
    rows = ctx["rows"]
    assert len(rows) == 1 and rows[0]["JobID"] == "J1"  # billed filtered out
    assert ctx["view"] == "detail"
    assert ctx["total_cost"] >= 0.0


def test_my_usage_aggregate_builds_single_row(client, app, monkeypatch):
    login_user(client, "alice", "alice")
    df = _df({"JobID": "A"}, {"JobID": "B"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids", lambda: set())

    with captured_templates(app) as rec:
        r = client.get(f"/me?view=aggregate&before={date.today().isoformat()}")
        assert r.status_code == 200

    _, ctx = rec[-1]
    agg = ctx["agg_rows"]
    assert len(agg) == 1
    assert agg[0]["jobs"] == 2
    # cost fields exist (value depends on compute_costs but should be numeric)
    assert isinstance(agg[0]["Cost (฿)"], float)


def test_my_usage_billed_view_sums(client, app, monkeypatch):
    login_user(client, "alice", "alice")

    def fake_items(username, status):
        base = {
            "start": "1970-01-01",
            "end": "1970-01-31",
            "job_id_display": "job-1",
            "cpu_core_hours": 1.0,
            "gpu_hours": 0.0,
            "mem_gb_hours": 2.0,
        }
        if status == "pending":
            return [
                dict(base, receipt_id=101, cost=10.0,
                     created_at="2025-01-01T00:00:00Z"),
                dict(base, receipt_id=102, cost=2.5,
                     created_at="2025-01-02T00:00:00Z"),
            ]
        else:
            return [
                dict(base, receipt_id=201, cost=1.0,
                     paid_at="2025-01-03T00:00:00Z"),
                dict(base, receipt_id=202, cost=4.0,
                     paid_at="2025-01-04T00:00:00Z"),
                dict(base, receipt_id=203, cost=0.5,
                     paid_at="2025-01-05T00:00:00Z"),
            ]

    monkeypatch.setattr(
        "controllers.user.list_billed_items_for_user", fake_items)

    with captured_templates(app) as rec:
        r = client.get(f"/me?view=billed&before={date.today().isoformat()}")
        assert r.status_code == 200

    _, ctx = rec[-1]
    assert ctx["sum_pending"] == pytest.approx(12.5)
    assert ctx["sum_paid"] == pytest.approx(5.5)


def test_my_usage_respects_end_cutoff(client, app, monkeypatch):
    login_user(client, "alice", "alice")
    # job ends far in the future: should be filtered out
    df = _df({"JobID": "FUT", "End": "2099-12-31T00:00:00"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids", lambda: set())

    with captured_templates(app) as rec:
        r = client.get(f"/me?view=detail&before={date.today().isoformat()}")
        assert r.status_code == 200
    _, ctx = rec[-1]
    assert ctx["rows"] == []  # filtered by cutoff


def test_my_usage_invalid_view_defaults_to_detail(client, app, monkeypatch):
    login_user(client, "alice", "alice")
    df = _df({"JobID": "X"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids", lambda: set())

    with captured_templates(app) as rec:
        r = client.get(
            f"/me?view=not-a-view&before={date.today().isoformat()}")
        assert r.status_code == 200
    _, ctx = rec[-1]
    assert ctx["view"] == "detail"


def test_my_usage_csv_downloads(client, monkeypatch):
    login_user(client, "alice", "alice")
    df = _df({"JobID": "CSV1"}, {"JobID": "CSV2"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))

    r = client.get(f"/me.csv?before={date.today().isoformat()}")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    # filename header present
    assert "attachment; filename=usage_alice" in r.headers.get(
        "Content-Disposition", "")
    body = r.get_data(as_text=True)
    assert "CSV1" in body and "CSV2" in body


def test_create_receipt_no_jobs_audits_and_redirects(client, app, monkeypatch):
    login_user(client, "alice", "alice")
    # empty df -> noop path
    df = _df()[:0]
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    # Post without CSRF (disabled in tests)
    r = client.post(
        f"/me/receipt", data={"before": date.today().isoformat()}, follow_redirects=False)
    assert r.status_code in (302, 303)

    # confirm an audit row was written with the noop action
    db = get_db()
    row = db.execute(
        "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row and row["action"] == "receipt.create.noop"


def test_create_receipt_creates_and_redirects_to_receipts(client, app, monkeypatch):
    login_user(client, "alice", "alice")
    df = _df({"JobID": "MAKE1"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids", lambda: set())

    r = client.post(
        f"/me/receipt", data={"before": date.today().isoformat()}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/me/receipts" in r.headers["Location"]

    # Verify we actually created a receipt with items
    recs = list_receipts("alice")
    assert recs, "expected at least one receipt to be created"
    rid = recs[-1]["id"]
    rec, items = get_receipt_with_items(rid)
    assert rec["username"] == "alice" and len(items) == 1


def test_my_receipts_lists_user_receipts(client, app, monkeypatch):
    # make sure the user has at least one receipt
    test_create_receipt_creates_and_redirects_to_receipts(
        client, app, monkeypatch)
    with captured_templates(app) as rec:
        r = client.get("/me/receipts")
        assert r.status_code == 200
    _, ctx = rec[-1]
    assert ctx["receipts"] and all(
        r["username"] == "alice" for r in ctx["receipts"])


def test_view_receipt_owner_ok(client, app, monkeypatch):
    # create a receipt first
    login_user(client, "alice", "alice")
    df = _df({"JobID": "OWN1"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids", lambda: set())
    client.post(f"/me/receipt", data={"before": date.today().isoformat()})

    rid = list_receipts("alice")[-1]["id"]
    with captured_templates(app) as rec:
        r = client.get(f"/me/receipts/{rid}")
        assert r.status_code == 200
    _, ctx = rec[-1]
    assert ctx["r"]["id"] == rid
    assert ctx["is_owner"] is True


def test_view_receipt_admin_can_view_others(client, app, monkeypatch):
    # create receipt as alice
    login_user(client, "alice", "alice")
    df = _df({"JobID": "ADM1"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids", lambda: set())
    client.post(f"/me/receipt", data={"before": date.today().isoformat()})
    rid = list_receipts("alice")[-1]["id"]

    # view as admin
    login_admin(client)
    r = client.get(f"/me/receipts/{rid}")
    assert r.status_code == 200  # admin can view others' receipts


def test_view_receipt_non_owner_redirects_and_audits(client, app, monkeypatch):
    # create receipt as alice
    login_user(client, "alice", "alice")
    df = _df({"JobID": "NOPE"})
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks",
                        lambda *a, **k: (df.copy(), "fake", []))
    monkeypatch.setattr("controllers.user.billed_job_ids", lambda: set())
    client.post(f"/me/receipt", data={"before": date.today().isoformat()})
    rid = list_receipts("alice")[-1]["id"]

    # bob tries to view
    login_user(client, "bob", "bob")
    r = client.get(f"/me/receipts/{rid}", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/me/receipts" in r.headers["Location"]

    # audit recorded
    db = get_db()
    row = db.execute(
        "SELECT action, target FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["action"] == "receipt.view.denied"
    assert f"receipt={rid}" in row["target"]


def test_my_usage_collects_notes_on_exception(client, app, monkeypatch):
    login_user(client, "alice", "alice")
    def boom(*a, **k): raise RuntimeError("boom")
    monkeypatch.setattr("controllers.user.fetch_jobs_with_fallbacks", boom)
    with captured_templates(app) as rec:
        r = client.get(f"/me?view=detail&before={date.today().isoformat()}")
        assert r.status_code == 200
    _, ctx = rec[-1]
    assert any("boom" in n for n in ctx["notes"])
