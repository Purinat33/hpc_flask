import pandas as pd
from tests.utils import login_user
from models.billing_store import create_receipt_from_rows, get_receipt_with_items
from models.payments_store import get_payment
from models.base import init_engine_and_session
from models.schema import Receipt, Payment
from models.payments_store import finalize_success_if_amount_matches


def test_dummy_payment_happy_path_marks_paid(client, app):
    # Make a receipt for alice
    from services.billing import compute_costs
    df = pd.DataFrame([{
        "User": "alice", "JobID": "pay-1",
        "Elapsed": "01:00:00", "TotalCPU": "01:00:00",
        "ReqTRES": "cpu=1,mem=1G", "State": "COMPLETED"
    }])
    df = compute_costs(df)
    rid, *_ = create_receipt_from_rows("alice", "1970-01-01",
                                       "2099-12-31", df.to_dict(orient="records"))

    login_user(client, "alice", "alice")

    # Follow redirects all the way (start -> simulate -> webhook -> thanks)
    r = client.get(f"/payments/receipt/{rid}/start", follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Payment" in html  # thanks page

    # Verify via ORM: receipt paid, payment succeeded, tx_ref matches
    _, SessionLocal = init_engine_and_session()
    with SessionLocal() as s:
        rec = s.get(Receipt, rid)
        assert rec is not None
        assert rec.status == "paid"
        assert rec.method and rec.method.startswith("auto:dummy")
        assert rec.tx_ref  # should look like 'dummy_<pid>'

    # Payment row matches
    pay = (
        s.query(Payment)
        .filter(Payment.receipt_id == rid)
        .order_by(Payment.id.desc())
        .first()
    )
    assert pay is not None
    assert pay.status == "succeeded"
    assert pay.external_payment_id == rec.tx_ref


def test_finalize_rejects_non_pending(app):
    # Create a VOID receipt + a (failed) payment pointing at it, then try to finalize.
    _, SessionLocal = init_engine_and_session()
    with SessionLocal() as s:
        rec = Receipt(
            username="alice",
            start="1970-01-01",
            end="1970-01-02",
            total=1.00,
            status="void",
            created_at="2025-01-01T00:00:00Z",
        )
        s.add(rec)
        s.flush()  # assigns rec.id

        pay = Payment(
            provider="dummy",
            receipt_id=rec.id,
            username="alice",
            status="failed",
            currency="THB",
            amount_cents=100,
            external_payment_id="x1",
            created_at="2025-01-01T00:00:00Z",
            updated_at="2025-01-01T00:00:00Z",
        )

        s.add(pay)
        s.commit()
        assert finalize_success_if_amount_matches(
            "x1", 100, "THB", "dummy") is False
