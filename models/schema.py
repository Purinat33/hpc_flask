# models/schema.py
from sqlalchemy import (
    PrimaryKeyConstraint, String, Text, Integer, Float, Date, DateTime, ForeignKey, CheckConstraint,
    UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from models.base import Base
from datetime import datetime, date
# --- USERS (users.sqlite3)


class User(Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String, primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(
        String, nullable=False)  # ('admin','user')
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("role in ('admin','user')", name="ck_users_role"),
    )

# --- BILLING (billing.sqlite3)


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    # who / period
    username: Mapped[str] = mapped_column(
        String, nullable=False)  # optional FK to users.username
    start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end:   Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # NEW: snapshot of pricing inputs locked at creation-time
    pricing_tier:  Mapped[str] = mapped_column(
        String, nullable=False)   # 'mu' | 'gov' | 'private'
    rate_cpu:      Mapped[float] = mapped_column(
        Float,  nullable=False)   # THB per CPU core-hour
    rate_gpu:      Mapped[float] = mapped_column(
        Float,  nullable=False)   # THB per GPU-hour
    rate_mem:      Mapped[float] = mapped_column(
        Float,  nullable=False)   # THB per GB-hour
    rates_locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)

    # totals / lifecycle
    total:   Mapped[float] = mapped_column(Float,  nullable=False, default=0.0)
    status:  Mapped[str] = mapped_column(
        String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    paid_at:    Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    method:     Mapped[str | None] = mapped_column(String)
    tx_ref:     Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_receipts_total_ge_0"),
        CheckConstraint("status in ('pending','paid','void')",
                        name="ck_receipts_status"),
        CheckConstraint("pricing_tier in ('mu','gov','private')",
                        name="ck_receipts_tier"),
    )


class ReceiptItem(Base):
    __tablename__ = "receipt_items"

    receipt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False
    )
    job_key: Mapped[str] = mapped_column(String, nullable=False)
    job_id_display: Mapped[str] = mapped_column(String, nullable=False)
    cost: Mapped[float] = mapped_column(Float, nullable=False)
    cpu_core_hours: Mapped[float] = mapped_column(Float, nullable=False)
    gpu_hours: Mapped[float] = mapped_column(Float, nullable=False)
    mem_gb_hours: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("receipt_id", "job_key", name="pk_receipt_items"),
        UniqueConstraint("job_key", name="uq_receipt_items_job_key"),
        Index("idx_items_receipt", "receipt_id"),
    )


# Index("idx_items_receipt", ReceiptItem.receipt_id)


class Rate(Base):
    __tablename__ = "rates"
    tier: Mapped[str] = mapped_column(
        String, primary_key=True)  # 'mu' | 'gov' | 'private'
    cpu: Mapped[float] = mapped_column(Float, nullable=False)
    gpu: Mapped[float] = mapped_column(Float, nullable=False)
    mem: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    receipt_id: Mapped[int] = mapped_column(Integer, ForeignKey(
        "receipts.id", ondelete="CASCADE"), nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending")
    currency: Mapped[str] = mapped_column(String, nullable=False)  # 3-letter
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    external_payment_id: Mapped[str | None] = mapped_column(String)
    checkout_url: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint("amount_cents >= 0", name="ck_payments_amount_ge_0"),
        CheckConstraint(
            "status in ('pending','succeeded','failed','canceled')", name="ck_payments_status"),
        UniqueConstraint("external_payment_id",
                         name="uq_payments_external_payment_id"),
        # Partial unique on (provider, idempotency_key) when idempotency_key IS NOT NULL
        # will be added in Alembic migration as a PostgreSQL-partial index (section 5).
    )


Index("idx_payments_receipt", Payment.receipt_id)
Index("idx_payments_status", Payment.status)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(String)
    payment_id: Mapped[int | None] = mapped_column(
        Integer)  # intended FK to payments.id (nullable)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    raw: Mapped[str] = mapped_column(Text, nullable=False)
    signature_ok: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("provider", "external_event_id",
                         name="uq_paymentevents_provider_external"),
        CheckConstraint("signature_ok IN (0,1)",
                        name="ck_paymentevents_signature_ok"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    actor: Mapped[str | None] = mapped_column(String)
    ip: Mapped[str | None] = mapped_column(String)
    ua: Mapped[str | None] = mapped_column(String)
    method: Mapped[str | None] = mapped_column(String)
    path: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String)
    status: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[str | None] = mapped_column(String)
    hash: Mapped[str | None] = mapped_column(String)


class AuthThrottle(Base):
    __tablename__ = "auth_throttle"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    ip: Mapped[str] = mapped_column(String, nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))


Index("idx_auth_throttle_user_ip", AuthThrottle.username,
      AuthThrottle.ip, unique=True)


class UserTierOverride(Base):
    __tablename__ = "user_tier_overrides"
    username: Mapped[str] = mapped_column(String, primary_key=True)
    tier: Mapped[str] = mapped_column(
        String, nullable=False)  # 'mu'|'gov'|'private'
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("tier in ('mu','gov','private')",
                        name="ck_user_tier_overrides_tier"),
    )
