import pandas as pd
from flask import url_for
from tests.utils import login_admin
from models.billing_store import create_receipt_from_rows, billed_job_ids
from services.billing import compute_costs


def test_create_receipt_and_billed_ids(client, app):
    login_admin(client)

    # Fake jobs for admin user
    df = pd.DataFrame([{
        "User": "admin", "JobID": "999",
        "Elapsed": "01:00:00", "TotalCPU": "01:00:00",
        "ReqTRES": "cpu=1,mem=1G", "State": "COMPLETED"
    }])
    df = compute_costs(df)

    rid, total, items = create_receipt_from_rows(
        "admin", "1970-01-01", "2099-12-31", df.to_dict(orient="records"))
    assert isinstance(rid, int) and rid > 0
    assert total > 0
    assert any(i["job_key"] for i in items)

    # job key should now be recognized as billed
    keys = billed_job_ids()
    assert df["JobID"].astype(str).map(str).iat[0] in "".join(
        keys)  # canonicalization depends on your impl


def test_admin_usage_hides_billed_jobs(client, app, monkeypatch):
    # Prepare data source to only show one job owned by admin
    import services.data_sources as ds
    import pandas as pd
    fake_df = pd.DataFrame([{
        "User": "admin", "JobID": "HIDE-ME",
        "Elapsed": "00:10:00", "TotalCPU": "00:10:00",
        "ReqTRES": "cpu=1,mem=1G", "End": "2025-01-01T00:00:00", "State": "COMPLETED"
    }])

    monkeypatch.setattr(ds, "fetch_from_slurmrestd", lambda *a,
                        **k: (_ for _ in ()).throw(RuntimeError("off")))
    monkeypatch.setattr(ds, "fetch_from_sacct", lambda *a,
                        **k: (_ for _ in ()).throw(RuntimeError("off")))
    monkeypatch.setattr(ds, "fetch_via_fallback", lambda: fake_df)
    monkeypatch.setattr(ds, "fetch_jobs_with_fallbacks",
                        lambda s, e, username=None: (fake_df.copy(), "test.csv", []))

    # Create a receipt for that job so it becomes "billed"
    from models.billing_store import canonical_job_id, create_receipt_from_rows
    from services.billing import compute_costs
    df = compute_costs(fake_df)
    df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
    rid, _, _ = create_receipt_from_rows(
        "admin", "1970-01-01", "2099-12-31", df.to_dict(orient="records"))

    # Now, requesting Admin "usage" should not show that job in detail view
    login_admin(client)
    r = client.get("/admin?section=usage&view=detail&before=2099-12-31")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "HIDE-ME" not in html  # job is filtered because it's billed
