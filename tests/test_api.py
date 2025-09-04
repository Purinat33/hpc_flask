def test_get_formula_default_mu(client):
    r = client.get("/formula?type=mu")
    assert r.status_code == 200
    js = r.get_json()
    assert js["type"] == "mu"
    assert js["unit"] == "per-hour"
    assert {"cpu", "gpu", "mem"} <= js["rates"].keys()


def test_update_formula_requires_admin(client):
    r = client.post(
        "/formula", json={"type": "mu", "cpu": 1, "gpu": 2, "mem": 3})
    # Not logged in + admin_required -> redirect to /login (302) or 401 depending on your decorator
    assert r.status_code in (302, 401)
