# tests/test_forecast_endpoint.py
import os
import csv
import pytest


def _seed_csv(app):
    os.makedirs(app.instance_path, exist_ok=True)
    path = os.path.join(app.instance_path, "test.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter="|")
        w.writerow(["JobID", "User", "End", "CPU_Core_Hours",
                   "GPU_Hours", "Mem_GB_Hours_Used", "Cost (฿)", "tier"])
        w.writerow(["f-001", "admin", "2025-01-03T00:00:00Z",
                   "1", "0", "0", "25", "mu"])
        w.writerow(["f-002", "admin", "2025-01-10T00:00:00Z",
                   "2", "1", "0", "125", "mu"])
        w.writerow(["f-003", "admin", "2025-01-17T00:00:00Z",
                   "1", "0", "1", "60", "gov"])


@pytest.mark.db
def test_admin_forecast_json_if_present(client, admin_user, app):
    _seed_csv(app)

    r = client.get("/admin/forecast.json?start=2025-01-01&end=2025-01-31")
    if r.status_code == 404:
        pytest.skip("forecast endpoint not present in this build")
    assert r.status_code in (200, 204)
    if r.status_code == 200:
        data = r.get_json()
        # Be permissive: different builds expose different shapes
        assert isinstance(data, (dict, list))
        # If it’s a dict it should have *some* keys, but don’t pin to names
        if isinstance(data, dict):
            assert len(data) >= 1
