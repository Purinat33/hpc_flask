# billing_store.py
import io
import csv
import os
import sqlite3
from datetime import datetime
from typing import Iterable, Tuple, Dict, Any
from models.db import get_db


def canonical_job_id(s: str) -> str:
    # 12345.batch -> 12345, 12345_7.extern -> 12345_7
    return (s or "").split(".", 1)[0].strip()


def billed_job_ids() -> set[str]:
    db = get_db()
    cur = db.execute("SELECT job_key FROM receipt_items")
    return {r[0] for r in cur.fetchall()}


def list_receipts(username: str | None = None) -> list[dict]:
    db = get_db()
    if username:
        cur = db.execute(
            "SELECT * FROM receipts WHERE username=? ORDER BY id DESC", (username,))
    else:
        cur = db.execute("SELECT * FROM receipts ORDER BY id DESC")
    return [dict(r) for r in cur.fetchall()]


def get_receipt_with_items(receipt_id: int) -> tuple[dict, list[dict]]:
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


def create_receipt_from_rows(username: str, start: str, end: str, rows: Iterable[dict]) -> Tuple[int, float, list[str]]:
    """
    rows must contain: JobID, Cost (฿), CPU_Core_Hours, GPU_Hours, Mem_GB_Hours
    Returns: (receipt_id, total, skipped_job_keys)
    """
    db = get_db()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    skipped: list[str] = []
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
            except sqlite3.IntegrityError:
                # UNIQUE(job_key) -> already billed somewhere
                skipped.append(job_key)

        db.execute("UPDATE receipts SET total=? WHERE id=?",
                   (round(total, 2), rid))
    return rid, round(total, 2), skipped


def mark_paid(receipt_id: int, method: str = "admin", tx_ref: str | None = None):
    db = get_db()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with db:
        db.execute(
            "UPDATE receipts SET status='paid', paid_at=?, method=?, tx_ref=? WHERE id=?",
            (now, method, tx_ref, receipt_id)
        )


def void_receipt(receipt_id: int):
    """Optional: frees jobs for rebilling by deleting items."""
    db = get_db()
    with db:
        db.execute("DELETE FROM receipt_items WHERE receipt_id=?", (receipt_id,))
        db.execute("UPDATE receipts SET status='void' WHERE id=?", (receipt_id,))


# billing_store.py (add near the bottom)


def list_billed_items_for_user(username: str, status: str | None = None) -> list[dict]:
    """
    Returns receipt items for a user, joined with their receipt metadata.
    status: 'pending' | 'paid' | None (both)
    """
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


# billing_store.py

DB_PATH = os.environ.get("BILLING_DB", "billing.sqlite3")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def admin_list_receipts(status: str | None = None):
    """
    List all receipts. If status provided ('pending' or 'paid'), filter by it.
    Returns list of dicts with keys: id, username, start, end, total, status, created_at, paid_at.
    """
    q = "SELECT id, username, start, end, total, status, created_at, paid_at FROM receipts"
    params = []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY created_at DESC, id DESC"
    with _connect() as cx:
        rows = [dict(r) for r in cx.execute(q, params).fetchall()]
    return rows


def mark_receipt_paid(receipt_id: int, admin_user: str):
    """
    Set a receipt to paid (idempotent; if already paid, no error).
    """
    with _connect() as cx:
        cur = cx.execute(
            "SELECT status FROM receipts WHERE id = ?", (receipt_id,))
        row = cur.fetchone()
        if not row:
            return False  # not found
        if row["status"] == "paid":
            return True   # already paid
        now = datetime.utcnow().isoformat(timespec="seconds")
        cx.execute(
            "UPDATE receipts SET status='paid', paid_at=? WHERE id=?",
            (now, receipt_id)
        )
        cx.commit()
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
            f"{float(r['total']):.2f}", r["status"], r["created_at"], r["paid_at"] or ""
        ])
    out.seek(0)
    return ("paid_receipts_history.csv", out.read())
