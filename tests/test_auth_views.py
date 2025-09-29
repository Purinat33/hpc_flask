# tests/test_auth_views.py
def test_login_view_messages(client):
    # locked message
    r1 = client.get("/login?err=locked&left=75&u=someone")
    assert r1.status_code == 200
    assert b"Too many failed attempts" in r1.data

    # bad creds message
    r2 = client.get("/login?err=bad")
    assert r2.status_code == 200
    assert b"Invalid username or password" in r2.data


def test_logout_redirects(client, admin_user):
    # After logging in via fixture, logout should redirect to /login
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code in (302, 303)
