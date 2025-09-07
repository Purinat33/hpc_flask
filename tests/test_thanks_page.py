import re
import pandas as pd
from tests.utils import login_user
from models.billing_store import create_receipt_from_rows
from models.db import get_db


def test_thanks_requires_login(client):
    r = client.get("/payments/thanks")
    assert r.status_code in (
        302, 303) and "/login" in r.headers.get("Location", "")


def test_thanks_shows_status(client):
    from services.billing import compute_costs
    df = pd.DataFrame([{
        "User": "alice", "JobID": "tks-1",
        "Elapsed": "00:05:00", "TotalCPU": "00:05:00",
        "ReqTRES": "cpu=1,mem=1G", "State": "COMPLETED"
    }])
    df = compute_costs(df)
    rid, *_ = create_receipt_from_rows("alice", "1970-01-01",
                                       "2099-12-31", df.to_dict(orient="records"))

    login_user(client, "alice", "alice")
    r1 = client.get(f"/payments/thanks?rid={rid}")
    assert r1.status_code == 200
    html = r1.get_data(as_text=True).lower()
    assert re.search(r"\bprocess(?:ing|ed)\b", html)

    # flip to paid to check the other branch
    db = get_db()
    with db:
        db.execute("UPDATE receipts SET status='paid' WHERE id=?", (rid,))
    r2 = client.get(f"/payments/thanks?rid={rid}")
    assert r2.status_code == 200
    assert "payment confirmed" in r2.get_data(as_text=True).lower()
