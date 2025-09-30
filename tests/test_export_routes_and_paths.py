# tests/test_export_routes_and_paths.py
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
def test_ledger_csv_route_with_fallback_file(client, admin_user, app):
    # Seed the fallback file that /admin/export/ledger.csv reads when needed
    _ensure_fallback_csv(app, [
        {"JobID": "j-001", "User": "admin", "End": "2025-01-10T00:00:00Z", "CPU_Core_Hours": "1",
         "GPU_Hours": "0", "Mem_GB_Hours_Used": "0", "Cost (฿)": "50", "tier": "mu"},
        {"JobID": "j-002", "User": "admin", "End": "2025-01-15T00:00:00Z", "CPU_Core_Hours": "2",
         "GPU_Hours": "1", "Mem_GB_Hours_Used": "0", "Cost (฿)": "150", "tier": "gov"},
    ])

    # Existing, supported route in this build:
    r = client.get(
        "/admin/export/ledger.csv?start=2025-01-01&end=2025-01-31&mode=derived")
    assert r.status_code == 200
    assert "csv" in r.headers.get("Content-Type", "").lower()
    assert r.data  # bytes present
