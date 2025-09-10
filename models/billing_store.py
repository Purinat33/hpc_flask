# models/billing_store.py
import io
import csv
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Tuple, List, Dict

# --- PG/SQLite switch ---
USE_PG = bool(os.getenv("DATABASE_URL"))

if USE_PG:
    from sqlalchemy import select, and_, desc, func
    from sqlalchemy.exc import IntegrityError
    from models.base import init_engine_and_session
    from models.schema import Receipt, ReceiptItem
    Engine, SessionLocal = init_engine_and_session()
else:
    # legacy sqlite helpers
    from models.db import get_db


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_job_id(s: str) -> str:
    """
    Normalize slurm job IDs for duplicate detection.
    - For numeric IDs (e.g. '12345.batch', '12345_7.extern'), drop the suffix after the first dot.
    - For non-numeric prefixes (e.g. 'CSV.A'), keep the full string to avoid collisions.
    """
    s = (s or "").strip()
    if not s:
        return ""
    if "." in s:
        prefix = s.split(".", 1)[0]
        if re.fullmatch(r"\d+(?:_\d+)?", prefix):
            return prefix
        return s
    return s


# --------------------
# Read helpers
# --------------------
def billed_job_ids() -> set[str]:
    if USE_PG:
        with SessionLocal() as s:
            keys = s.scalars(select(ReceiptItem.job_key)).all()
            return set(keys)
    db = get_db()
    cur = db.execute("SELECT job_key FROM receipt_items")
    return {r[0] for r in cur.fetchall()}


def list_receipts(username: str | None = None) -> List[dict]:
    if USE_PG:
        with SessionLocal() as s:
            stmt = select(Receipt)
            if username:
                stmt = stmt.where(Receipt.username == username)
            stmt = stmt.order_by(desc(Receipt.id))
            rows = s.scalars(stmt).all()
            return [dict(
                id=r.id, username=r.username, start=r.start, end=r.end, total=r.total,
                status=r.status, created_at=r.created_at, paid_at=r.paid_at,
                method=r.method, tx_ref=r.tx_ref
            ) for r in rows]
    db = get_db()
    if username:
        cur = db.execute(
            "SELECT * FROM receipts WHERE username=? ORDER BY id DESC", (username,))
    else:
        cur = db.execute("SELECT * FROM receipts ORDER BY id DESC")
    return [dict(r) for r in cur.fetchall()]


def get_receipt_with_items(receipt_id: int) -> tuple[dict, list[dict]]:
    if USE_PG:
        with SessionLocal() as s:
            r = s.get(Receipt, receipt_id)
            if not r:
                return {}, []
            items = s.scalars(
                select(ReceiptItem)
                .where(ReceiptItem.receipt_id == receipt_id)
                .order_by(ReceiptItem.job_id_display)
            ).all()
            rdict = dict(
                id=r.id, username=r.username, start=r.start, end=r.end, total=r.total,
                status=r.status, created_at=r.created_at, paid_at=r.paid_at,
                method=r.method, tx_ref=r.tx_ref
            )
            idicts = [dict(
                receipt_id=i.receipt_id, job_key=i.job_key, job_id_display=i.job_id_display,
                cost=i.cost, cpu_core_hours=i.cpu_core_hours, gpu_hours=i.gpu_hours, mem_gb_hours=i.mem_gb_hours
            ) for i in items]
            return rdict, idicts
    db = get_db()
    r = db.execute("SELECT * FROM receipts WHERE id=?",
                   (receipt_id,)).fetchone()
    if not r:
        return {}, []
    items = db.execute(
        "SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY job_id_display",
        (receipt_id,)
    ).fetchall()
    return dict(r), [dict(i) for i in items]


