# tests/test_user_filters_csv.py
import pytest
from models.users_db import create_user


def _login(client, u, p):
    client.post("/login", data={"username": u,
                "password": p}, follow_redirects=True)


@pytest.mark.db
def test_me_with_date_filters_and_csv(client):
    # login as a normal user (admins get redirected away from /me)
    try:
        create_user("user1", "pw", role="user")
    except Exception:
        pass
    _login(client, "user1", "pw")

    # /me with explicit window
    r = client.get("/me?start=2025-01-01&end=2025-01-31")
    assert r.status_code in (200, 304)

    # /me.csv with the same window
    csv = client.get("/me.csv?start=2025-01-01&end=2025-01-31")
    assert csv.status_code == 200
    assert "csv" in csv.headers.get("Content-Type", "").lower()
    assert csv.data  # some bytes returned


@pytest.mark.db
def test_me_redirects_for_admin(client, admin_user):
    # when logged in as admin, /me should redirect to admin UI
    r = client.get("/me", follow_redirects=False)
    assert r.status_code in (302, 303)
    loc = r.headers.get("Location", "")
    assert "/admin" in loc or "admin" in loc.lower()
