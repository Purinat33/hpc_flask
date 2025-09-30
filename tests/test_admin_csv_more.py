# tests/test_admin_csv_more.py
import os
import pytest
import pandas as pd
from io import StringIO

CSV_CONTENT = """JobID|User|End|CPU_Core_Hours|GPU_Hours|Mem_GB_Hours_Used|Cost (฿)|tier
j-001|admin|2025-01-10T00:00:00Z|1|0|0|50|mu
"""


def _ensure_fallback_csv(app):
    # Best-effort: write to Flask instance path (works on most setups)
    os.makedirs(app.instance_path, exist_ok=True)
    path = os.path.join(app.instance_path, "test.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(CSV_CONTENT)
    return path


@pytest.mark.db
def test_admin_home_and_basic_csvs(client, admin_user, app, monkeypatch):
    _ensure_fallback_csv(app)

    # Patch pandas.read_csv so anything trying to read ".../instance/test.csv"
    # gets our in-memory CSV instead of hitting the filesystem.
    real_read_csv = pd.read_csv

    def _patched_read_csv(path, *args, **kwargs):
        path_str = str(path).replace("\\", "/")
        if "/instance/test.csv" in path_str:
            return real_read_csv(StringIO(CSV_CONTENT), sep="|", keep_default_na=False, dtype=str)
        return real_read_csv(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", _patched_read_csv, raising=True)

    # Admin landing page
    home = client.get("/admin")
    assert home.status_code in (200, 304)

    # My CSV (unpaid/issued window)
    my_csv = client.get("/admin/my.csv?start=2025-01-01&end=2025-01-31")
    assert my_csv.status_code == 200
    assert "csv" in my_csv.headers.get("Content-Type", "").lower()

    # Paid CSV (should still succeed even if empty)
    paid_csv = client.get("/admin/paid.csv?start=2025-01-01&end=2025-01-31")
    assert paid_csv.status_code == 200
    assert "csv" in paid_csv.headers.get("Content-Type", "").lower()

    # Audit CSV (login wrote an event)
    audit_csv = client.get("/admin/audit.csv?start=2025-01-01&end=2025-01-31")
    assert audit_csv.status_code == 200
    assert "csv" in audit_csv.headers.get("Content-Type", "").lower()

    # Ledger CSV (derived mode)
    ledger_csv = client.get(
        "/admin/export/ledger.csv?start=2025-01-01&end=2025-01-31&mode=derived"
    )
    assert ledger_csv.status_code == 200
    assert "csv" in ledger_csv.headers.get("Content-Type", "").lower()
