# tests/test_accounting_endpoint_more.py
import os
import csv
import pytest


def _ensure_fallback_csv(app, rows):
    os.makedirs(app.instance_path, exist_ok=True)
    path = os.path.join(app.instance_path, "test.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="|")
        w.writerow(["JobID", "User", "End", "CPU_Core_Hours",
                    "GPU_Hours", "Mem_GB_Hours_Used", "Cost (฿)", "tier"])
        for r in rows:
            w.writerow([
                r["JobID"], r["User"], r["End"], r["CPU_Core_Hours"],
                r["GPU_Hours"], r["Mem_GB_Hours_Used"], r["Cost (฿)"], r["tier"]
            ])
    return path


@pytest.mark.db
def test_admin_accounting_family_multiple_windows(client, admin_user, app):
    # Seed fallback with a couple of days + different tiers for ledger export
    _ensure_fallback_csv(app, [
        {"JobID": "j-001", "User": "admin", "End": "2025-01-10T00:00:00Z", "CPU_Core_Hours": "1",
         "GPU_Hours": "0", "Mem_GB_Hours_Used": "0", "Cost (฿)": "50", "tier": "mu"},
        {"JobID": "j-002", "User": "admin", "End": "2025-01-15T00:00:00Z", "CPU_Core_Hours": "2",
         "GPU_Hours": "1", "Mem_GB_Hours_Used": "0", "Cost (฿)": "150", "tier": "gov"},
    ])

    # Admin landing (sanity)
    home = client.get("/admin")
    assert home.status_code in (200, 304)

    # Ledger CSV (derived) — primary supported export
    led = client.get(
        "/admin/export/ledger.csv?start=2025-01-01&end=2025-01-31&mode=derived"
    )
    assert led.status_code == 200
    assert "csv" in led.headers.get("Content-Type", "").lower()
    assert led.data  # has bytes


@pytest.mark.db
def test_admin_accounting_csv_is_absent(client, admin_user):
    # This build intentionally doesn’t have /admin/export/accounting.csv
    acc = client.get(
        "/admin/export/accounting.csv?start=2025-01-01&end=2025-01-31"
    )
    assert acc.status_code == 404
