import pytest
from datetime import datetime


@pytest.mark.db
def test_simulate_rates_json(client, admin_user, tmp_path):
    # Build a tiny fallback CSV the route can read
    p = tmp_path / "test.csv"
    p.write_text(
        "End|User|JobID|Elapsed|TotalCPU|CPUTimeRAW|ReqTRES|AllocTRES|AveRSS|State\n"
        "2025-01-15T12:00:00+07:00|admin|123|01:00:00|01:00:00|3600|cpu=1,mem=4G|cpu=1,mem=4G|1G|COMPLETED\n",
        encoding="utf-8"
    )

    # Point the app to our CSV so fetch_jobs_with_fallbacks() succeeds
    client.application.config["FALLBACK_CSV"] = str(p)

    # Provide an end window that includes the row above
    r = client.get(
        "/admin/simulate_rates.json?before=2025-01-31&cpu_mu=1&gpu_mu=5&mem_mu=0.5")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)
    # sanity checks on structure
    assert "window" in data and data["window"]["end"] == "2025-01-31"
    assert "rates" in data and "candidate" in data["rates"]
    # depending on the wrapper shape
    assert "candidate_by_tier" in data or "by_tier" in data
