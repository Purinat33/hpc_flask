# models/billing_store.py (Postgres / SQLAlchemy)
from services.datetimex import now_utc, APP_TZ
from sqlalchemy import select, delete
from zoneinfo import ZoneInfo
from typing import Iterable, Tuple, List
from datetime import date, datetime, time, timezone
import re
from datetime import date, datetime, timezone
from typing import Iterable, Tuple, List, Dict

from sqlalchemy import select, func, update, delete
from sqlalchemy.exc import IntegrityError
from models.base import session_scope
from models.schema import Receipt, ReceiptItem
from models import rates_store
from services.datetimex import now_utc, to_iso_z
import re


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


def _now_utc() -> datetime:
    return now_utc()


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
                "id": r.id,
                "username": r.username,
                "start": r.start,
                "end": r.end,
                "total": float(r.total),
                "status": r.status,
                "created_at": r.created_at,
                "paid_at": r.paid_at,
                "method": r.method,
                "tx_ref": r.tx_ref,
                # NEW:
                "pricing_tier": r.pricing_tier,
                "rate_cpu": float(r.rate_cpu),
                "rate_gpu": float(r.rate_gpu),
                "rate_mem": float(r.rate_mem),
                "rates_locked_at": r.rates_locked_at,
            })
        return out


def get_receipt_with_items(receipt_id: int) -> tuple[dict, list[dict]]:
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return {}, []
        items = s.execute(
            select(ReceiptItem)
            .where(ReceiptItem.receipt_id == receipt_id)
            .order_by(ReceiptItem.job_id_display)
        ).scalars().all()
        return (
            {
                "id": r.id,
                "username": r.username,
                "start": r.start,
                "end": r.end,
                "total": float(r.total),
                "status": r.status,
                "created_at": r.created_at,
                "paid_at": r.paid_at,
                "method": r.method,
                "tx_ref": r.tx_ref,
                # NEW:
                "pricing_tier": r.pricing_tier,
                "rate_cpu": float(r.rate_cpu),
                "rate_gpu": float(r.rate_gpu),
                "rate_mem": float(r.rate_mem),
                "rates_locked_at": r.rates_locked_at,
            },
            [
                {
                    "receipt_id": i.receipt_id,
                    "job_key": i.job_key,
                    "job_id_display": i.job_id_display,
                    "cost": float(i.cost),
                    "cpu_core_hours": float(i.cpu_core_hours),
                    "gpu_hours": float(i.gpu_hours),
                    "mem_gb_hours": float(i.mem_gb_hours),
                }
                for i in items
            ],
        )


def _tz_from_app() -> ZoneInfo:
    # APP_TZ may be a string ("Asia/Bangkok") or a tzinfo (pytz/zoneinfo).
    tzname = getattr(APP_TZ, "key", None) or getattr(
        APP_TZ, "zone", None) or (APP_TZ if isinstance(APP_TZ, str) else "UTC")
    return ZoneInfo(str(tzname))


def _day_start_utc(d: date) -> datetime:
    # local day start → UTC
    tz = _tz_from_app()
    return datetime.combine(d, time(0, 0, 0), tzinfo=tz).astimezone(timezone.utc)


def _day_end_utc(d: date) -> datetime:
    # local day end → UTC (inclusive boundary)
    tz = _tz_from_app()
    return datetime.combine(d, time(23, 59, 59), tzinfo=tz).astimezone(timezone.utc)


def create_receipt_from_rows(username: str, start: str, end: str, rows: Iterable[dict]) -> Tuple[int, float, list[dict]]:
    """
    `start` / `end` are ISO dates (YYYY-MM-DD) coming from UI.
    We store *precise* UTC instants:
      start = local 00:00:00 converted to UTC
      end   = local 23:59:59 converted to UTC
    """
    now = _now_utc()
    rows = list(rows)
    inserted: List[dict] = []
    total = 0.0

    # Determine tier & snapshot rates
    tier = next((str((r.get("tier") or "")).lower()
                for r in rows if r.get("tier")), "mu")
    snap = rates_store.get_rate_for_tier(tier)

    # Build precise UTC bounds
    start_dt_utc = _day_start_utc(date.fromisoformat(start))
    end_dt_utc = _day_end_utc(date.fromisoformat(end))

    with session_scope() as s:
        r = Receipt(
            username=username,
            start=start_dt_utc,
            end=end_dt_utc,
            total=0.0,
            status="pending",
            created_at=now,
            pricing_tier=tier,
            rate_cpu=float(snap["cpu"]),
            rate_gpu=float(snap["gpu"]),
            rate_mem=float(snap["mem"]),
            rates_locked_at=now,
        )
        s.add(r)
        s.flush()  # obtain r.id

        for row in rows:
            job_key = canonical_job_id(str(row["JobID"]))
            item = ReceiptItem(
                receipt_id=r.id,
                job_key=job_key,
                job_id_display=str(row["JobID"]),
                cost=float(row.get("Cost (฿)", 0.0)),
                cpu_core_hours=float(row.get("CPU_Core_Hours", 0.0)),
                gpu_hours=float(row.get("GPU_Hours", 0.0)),
                # ✅ use used-hours to match UI/logic
                mem_gb_hours=float(row.get("Mem_GB_Hours_Used", 0.0)),
            )
            s.add(item)
            total += float(item.cost)
            inserted.append({
                "job_key": job_key,
                "job_id_display": item.job_id_display,
                "cost": float(item.cost),
                "cpu_core_hours": float(item.cpu_core_hours),
                "gpu_hours": float(item.gpu_hours),
                "mem_gb_hours": float(item.mem_gb_hours),
            })

        r.total = float(total)
        s.add(r)

    return r.id, float(total), inserted


def mark_paid(receipt_id: int, method: str = "admin", tx_ref: str | None = None):
    with session_scope() as s:
        r = s.get(Receipt, receipt_id)
        if not r:
            return
        r.status = "paid"
        r.paid_at = _now_utc()
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
        out = []
        for r in rows:
            out.append({
                "id": r.id, "username": r.username,
                "start": r.start,         # date
                "end": r.end,             # date
                "total": float(r.total), "status": r.status,
                # datetime (UTC)
                "created_at": r.created_at, "paid_at": r.paid_at,
                "pricing_tier": r.pricing_tier,
                "rate_cpu": float(r.rate_cpu), "rate_gpu": float(r.rate_gpu), "rate_mem": float(r.rate_mem),
                "rates_locked_at": r.rates_locked_at,
            })
        return out


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
        r.paid_at = _now_utc()
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
    w.writerow([
        "id", "username", "start", "end",
        "total_THB", "status", "created_at", "paid_at",
        "pricing_tier", "rate_cpu", "rate_gpu", "rate_mem", "rates_locked_at",
    ])
    for r in rows:
        w.writerow([
            r["id"], r["username"],
            r["start"].isoformat(),
            r["end"].isoformat(),
            f"{float(r['total']):.2f}", r["status"],
            r["created_at"].isoformat(),
            (r["paid_at"].isoformat().replace(
                "+00:00", "Z") if r["paid_at"] else ""),
            r.get("pricing_tier", ""), r.get("rate_cpu", ""), r.get(
                "rate_gpu", ""), r.get("rate_mem", ""),
            r["rates_locked_at"].isoformat(),
        ])
    out.seek(0)
    return ("paid_receipts_history.csv", out.read())
