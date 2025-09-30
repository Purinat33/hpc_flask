import pytest
from models import rates_store


@pytest.mark.db
def test_formula_post_bad_numbers_returns_400(client, admin_user):
    # non-numeric cpu/gpu/mem should be rejected
    r = client.post(
        "/formula", json={"type": "mu", "cpu": "x", "gpu": "y", "mem": "z"})
    assert r.status_code == 400
    j = r.get_json()
    assert isinstance(j, dict) and "error" in j


@pytest.mark.db
def test_formula_post_updates_rates_and_get_reflects(client, admin_user):
    # valid numeric update on GOV
    r = client.post(
        "/formula", json={"type": "gov", "cpu": 2.0, "gpu": 6.0, "mem": 0.7})
    assert r.status_code == 200
    data = r.get_json()
    assert data and data.get("ok") is True

    # GET should reflect the new values (note: nested under "rates")
    g = client.get("/formula?type=gov")
    assert g.status_code == 200
    body = g.get_json()
    assert isinstance(body, dict)
    assert body.get("type", "").lower() == "gov"
    assert "rates" in body and isinstance(body["rates"], dict)
    assert body["rates"].get("cpu") == 2.0
    assert body["rates"].get("gpu") == 6.0
    assert body["rates"].get("mem") == 0.7
    # optional: schema extras
    assert body.get("currency") in (None, "THB")  # depends on your controller
    assert body.get("unit") in (None, "per-hour")

    # rates_store.load_rates() should also see the update under 'gov'
    live = rates_store.load_rates()
    assert "gov" in live
    assert live["gov"]["cpu"] == 2.0
    assert live["gov"]["gpu"] == 6.0
    assert live["gov"]["mem"] == 0.7
