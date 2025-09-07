def test_lockout_after_repeated_failures(client, app, monkeypatch):
    # Ensure small numbers for a fast test
    app.config.update(AUTH_THROTTLE_MAX_FAILS=2,
                      AUTH_THROTTLE_WINDOW_SEC=60, AUTH_THROTTLE_LOCK_SEC=60)

    # 2 failures
    for _ in range(2):
        r = client.post(
            "/login", data={"username": "lockuser", "password": "wrong"})
        assert r.status_code in (302, 303)

    # Now locked
    r3 = client.post(
        "/login", data={"username": "lockuser", "password": "wrong"})
    assert r3.status_code in (302, 303)
    # redirected with err=locked in query string
    assert "err=locked" in r3.headers.get("Location", "")
