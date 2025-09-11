# models/billing_store.py (Postgres / SQLAlchemy)
import re
from datetime import datetime, timezone
from typing import Iterable, Tuple, List, Dict

from sqlalchemy import select, func, update, delete
from sqlalchemy.exc import IntegrityError
from models.base import session_scope
from models.schema import Receipt, ReceiptItem


def canonical_job_id(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "." in s:
        prefix = s.split(".", 1)[0]
        if re.fullmatch(r"\d+(?:_\d+)?", prefix):
            return prefix
        return s
    return s


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def billed_job_ids() -> set[str]:
    with session_scope() as s:
        rows = s.execute(select(ReceiptItem.job_key)).all()
        return {r[0] for r in rows}


def list_receipts(username: str | None = None) -> list[dict]:
    with session_scope() as s:
        stmt = select(Receipt).order_by(Receipt.id.desc())
        if username:
            stmt = stmt.where(Receipt.username == username)
        rows = s.execute(stmt).scalars().all()
        out = []
        for r in rows:
            out.append({
                "id": r.id, "username": r.username, "start": r.start, "end": r.end,
                "total": float(r.total), "status": r.status, "created_at": r.created_at,
                "paid_at": r.paid_at, "method": r.method, "tx_ref": r.tx_ref
            })
        return out


def get_receipt_with_items(receipt_id: int) -> tuple[dict, list[dict]]:
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return {}, []
        items = s.execute(
            select(ReceiptItem).where(ReceiptItem.receipt_id ==
                                      receipt_id).order_by(ReceiptItem.job_id_display)
        ).scalars().all()
        return (
            {
                "id": r.id, "username": r.username, "start": r.start, "end": r.end,
                "total": float(r.total), "status": r.status, "created_at": r.created_at,
                "paid_at": r.paid_at, "method": r.method, "tx_ref": r.tx_ref
            },
            [
                {
                    "receipt_id": i.receipt_id, "job_key": i.job_key, "job_id_display": i.job_id_display,
                    "cost": float(i.cost), "cpu_core_hours": float(i.cpu_core_hours),
                    "gpu_hours": float(i.gpu_hours), "mem_gb_hours": float(i.mem_gb_hours)
                } for i in items
            ]
        )


def create_receipt_from_rows(username: str, start: str, end: str, rows: Iterable[dict]) -> Tuple[int, float, list[dict]]:
    now = _now_iso()
    inserted: List[dict] = []
    total = 0.0
    with session_scope() as s:
        r = Receipt(username=username, start=start, end=end,
                    total=0.0, status="pending", created_at=now)
        s.add(r)
        s.flush()  # get r.id

        for row in rows:
            job_key = canonical_job_id(str(row["JobID"]))
            item = ReceiptItem(
                receipt_id=r.id,
                job_key=job_key,
                job_id_display=str(row["JobID"]),
                cost=float(row["Cost (฿)"]),
                cpu_core_hours=float(row["CPU_Core_Hours"]),
                gpu_hours=float(row["GPU_Hours"]),
                mem_gb_hours=float(row["Mem_GB_Hours"]),
            )
            try:
                s.add(item)
                s.flush()
                total += float(row["Cost (฿)"])
                inserted.append({
                    "receipt_id": r.id, "job_key": job_key, "job_id_display": str(row["JobID"]),
                    "cost": float(row["Cost (฿)"]),
                    "cpu_core_hours": float(row["CPU_Core_Hours"]),
                    "gpu_hours": float(row["GPU_Hours"]),
                    "mem_gb_hours": float(row["Mem_GB_Hours"]),
                })
            except IntegrityError:
                s.rollback()  # rollback only the failed item; keep session alive
                # skip duplicates (job_key UNIQUE)
                pass

        r.total = round(total, 2)
        s.add(r)
    return r.id, round(total, 2), inserted


def mark_paid(receipt_id: int, method: str = "admin", tx_ref: str | None = None):
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return
        r.status = "paid"
        r.paid_at = _now_iso()
        r.method = method
        r.tx_ref = tx_ref
        s.add(r)


def void_receipt(receipt_id: int):
    with session_scope() as s:
        # ON DELETE CASCADE will drop children if we delete the parent,
        # but old semantics were: delete items, then mark 'void'.
        s.execute(delete(ReceiptItem).where(
            ReceiptItem.receipt_id == receipt_id))
        r = s.get(Receipt, receipt_id)
        if r:
            r.status = "void"
            s.add(r)


def list_billed_items_for_user(username: str, status: str | None = None) -> list[dict]:
    with session_scope() as s:
        q = select(ReceiptItem, Receipt).join(Receipt, Receipt.id ==
                                              ReceiptItem.receipt_id).where(Receipt.username == username)
        if status in ("pending", "paid"):
            q = q.where(Receipt.status == status)
        q = q.order_by(Receipt.id.desc(), ReceiptItem.job_id_display)
        rows = s.execute(q).all()
        out = []
        for (i, r) in rows:
            out.append({
                "job_id_display": i.job_id_display, "job_key": i.job_key,
                "cost": float(i.cost), "cpu_core_hours": float(i.cpu_core_hours),
                "gpu_hours": float(i.gpu_hours), "mem_gb_hours": float(i.mem_gb_hours),
                "receipt_id": r.id, "status": r.status, "start": r.start, "end": r.end,
                "created_at": r.created_at, "paid_at": r.paid_at
            })
        return out


def admin_list_receipts(status: str | None = None) -> list[dict]:
    with session_scope() as s:
        q = select(Receipt)
        if status:
            q = q.where(Receipt.status == status)
        q = q.order_by(Receipt.created_at.desc(), Receipt.id.desc())
        rows = s.execute(q).scalars().all()
        return [{
            "id": r.id, "username": r.username, "start": r.start, "end": r.end,
            "total": float(r.total), "status": r.status, "created_at": r.created_at, "paid_at": r.paid_at
        } for r in rows]


def mark_receipt_paid(receipt_id: int, admin_user: str) -> bool:
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return False
        if r.status == "paid":
            return True
        if r.status != "pending":
            return False
        r.status = "paid"
        r.paid_at = _now_iso()
        r.method = admin_user or "admin"
        r.tx_ref = None
        s.add(r)
        return True


def paid_receipts_csv():
    import io
    import csv
    rows = admin_list_receipts(status="paid")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "username", "start", "end", "total_THB",
               "status", "created_at", "paid_at"])
    for r in rows:
        w.writerow([r["id"], r["username"], r["start"], r["end"],
                   f"{float(r['total']):.2f}", r["status"], r["created_at"], r["paid_at"] or ""])
    out.seek(0)
    return ("paid_receipts_history.csv", out.read())
