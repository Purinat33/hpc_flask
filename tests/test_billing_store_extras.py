# tests/test_billing_store_extras.py
from __future__ import annotations
from models.billing_store import (
    canonical_job_id, billed_job_ids, create_receipt_from_rows,
    list_billed_items_for_user, admin_list_receipts,
    mark_receipt_paid, paid_receipts_csv,
)


def test_canonical_job_id_variants():
    assert canonical_job_id("12345.batch") == "12345"
    assert canonical_job_id("12345_7.extern") == "12345_7"
    assert canonical_job_id("NODOT") == "NODOT"
    assert canonical_job_id("") == ""
    assert canonical_job_id("CSV.A") == "CSV.A"


# tests/test_billing_store_extras.py

def test_create_receipt_skips_duplicate_jobs_and_updates_total(app):
    rows = [
        {"JobID": "DUP.1", "Cost (฿)": 1.5, "CPU_Core_Hours": 1,
         "GPU_Hours": 0, "Mem_GB_Hours": 1},
        {"JobID": "DUP.1", "Cost (฿)": 1.5, "CPU_Core_Hours": 1,
         "GPU_Hours": 0, "Mem_GB_Hours": 1},
    ]
    with app.app_context():
        rid, total, inserted = create_receipt_from_rows(
            "alice", "1970-01-01", "1970-01-31", rows)
        assert total == 1.5 and len(inserted) == 1
        assert canonical_job_id("DUP.1") in billed_job_ids()
        rid2, total2, inserted2 = create_receipt_from_rows(
            "alice", "1970-01-01", "1970-01-31", rows[:1])
        assert total2 == 0.0 and inserted2 == []


def test_admin_list_and_billed_items_and_paid_csv_flow(app):
    with app.app_context():
        rows1 = [{"JobID": "CSV.A",
                  "Cost (฿)": 2.0, "CPU_Core_Hours": 1, "GPU_Hours": 0, "Mem_GB_Hours": 1}]
        rows2 = [{"JobID": "CSV.B",
                  "Cost (฿)": 3.0, "CPU_Core_Hours": 2, "GPU_Hours": 0, "Mem_GB_Hours": 1}]
        rid1, * \
            _ = create_receipt_from_rows(
                "alice", "1970-01-01", "1970-01-31", rows1)
        rid2, * \
            _ = create_receipt_from_rows(
                "alice", "1970-02-01", "1970-02-28", rows2)

        pend = admin_list_receipts(status="pending")
        ids = {r["id"] for r in pend}
        assert {rid1, rid2}.issubset(ids)

        items_p = list_billed_items_for_user("alice", "pending")
        assert all(i["status"] == "pending" for i in items_p)
        assert {i["receipt_id"] for i in items_p} == {rid1, rid2}

        assert mark_receipt_paid(rid1, "admin") is True
        assert mark_receipt_paid(rid1, "admin") is True  # idempotent

        items_paid = list_billed_items_for_user("alice", "paid")
        assert all(i["status"] == "paid" for i in items_paid)
        assert {i["receipt_id"] for i in items_paid} == {rid1}

        fname, csv_text = paid_receipts_csv()
        assert fname.endswith(".csv")
        assert "id,username,start,end,total_THB,status,created_at,paid_at" in csv_text.splitlines()[
            0]
        assert f"{rid1},alice," in csv_text
