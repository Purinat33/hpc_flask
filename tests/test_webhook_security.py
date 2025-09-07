import json
import hmac
import hashlib
import pandas as pd
from models.db import get_db
from models.payments_store import create_payment_for_receipt
from services.payments.registry import get_provider
from tests.utils import login_user


def _sign(app, body: bytes) -> dict:
    secret = (app.config.get("PAYMENT_WEBHOOK_SECRET")
              or "dev_webhook_secret").encode("utf-8")
    mac = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return {"X-Dummy-Signature": mac, "Content-Type": "application/json"}


def _make_receipt(client, username):
    from services.billing import compute_costs
    from models.billing_store import create_receipt_from_rows
    df = pd.DataFrame([{
        "User": username, "JobID": "whk-1",
        "Elapsed": "00:20:00", "TotalCPU": "00:20:00",
        "ReqTRES": "cpu=1,mem=1G", "State": "COMPLETED"
    }])
    df = compute_costs(df)
    rid, *_ = create_receipt_from_rows(username, "1970-01-01",
                                       "2099-12-31", df.to_dict(orient="records"))
    return rid


def test_wrong_amount_or_currency_does_not_mark_paid(client, app):
    login_user(client, "alice", "alice")
    rid = _make_receipt(client, "alice")

    # Create local intent to get amount/currency and a payment_id/external id
    provider = get_provider()
    from models.payments_store import load_receipt, get_payment
    rec = load_receipt(rid)
    pid, amt_cents = create_payment_for_receipt(
        provider.name, rid, "alice", "THB")

    # Attach an external id to match finalize logic
    from models.payments_store import attach_provider_checkout, get_payment
    external_id = f"dummy_{pid}"
    attach_provider_checkout(pid, external_id, None, f"idem_{pid}")

    # 1) Wrong amount
    body = json.dumps({
        "event_type": "payment.succeeded",
        "event_id": "evt_wrong_amt",
        "external_payment_id": external_id,
        "amount_cents": amt_cents + 1,
        "currency": "THB",
    }).encode("utf-8")
    r = client.post("/payments/webhook", data=body, headers=_sign(app, body))
    assert r.status_code == 200

    db = get_db()
    recrow = db.execute(
        "SELECT status FROM receipts WHERE id=?", (rid,)).fetchone()
    assert recrow["status"] == "pending"

    # 2) Wrong currency
    body2 = json.dumps({
        "event_type": "payment.succeeded",
        "event_id": "evt_wrong_cur",
        "external_payment_id": external_id,
        "amount_cents": amt_cents,
        "currency": "USD",
    }).encode("utf-8")
    r2 = client.post("/payments/webhook", data=body2,
                     headers=_sign(app, body2))
    assert r2.status_code == 200
    recrow2 = db.execute(
        "SELECT status FROM receipts WHERE id=?", (rid,)).fetchone()
    assert recrow2["status"] == "pending"


def test_webhook_idempotency_on_event_id(client, app):
    login_user(client, "alice", "alice")
    rid = _make_receipt(client, "alice")
    provider = get_provider()
    pid, amt_cents = create_payment_for_receipt(
        provider.name, rid, "alice", "THB")
    from models.payments_store import attach_provider_checkout
    external_id = f"dummy_{pid}"
    attach_provider_checkout(pid, external_id, None, f"idem_{pid}")

    # Send the same event twice (same event_id)
    payload = {
        "event_type": "payment.succeeded",
        "event_id": "evt_dup_1",
        "external_payment_id": external_id,
        "amount_cents": amt_cents,
        "currency": "THB",
    }
    body = json.dumps(payload).encode("utf-8")
    for _ in range(2):
        r = client.post("/payments/webhook", data=body,
                        headers=_sign(app, body))
        assert r.status_code == 200

    db = get_db()
    # Only one row recorded for this provider + event_id
    c = db.execute("SELECT COUNT(*) AS c FROM payment_events WHERE provider=? AND external_event_id=?",
                   (provider.name, "evt_dup_1")).fetchone()["c"]
    assert c == 1

    # Receipt is paid, and replays don't harm
    rec = db.execute("SELECT status FROM receipts WHERE id=?",
                     (rid,)).fetchone()
    assert rec["status"] == "paid"
