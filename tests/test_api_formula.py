# tests/test_api_formula.py
import pytest


# def test_formula_get_and_etag(client):
#     r1 = client.get("/formula?type=gov")
#     assert r1.status_code == 200
#     etag = r1.headers.get("ETag")
#     assert etag

#     # If-None-Match → 304 path
#     r2 = client.get("/formula?type=private", headers={"If-None-Match": etag})
#     assert r2.status_code == 304


@pytest.mark.db
def test_formula_update_as_admin(client, admin_user):
    payload = {"type": "mu", "cpu": 1.1, "gpu": 2.2, "mem": 3.3}
    r = client.post("/formula", json=payload)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["updated"]["mu"] == {"cpu": 1.1, "gpu": 2.2, "mem": 3.3}

    # Confirm via GET
    r2 = client.get("/formula?type=mu")
    assert r2.status_code == 200
    assert r2.get_json()["rates"] == {"cpu": 1.1, "gpu": 2.2, "mem": 3.3}