# --------------------
# Write paths
# --------------------
def create_receipt_from_rows(username: str, start: str, end: str, rows: Iterable[dict]) -> Tuple[int, float, List[dict]]:
    """
    rows must contain: JobID, Cost (฿), CPU_Core_Hours, GPU_Hours, Mem_GB_Hours
    Returns: (receipt_id, total, inserted_items)
    Each inserted item is a dict with at least: job_key, job_id_display, cost, cpu_core_hours, gpu_hours, mem_gb_hours.
    """
    now = _now_utc_iso()

    if USE_PG:
        rows = list(rows)
        # precompute + dedupe by job_key and skip already-billed keys
        want_keys: List[str] = [canonical_job_id(
            str(r["JobID"])) for r in rows]
        with SessionLocal() as s:
            existing = set(s.scalars(select(ReceiptItem.job_key).where(
                ReceiptItem.job_key.in_(want_keys))).all())
            r = Receipt(
                username=username, start=start, end=end, total=0.0,
                status="pending", created_at=now, paid_at=None, method=None, tx_ref=None
            )
            s.add(r)
            s.flush()  # get r.id

            inserted: List[dict] = []
            total = 0.0
            for row in rows:
                job_key = canonical_job_id(str(row["JobID"]))
                if job_key in existing:
                    continue
                item = ReceiptItem(
                    receipt_id=r.id,
                    job_key=job_key,
                    job_id_display=str(row["JobID"]),
                    cost=float(row["Cost (฿)"]),
                    cpu_core_hours=float(row["CPU_Core_Hours"]),
                    gpu_hours=float(row["GPU_Hours"]),
                    mem_gb_hours=float(row["Mem_GB_Hours"]),
                )
                s.add(item)
                inserted.append(dict(
                    receipt_id=r.id, job_key=job_key, job_id_display=str(
                        row["JobID"]),
                    cost=float(row["Cost (฿)"]),
                    cpu_core_hours=float(row["CPU_Core_Hours"]),
                    gpu_hours=float(row["GPU_Hours"]),
                    mem_gb_hours=float(row["Mem_GB_Hours"]),
                ))
                total += float(row["Cost (฿)"])

            r.total = round(total, 2)
            s.commit()
            return r.id, r.total, inserted

    # --- SQLite legacy path ---
    db = get_db()
    now = _now_utc_iso()
    inserted: list[dict] = []
    total = 0.0
    with db:  # transaction
        rid = db.execute(
            "INSERT INTO receipts(username,start,end,total,status,created_at) VALUES(?,?,?,?,?,?)",
            (username, start, end, 0.0, "pending", now)
        ).lastrowid

        for r in rows:
            job_key = canonical_job_id(str(r["JobID"]))
            try:
                db.execute(
                    """INSERT INTO receipt_items
                       (receipt_id, job_key, job_id_display, cost, cpu_core_hours, gpu_hours, mem_gb_hours)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (rid, job_key, str(r["JobID"]),
                     float(r["Cost (฿)"]),
                     float(r["CPU_Core_Hours"]),
                     float(r["GPU_Hours"]),
                     float(r["Mem_GB_Hours"]))
                )
                total += float(r["Cost (฿)"])
                inserted.append({
                    "receipt_id": rid,
                    "job_key": job_key,
                    "job_id_display": str(r["JobID"]),
                    "cost": float(r["Cost (฿)"]),
                    "cpu_core_hours": float(r["CPU_Core_Hours"]),
                    "gpu_hours": float(r["GPU_Hours"]),
                    "mem_gb_hours": float(r["Mem_GB_Hours"]),
                })
            except sqlite3.IntegrityError:
                pass

        db.execute("UPDATE receipts SET total=? WHERE id=?",
                   (round(total, 2), rid))
    return rid, round(total, 2), inserted


def mark_paid(receipt_id: int, method: str = "admin", tx_ref: str | None = None):
    now = _now_utc_iso()
    if USE_PG:
        with SessionLocal() as s:
            r = s.get(Receipt, receipt_id)
            if not r:
                return
            r.status = "paid"
            r.paid_at = now
            r.method = method
            r.tx_ref = tx_ref
            s.commit()
        return
    db = get_db()
    with db:
        db.execute(
            "UPDATE receipts SET status='paid', paid_at=?, method=?, tx_ref=? WHERE id=?",
            (now, method, tx_ref, receipt_id)
        )


def void_receipt(receipt_id: int):
    """Optional: frees jobs for rebilling by deleting items."""
    if USE_PG:
        with SessionLocal() as s:
            # delete items (CASCADE also handles this, but be explicit)
            s.query(ReceiptItem).filter(
                ReceiptItem.receipt_id == receipt_id).delete()
            r = s.get(Receipt, receipt_id)
            if r:
                r.status = "void"
            s.commit()
        return
    db = get_db()
    with db:
        db.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
        db.execute("UPDATE receipts SET status='void' WHERE id=?", (receipt_id,))


def list_billed_items_for_user(username: str, status: str | None = None) -> List[dict]:
    """
    Returns receipt items for a user, joined with their receipt metadata.
    status: 'pending' | 'paid' | None (both)
    """
    if USE_PG:
        with SessionLocal() as s:
            stmt = (
                select(
                    ReceiptItem.job_id_display, ReceiptItem.job_key, ReceiptItem.cost,
                    ReceiptItem.cpu_core_hours, ReceiptItem.gpu_hours, ReceiptItem.mem_gb_hours,
                    Receipt.id.label(
                        "receipt_id"), Receipt.status, Receipt.start,
                    Receipt.end, Receipt.created_at, Receipt.paid_at
                )
                .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
                .where(Receipt.username == username)
            )
            if status in ("pending", "paid"):
                stmt = stmt.where(Receipt.status == status)
            stmt = stmt.order_by(desc(Receipt.id), ReceiptItem.job_id_display)
            rows = s.execute(stmt).all()
            return [dict(row._mapping) for row in rows]

    db = get_db()
    base_sql = """
      SELECT i.job_id_display, i.job_key, i.cost, i.cpu_core_hours, i.gpu_hours, i.mem_gb_hours,
             r.id AS receipt_id, r.status, r.start, r.end, r.created_at, r.paid_at
      FROM receipt_items i
      JOIN receipts r ON r.id = i.receipt_id
      WHERE r.username = ?
    """
    args = [username]
    if status in ("pending", "paid"):
        base_sql += " AND r.status = ?"
        args.append(status)
    base_sql += " ORDER BY r.id DESC, i.job_id_display"
    cur = db.execute(base_sql, args)
    return [dict(row) for row in cur.fetchall()]


def admin_list_receipts(status: str | None = None) -> List[dict]:
    if USE_PG:
        with SessionLocal() as s:
            stmt = select(Receipt)
            if status:
                stmt = stmt.where(Receipt.status == status)
            stmt = stmt.order_by(desc(Receipt.created_at), desc(Receipt.id))
            rows = s.scalars(stmt).all()
            return [dict(
                id=r.id, username=r.username, start=r.start, end=r.end, total=r.total,
                status=r.status, created_at=r.created_at, paid_at=r.paid_at
            ) for r in rows]

    db = get_db()
    q = "SELECT id, username, start, end, total, status, created_at, paid_at FROM receipts"
    params = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY created_at DESC, id DESC"
    rows = [dict(r) for r in db.execute(q, params).fetchall()]
    return rows


def mark_receipt_paid(receipt_id: int, admin_user: str) -> bool:
    now = _now_utc_iso()
    if USE_PG:
        with SessionLocal() as s:
            r = s.get(Receipt, receipt_id)
            if not r:
                return False
            if r.status == "paid":
                return True
            if r.status != "pending":
                return False
            r.status = "paid"
            r.paid_at = now
            r.method = admin_user or "admin"
            r.tx_ref = None
            s.commit()
            return True

    db = get_db()
    row = db.execute("SELECT status FROM receipts WHERE id = ?",
                     (receipt_id,)).fetchone()
    if not row:
        return False
    if row["status"] == "paid":
        return True
    if row["status"] != "pending":
        return False
    with db:
        db.execute(
            "UPDATE receipts SET status='paid', paid_at=?, method=?, tx_ref=? WHERE id=?",
            (now, admin_user or "admin", None, receipt_id)
        )
    return True


def paid_receipts_csv():
    """
    Return (filename, csv_text) for all paid receipts (payment history).
    """
    rows = admin_list_receipts(status="paid")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "username", "start", "end",
                    "total_THB", "status", "created_at", "paid_at"])
    for r in rows:
        writer.writerow([
            r["id"], r["username"], r["start"], r["end"],
            f"{float(r['total']):.2f}", r["status"], r["created_at"], r.get(
                "paid_at") or ""
        ])
    out.seek(0)
    return ("paid_receipts_history.csv", out.read())
