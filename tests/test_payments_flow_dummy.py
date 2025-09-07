import pandas as pd
from tests.utils import login_user
from models.billing_store import create_receipt_from_rows, get_receipt_with_items
from models.payments_store import get_payment
from models.db import get_db


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

    # Verify DB: receipt is paid, payment is succeeded, method/tx_ref set
    db = get_db()
    rec = db.execute(
        "SELECT status, method, tx_ref FROM receipts WHERE id=?", (rid,)).fetchone()
    assert rec["status"] == "paid"
    assert rec["method"].startswith("auto:dummy")
    assert rec["tx_ref"]  # should be 'dummy_<pid>'

    # Payment row matches
    pay = db.execute(
        "SELECT status, external_payment_id FROM payments WHERE receipt_id=?", (rid,)).fetchone()
    assert pay["status"] == "succeeded"
    assert pay["external_payment_id"] == rec["tx_ref"]


def test_finalize_rejects_non_pending(app):
    from models.payments_store import finalize_success_if_amount_matches, get_db
    db = get_db()
    with db:
        db.executescript("""
          INSERT INTO receipts(username,start,end,total,status,created_at)
          VALUES('alice','1970-01-01','1970-01-02',1.00,'void','2025-01-01T00:00:00Z');
          INSERT INTO payments(provider,receipt_id,username,status,currency,amount_cents,created_at,updated_at,external_payment_id)
          VALUES('dummy', last_insert_rowid(),'alice','failed','THB',100,'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z','x1');
        """)
    assert finalize_success_if_amount_matches(
        "x1", 100, "THB", "dummy") is False
