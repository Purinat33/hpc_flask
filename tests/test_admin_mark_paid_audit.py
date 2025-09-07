import pandas as pd
from tests.utils import login_admin
from models.billing_store import create_receipt_from_rows
from models.db import get_db


def test_admin_mark_paid_audited_as_admin_action(client):
    # Make pending receipt for bob
    from services.billing import compute_costs
    df = pd.DataFrame([{
        "User": "bob", "JobID": "adm-1",
        "Elapsed": "00:10:00", "TotalCPU": "00:10:00",
        "ReqTRES": "cpu=1,mem=1G", "State": "COMPLETED"
    }])
    df = compute_costs(df)
    rid, *_ = create_receipt_from_rows("bob", "1970-01-01",
                                       "2099-12-31", df.to_dict(orient="records"))

    login_admin(client)

    # Mark paid via admin
    r = client.post(
        f"/admin/receipts/{rid}/paid", data={"csrf_token": "x"}, follow_redirects=False)
    assert r.status_code in (302, 303)

    db = get_db()
    rec = db.execute(
        "SELECT status, method, tx_ref FROM receipts WHERE id=?", (rid,)).fetchone()
    assert rec["status"] == "paid"
    # billing_store.mark_receipt_paid writes method=<admin_user>, tx_ref=NULL
    assert rec["method"] == "admin"
    # If your implementation stores the actual actor username, assert that instead.

    # audit row exists with action 'receipt.paid.admin'
    row = db.execute("SELECT COUNT(*) AS c FROM audit_log WHERE action='receipt.paid.admin' AND target=?",
                     (f"receipt={rid}",)).fetchone()
    assert row["c"] >= 1
