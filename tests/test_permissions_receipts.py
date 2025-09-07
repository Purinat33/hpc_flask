import pandas as pd
from tests.utils import login_admin, login_user
from models.billing_store import create_receipt_from_rows, get_receipt_with_items


def _make_receipt_for_user(username: str):
    # one fake job
    df = pd.DataFrame([{
        "User": username, "JobID": "perm-1",
        "Elapsed": "01:00:00", "TotalCPU": "01:00:00",
        "ReqTRES": "cpu=1,mem=2G", "State": "COMPLETED"
    }])
    from services.billing import compute_costs
    df = compute_costs(df)
    rid, *_ = create_receipt_from_rows(username, "1970-01-01",
                                       "2099-12-31", df.to_dict(orient="records"))
    return rid


def test_owner_can_view_and_start_payment(client):
    rid = _make_receipt_for_user("alice")
    r = login_user(client, "alice", "alice")
    assert r.status_code in (302, 303)

    # view works
    r2 = client.get(f"/me/receipts/{rid}")
    assert r2.status_code == 200
    assert "Pay now" in r2.get_data(as_text=True)

    # start payment allowed (we won’t follow redirects here)
    r3 = client.get(f"/payments/receipt/{rid}/start", follow_redirects=False)
    assert r3.status_code in (302, 303)


def test_admin_can_view_others_but_cannot_start_checkout(client):
    rid = _make_receipt_for_user("alice")
    login_admin(client)

    # admin can view user's receipt (read-only)
    r1 = client.get(f"/me/receipts/{rid}")
    assert r1.status_code == 200
    assert "Pay now" not in r1.get_data(as_text=True)

    # admin cannot start checkout for another user
    r2 = client.get(f"/payments/receipt/{rid}/start", follow_redirects=False)
    assert r2.status_code == 403


def test_other_user_cannot_view_your_receipt(client):
    rid = _make_receipt_for_user("alice")
    login_user(client, "bob", "bob")
    r = client.get(f"/me/receipts/{rid}", follow_redirects=False)
    # redirected to /me/receipts by the view guard
    assert r.status_code in (302, 303)
    assert "/me/receipts" in r.headers.get("Location", "")
