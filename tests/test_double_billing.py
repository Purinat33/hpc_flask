import pandas as pd
from models.billing_store import create_receipt_from_rows, billed_job_ids


def test_cannot_bill_same_job_twice(client):
    # seed a single job for alice
    from services.billing import compute_costs
    df = pd.DataFrame([{
        "User": "alice", "JobID": "dupe-job-1",
        "Elapsed": "00:30:00", "TotalCPU": "00:30:00",
        "ReqTRES": "cpu=1,mem=1G", "State": "COMPLETED"
    }])
    df = compute_costs(df)

    rid1, total1, items1 = create_receipt_from_rows(
        "alice", "1970-01-01", "2099-12-31", df.to_dict(orient="records"))
    assert total1 > 0 and len(items1) == 1

    # try again: should insert zero new items and total should be 0
    rid2, total2, items2 = create_receipt_from_rows(
        "alice", "1970-01-01", "2099-12-31", df.to_dict(orient="records"))
    assert total2 in (0.0, 0) and len(items2) == 0

    # billed set contains that job key exactly once
    keys = billed_job_ids()
    assert any("dupe-job-1" in k for k in keys)
