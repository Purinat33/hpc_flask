def test_admin_page_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers.get("Location", "")


def test_user_page_requires_login(client):
    r = client.get("/me", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers.get("Location", "")
