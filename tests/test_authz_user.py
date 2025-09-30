# tests/test_authz_user.py
import pytest
from models.users_db import create_user
from models.billing_store import create_receipt_from_rows


def _mk_receipt(username):
    rid, total, _ = create_receipt_from_rows(
        username, "2025-01-01", "2025-01-31",
        [{"JobID": "u1", "Cost (฿)": 42, "CPU_Core_Hours": 1.0, "GPU_Hours": 0.0,
                                "Mem_GB_Hours_Used": 0.0, "tier": "mu", "User": username}]
    )
    assert total == 42
    return rid


def _login(client, u, p):
    client.post("/login", data={"username": u,
                "password": p}, follow_redirects=True)


@pytest.mark.db
def test_user_cannot_access_admin_exports(client):
    # normal user
    try:
        create_user("user1", "pw", role="user")
    except Exception:
        pass
    _login(client, "user1", "pw")

    # admin export endpoints must be blocked for non-admin
    r1 = client.get("/admin/export/ledger.csv?start=2025-01-01&end=2025-01-31")
    assert r1.status_code in (302, 403)  # redirect to login or forbidden

    r2 = client.get(
        "/admin/export/ledger_th.pdf?start=2025-01-01&end=2025-01-31&mode=derived")
    assert r2.status_code in (302, 403)


@pytest.mark.db
def test_user_cannot_mark_paid_or_revert(client):
    # create a receipt as user1
    try:
        create_user("user1", "pw", role="user")
    except Exception:
        pass
    rid = _mk_receipt("user1")

    # login as user1 and try admin-only actions
    _login(client, "user1", "pw")
    resp1 = client.post(
        f"/admin/receipts/{rid}/paid", data={}, follow_redirects=False)
    resp2 = client.post(
        f"/admin/receipts/{rid}/revert", data={"reason": "nope"}, follow_redirects=False)
    assert resp1.status_code in (302, 403)
    assert resp2.status_code in (302, 403)


@pytest.mark.db
def test_user_cannot_view_other_users_receipt(client):
    # two users; receipt belongs to user1
    for u in ("user1", "user2"):
        try:
            create_user(u, "pw", role="user")
        except Exception:
            pass
    rid = _mk_receipt("user1")

    # user2 should not see user1's receipt in /me namespace
    _login(client, "user2", "pw")

    # HTML page
    page = client.get(f"/me/receipts/{rid}", follow_redirects=False)
    assert page.status_code in (302, 303, 403, 404)
    if page.status_code in (302, 303):
        loc = page.headers.get("Location", "")
        # your app typically redirects to /me or /login for unauthorized
        assert "/me" in loc or "/login" in loc

    # PDF
    pdf = client.get(f"/me/receipts/{rid}.pdf", follow_redirects=False)
    assert pdf.status_code in (302, 303, 403, 404)
    if pdf.status_code in (302, 303):
        loc = pdf.headers.get("Location", "")
        assert "/me" in loc or "/login" in loc


@pytest.mark.db
def test_user_cannot_update_formula(client):
    # normal user logged in
    try:
        create_user("user1", "pw", role="user")
    except Exception:
        pass
    _login(client, "user1", "pw")

    # GET is fine for anyone
    g = client.get("/formula?type=mu")
    assert g.status_code in (200, 304)

    # POST must be admin-only
    p = client.post(
        "/formula", json={"type": "mu", "cpu": 9.9, "gpu": 9.9, "mem": 9.9})
    assert p.status_code in (302, 401, 403)
