from tests.utils import login_admin


from models.base import init_engine_and_session
from models.schema import Rate


def test_admin_can_update_formula(client):
    r = login_admin(client)
    assert r.status_code in (302, 303)

    r2 = client.post(
        "/formula", json={"type": "mu", "cpu": 1.23, "gpu": 4.56, "mem": 7.89})
    assert r2.status_code == 200
    js = r2.get_json()
    assert js["ok"] is True
    assert js["updated"]["mu"] == {"cpu": 1.23, "gpu": 4.56, "mem": 7.89}

    # Optional: verify directly in Postgres via SQLAlchemy
    _, SessionLocal = init_engine_and_session()
    with SessionLocal() as s:
        mu = s.get(Rate, "mu")
        assert mu and (mu.cpu, mu.gpu, mu.mem) == (1.23, 4.56, 7.89)

    r3 = client.get("/formula?type=mu")
    assert r3.status_code == 200
    got = r3.get_json()
    assert got["rates"] == {"cpu": 1.23, "gpu": 4.56, "mem": 7.89}
