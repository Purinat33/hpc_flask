from tests.utils import login_admin


def test_admin_can_update_formula(client):
    # login as admin
    r = login_admin(client)
    assert r.status_code in (302, 303)  # redirect to /playground

    # update rates
    r2 = client.post(
        "/formula", json={"type": "mu", "cpu": 1.23, "gpu": 4.56, "mem": 7.89})
    assert r2.status_code == 200
    js = r2.get_json()
    assert js["ok"] is True
    assert js["updated"]["mu"] == {"cpu": 1.23, "gpu": 4.56, "mem": 7.89}

    # read back
    r3 = client.get("/formula?type=mu")
    assert r3.status_code == 200
    got = r3.get_json()
    assert got["rates"] == {"cpu": 1.23, "gpu": 4.56, "mem": 7.89}
