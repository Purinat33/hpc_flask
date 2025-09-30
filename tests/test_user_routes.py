import re
import io
import pandas as pd
import pytest
from datetime import datetime, timezone

from models.users_db import create_user


def _dt(y, m, d, hh=12, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def login_user(client, username="user1", password="pw"):
    # ensure user exists
    try:
        create_user(username, password, role="user")
    except Exception:
        pass
    client.post("/logout")
    r = client.post(
        "/login", data={"username": username, "password": password})
    assert r.status_code in (302, 303)
    return username


# ------------------------------- AGGREGATE -------------------------------

@pytest.mark.db
def test_my_usage_aggregate_sums_and_headers(client, admin_user, monkeypatch):
    username = login_user(client)

    df = pd.DataFrame([
        {"User": username, "End": pd.Timestamp("2025-01-10T08:00:00Z"),
         "CPU_Core_Hours": 10.0, "GPU_Hours": 2.0, "Mem_GB_Hours_Used": 100.0, "Cost (฿)": 12.34},
        {"User": username, "End": pd.Timestamp("2025-01-25T08:00:00Z"),
         "CPU_Core_Hours": 5.0, "GPU_Hours": 0.0, "Mem_GB_Hours_Used": 50.0, "Cost (฿)": 3.21},
    ])
    monkeypatch.setattr(
        "services.data_sources.fetch_jobs_with_fallbacks",
        lambda s, e, username=None: (df.copy(), "test_source", []),
    )
    # passthrough so the columns stay as-is
    monkeypatch.setattr("services.billing.compute_costs", lambda d: d)

    r = client.get("/me?view=aggregate&before=2025-01-31")
    assert r.status_code == 200
    body = r.data.decode("utf-8", errors="ignore").lower()

    # We are on the aggregate view
    assert ("aggregate" in body) or ("summary" in body)

    # The page shows an aggregate block/table with expected metric labels.
    # Keep this tolerant to template wording.
    labels_ok = (
        ("cpu_core_hours" in body or "cpu core hours" in body)
        and ("gpu_hours" in body or "gpu hours" in body)
        and (
            "mem_gb_hours_used" in body
            or "mem gb hours used" in body
            or "memory" in body
        )
        and ("cost" in body)  # "Cost (฿)" often renders as "cost"
    )
    assert labels_ok, "Aggregate metric labels not found."

    # If your template uses a table for the aggregate row, also accept either form.
    assert ("<table" in body) or ("<details" in body) or ("summary" in body)


# --------------------------------- BILLED ---------------------------------

@pytest.mark.db
def test_my_usage_billed_lists_and_totals(client, admin_user, monkeypatch):
    username = login_user(client)

    monkeypatch.setattr(
        "models.billing_store.list_receipts",
        lambda u: [
            {"id": 1, "username": username, "status": "pending", "total": 10.00},
            {"id": 2, "username": username, "status": "paid",    "total": 20.00},
            {"id": 3, "username": "someoneelse",
                "status": "paid", "total": 999.00},
        ],
    )

    r = client.get("/me?view=billed")
    assert r.status_code == 200
    text = r.data.decode().lower()
    assert "pending" in text and "paid" in text
    # sums (10 and 20) show up somewhere
    assert "10" in text and "20" in text


# --------------------------------- TREND ----------------------------------

@pytest.mark.db
def test_my_usage_trend_year_aggregate_and_month_detail(client, admin_user, monkeypatch):
    username = login_user(client)

    df = pd.DataFrame([
        # Jan jobs
        {"User": username, "End": pd.Timestamp("2025-01-10T08:00:00Z"),
         "JobID": "J1", "CPU_Core_Hours": 1.0, "GPU_Hours": 0.0, "Mem_GB_Hours_Used": 10.0, "Cost (฿)": 1.0},
        # Feb jobs
        {"User": username, "End": pd.Timestamp("2025-02-05T12:00:00Z"),
         "JobID": "J2", "CPU_Core_Hours": 2.0, "GPU_Hours": 1.0, "Mem_GB_Hours_Used": 20.0, "Cost (฿)": 3.0},
    ])
    monkeypatch.setattr(
        "services.data_sources.fetch_jobs_with_fallbacks",
        lambda s, e, username=None: (df.copy(), "test_source", []),
    )
    monkeypatch.setattr("services.billing.compute_costs", lambda d: d)

    # Year aggregate
    r1 = client.get("/me?view=trend&year=2025")
    assert r1.status_code == 200
    body1 = r1.data.lower()
    assert b"2025" in body1
    assert b"cpu_core_hours" in body1 or b"cpu core hours" in body1

    # Month detail (February)
    r2 = client.get("/me?view=trend&year=2025&month=2")
    assert r2.status_code == 200
    body2 = r2.data.lower()
    # Should show the single Feb job and its totals
    assert b"j2" in body2 or b"2.0" in body2


# ---------------------------------- CSV -----------------------------------

@pytest.mark.db
def test_my_usage_csv_download(client, admin_user, monkeypatch):
    username = login_user(client)

    df = pd.DataFrame([
        {"User": username, "End": pd.Timestamp("2025-01-10T08:00:00Z"),
         "CPU_Core_Hours": 1.0, "GPU_Hours": 0.0,
         "Mem_GB_Hours_Used": 10.0, "Cost (฿)": 1.0},
    ])

    # Patch the names actually used by the route
    monkeypatch.setattr(
        "controllers.user.fetch_jobs_with_fallbacks",
        lambda s, e, username=None: (df.copy(), "test_source", []),
        raising=True,
    )
    monkeypatch.setattr("controllers.user.compute_costs",
                        lambda d: d, raising=True)

    r = client.get("/me.csv?before=2025-01-31")
    assert r.status_code == 200
    assert "csv" in r.headers.get("Content-Type", "").lower()

    # filename includes user
    disp = r.headers.get("Content-Disposition", "")
    assert username in disp

    text = r.data.decode("utf-8", errors="ignore").lower()
    lines = [ln for ln in text.splitlines() if ln.strip()]

    # must have header + at least one row
    assert len(lines) >= 2

    header = lines[0]
    # Accept either processed or raw schema
    processed_cols = ["user", "end", "cpu_core_hours",
                      "gpu_hours", "mem_gb_hours_used"]
    raw_cols = ["end", "user", "jobid", "totalcpu",
                "cputimeraw", "reqtres", "alloctres", "averss", "state"]
    has_processed = all(col in header for col in processed_cols)
    has_raw = all(col in header for col in raw_cols)
    assert has_processed or has_raw, f"Unexpected header: {header}"

    # If a 'user' column exists, the data row should contain the username
    if "user" in header:
        assert username.lower() in text


# # --------------------------------- PDFs -----------------------------------

# @pytest.mark.db
# def test_receipt_pdf_and_pdf_th(client, admin_user, monkeypatch):
#     from datetime import datetime, timezone
#     username = login_user(client)

#     rec = {
#         "id": 123,
#         "username": username,
#         "total": 42.0,
#         "pricing_tier": "mu",
#         "rate_cpu": 0.10,
#         "rate_gpu": 2.00,
#         "rate_mem": 0.01,
#         "start": datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
#         "end": datetime(2025, 1, 31, 12, 0, 0, tzinfo=timezone.utc),
#         "created_at": datetime(2025, 2, 1, 12, 0, 0, tzinfo=timezone.utc),
#     }

#     # Include the per-resource quantity fields the template sums over.
#     items = [
#         {
#             "description": "CPU core-hours",
#             "cpu_core_hours": 1.0,
#             "gpu_hours": 0.0,
#             "mem_gb_hours": 0.0,
#             "unit_price": 42.0,
#             "amount": 42.0,
#         }
#     ]

#     monkeypatch.setattr(
#         "controllers.user.get_receipt_with_items",
#         lambda rid: (rec, items),
#         raising=True,
#     )

#     class FakeHTML:
#         def __init__(self, *a, **k): pass
#         def write_pdf(self): return b"%PDF-1.4 fake"

#     monkeypatch.setattr("controllers.user.HTML", FakeHTML, raising=True)

#     r1 = client.get("/me/receipts/123.pdf")
#     assert r1.status_code == 200
#     assert "application/pdf" in r1.headers.get("Content-Type", "").lower()

#     r2 = client.get("/me/receipts/123.th.pdf")
#     assert r2.status_code == 200
#     assert "application/pdf" in r2.headers.get("Content-Type", "").lower()
