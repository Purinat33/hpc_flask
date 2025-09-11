import pandas as pd
from datetime import datetime, timezone

from tests.utils import login_admin
from services.billing import compute_costs

from models.base import init_engine_and_session
from models.schema import Receipt, ReceiptItem, AuditLog


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_admin_mark_paid_audited_as_admin_action(client):
    # 1) Create a pending receipt for bob (via ORM, not SQLite helpers)
    df = pd.DataFrame(
        [
            {
                "User": "bob",
                "JobID": "adm-1",
                "Elapsed": "00:10:00",
                "TotalCPU": "00:10:00",
                "ReqTRES": "cpu=1,mem=1G",
                "State": "COMPLETED",
            }
        ]
    )
    df = compute_costs(df)
    total = float(df["Cost (฿)"].sum())

    engine, SessionLocal = init_engine_and_session()
    with SessionLocal() as s:
        rec = Receipt(
            username="bob",
            start="1970-01-01",
            end="2099-12-31",
            total=round(total, 2),
            status="pending",
            created_at=_now_iso(),
            paid_at=None,
            method=None,
            tx_ref=None,
        )
        s.add(rec)
        s.flush()  # get rec.id
        rid = rec.id

        for row in df.to_dict(orient="records"):
            s.add(
                ReceiptItem(
                    receipt_id=rid,
                    # keep simple (your schema enforces uniqueness)
                    job_key=str(row["JobID"]),
                    job_id_display=str(row["JobID"]),
                    cost=float(row["Cost (฿)"]),
                    cpu_core_hours=float(row["CPU_Core_Hours"]),
                    gpu_hours=float(row["GPU_Hours"]),
                    mem_gb_hours=float(row["Mem_GB_Hours"]),
                )
            )
        s.commit()

    # 2) Mark it paid via the admin endpoint
    login_admin(client)
    r = client.post(
        f"/admin/receipts/{rid}/paid", data={"csrf_token": "x"}, follow_redirects=False)
    assert r.status_code in (302, 303)

    # 3) Assert receipt updated & audit logged
    with SessionLocal() as s:
        updated = s.get(Receipt, rid)
        assert updated is not None
        assert updated.status == "paid"

        # Your admin code writes method=<admin_user> (often "admin")
        assert updated.method in ("admin",) or (
            updated.method or "").startswith("admin")

        audit_count = (
            s.query(AuditLog)
            .filter(AuditLog.action == "receipt.paid.admin", AuditLog.target == f"receipt={rid}")
            .count()
        )
        assert audit_count >= 1
