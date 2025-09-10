# scripts/migrate_sqlite_to_pg.py
from models.schema import (
    User, Receipt, ReceiptItem, Rate, Payment, PaymentEvent, AuditLog, AuthThrottle
)
from models.base import init_engine_and_session
import os
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from dateutil import parser as dtparse
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


SQLITE_USERS = os.getenv(
    "SQLITE_USERS_URL", "sqlite:///instance/users.sqlite3")
SQLITE_BILLING = os.getenv(
    "SQLITE_BILLING_URL", "sqlite:///instance/billing.sqlite3")
# abort | skip | create
ORPHAN_POLICY = os.getenv("MIGRATION_ORPHANS", "abort").lower()


def parse_ts(s: str | None) -> str | None:
    if not s:
        return None
    try:
        dt = dtparse.isoparse(s)
        return dt.isoformat()
    except Exception:
        return s


def fetch_all(conn, sql):
    return conn.execute(text(sql)).mappings().all()


def main():
    sqlite_users = create_engine(SQLITE_USERS, future=True)
    sqlite_billing = create_engine(SQLITE_BILLING, future=True)

    pg_engine, SessionLocal = init_engine_and_session()
    with SessionLocal() as s, s.begin():
        # Phase 0: pre-read IDs to detect orphans
        with sqlite_billing.connect() as bconn:
            receipt_ids = {row["id"] for row in fetch_all(
                bconn, "SELECT id FROM receipts")}
            orphan_payment_rows = fetch_all(
                bconn,
                """SELECT id, receipt_id, provider, username, status, currency, amount_cents,
                          external_payment_id, checkout_url, idempotency_key, created_at, updated_at
                   FROM payments
                   WHERE receipt_id NOT IN (SELECT id FROM receipts)"""
            )

        if orphan_payment_rows:
            print(
                f"[!] Found {len(orphan_payment_rows)} orphan payments (receipt_id missing). Policy={ORPHAN_POLICY}")
            if ORPHAN_POLICY == "abort":
                for r in orphan_payment_rows[:10]:
                    print("   - orphan payment id=",
                          r["id"], " -> receipt_id=", r["receipt_id"])
                raise SystemExit(
                    "Aborting due to orphan payments. Set MIGRATION_ORPHANS=skip or create to proceed.")
        # Phase 1: users + rates
        with sqlite_users.connect() as uconn:
            for r in fetch_all(uconn, "SELECT username, password_hash, role, created_at FROM users"):
                s.add(User(
                    username=r["username"], password_hash=r["password_hash"], role=r["role"],
                    created_at=parse_ts(r["created_at"])
                ))

        with sqlite_billing.connect() as bconn:
            for r in fetch_all(bconn, "SELECT tier, cpu, gpu, mem, updated_at FROM rates"):
                s.add(Rate(
                    tier=r["tier"], cpu=r["cpu"], gpu=r["gpu"], mem=r["mem"],
                    updated_at=parse_ts(r["updated_at"])
                ))
        s.flush()  # ensure early issues show up here

        # Phase 2: receipts
        with sqlite_billing.connect() as bconn:
            for r in fetch_all(bconn, 'SELECT id, username, start, "end", total, status, created_at, paid_at, method, tx_ref FROM receipts'):
                s.add(Receipt(
                    id=r["id"], username=r["username"],
                    start=parse_ts(r["start"]), end=parse_ts(r["end"]),
                    total=r["total"], status=r["status"], created_at=parse_ts(
                        r["created_at"]),
                    paid_at=parse_ts(r["paid_at"]), method=r["method"], tx_ref=r["tx_ref"]
                ))
        s.flush()

        # Phase 3: receipt_items
        with sqlite_billing.connect() as bconn:
            for r in fetch_all(bconn, """SELECT receipt_id, job_key, job_id_display, cost, cpu_core_hours, gpu_hours, mem_gb_hours FROM receipt_items"""):
                s.add(ReceiptItem(
                    receipt_id=r["receipt_id"], job_key=r["job_key"], job_id_display=r["job_id_display"],
                    cost=r["cost"], cpu_core_hours=r["cpu_core_hours"], gpu_hours=r["gpu_hours"], mem_gb_hours=r["mem_gb_hours"]
                ))
        s.flush()

        # Phase 4: payments (handle orphans per policy)
        with sqlite_billing.connect() as bconn:
            pay_rows = fetch_all(bconn, """SELECT id, provider, receipt_id, username, status, currency, amount_cents,
                                                  external_payment_id, checkout_url, idempotency_key, created_at, updated_at
                                           FROM payments""")
            for r in pay_rows:
                rid = r["receipt_id"]
                if rid not in receipt_ids:
                    if ORPHAN_POLICY == "skip":
                        print(
                            f"[skip] payment id={r['id']} receipt_id={rid} (no such receipt)")
                        continue
                    elif ORPHAN_POLICY == "create":
                        # create a placeholder receipt once per missing rid
                        print(
                            f"[create] placeholder receipt id={rid} for orphan payment id={r['id']}")
                        s.add(Receipt(
                            id=rid, username=r["username"], start=parse_ts(
                                r["created_at"]),
                            end=parse_ts(r["created_at"]), total=0.0, status="void",
                            created_at=parse_ts(r["created_at"]), paid_at=None, method=None, tx_ref=None
                        ))
                        receipt_ids.add(rid)
                        s.flush()

                s.add(Payment(
                    id=r["id"], provider=r["provider"], receipt_id=rid, username=r["username"], status=r["status"],
                    currency=r["currency"], amount_cents=r["amount_cents"], external_payment_id=r["external_payment_id"],
                    checkout_url=r["checkout_url"], idempotency_key=r["idempotency_key"],
                    created_at=parse_ts(r["created_at"]), updated_at=parse_ts(r["updated_at"])
                ))
        s.flush()

        # Phase 5: payment_events, audit_log, auth_throttle
        with sqlite_billing.connect() as bconn:
            for r in fetch_all(bconn, """SELECT id, provider, external_event_id, payment_id, event_type, raw, signature_ok, received_at FROM payment_events"""):
                s.add(PaymentEvent(
                    id=r["id"], provider=r["provider"], external_event_id=r["external_event_id"],
                    payment_id=r["payment_id"], event_type=r["event_type"], raw=r["raw"],
                    signature_ok=r["signature_ok"], received_at=parse_ts(
                        r["received_at"])
                ))
            for r in fetch_all(bconn, """SELECT id, ts, actor, ip, ua, method, path, action, target, status, extra, prev_hash, hash FROM audit_log"""):
                s.add(AuditLog(
                    id=r["id"], ts=parse_ts(r["ts"]), actor=r["actor"], ip=r["ip"], ua=r["ua"],
                    method=r["method"], path=r["path"], action=r["action"], target=r["target"],
                    status=r["status"], extra=r["extra"], prev_hash=r["prev_hash"], hash=r["hash"]
                ))
            for r in fetch_all(bconn, """SELECT id, username, ip, window_start, fail_count, locked_until FROM auth_throttle"""):
                s.add(AuthThrottle(
                    id=r["id"], username=r["username"], ip=r["ip"],
                    window_start=parse_ts(r["window_start"]), fail_count=r["fail_count"], locked_until=parse_ts(r["locked_until"])
                ))

        # done (transaction committed by context manager)

    print("Data migrated successfully.")


if __name__ == "__main__":
    main()
