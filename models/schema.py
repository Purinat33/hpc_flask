# models/schema.py
from datetime import datetime, timezone
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey, Text, String, Integer, DateTime, Index
from decimal import Decimal
from sqlalchemy import Numeric
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import (
    JSON, Boolean, PrimaryKeyConstraint, String, Text, Integer, Float, DateTime, ForeignKey, CheckConstraint,
    UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column
from models.base import Base
from datetime import datetime
from typing import Optional
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
    start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    end:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    currency:  Mapped[str] = mapped_column(
        String(3), nullable=False, default="THB")

    # money fields → DECIMAL/NUMERIC
    subtotal:   Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"))  # ex-tax
    tax_label:  Mapped[str | None] = mapped_column(
        String)                          # e.g. 'VAT'
    tax_rate:   Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0"))   # percent
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0"))   # absolute
    tax_inclusive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)

    # NEW: snapshot of pricing inputs locked at creation-time
    pricing_tier:  Mapped[str] = mapped_column(
        String, nullable=False)   # 'mu' | 'gov' | 'private'
    rate_cpu:      Mapped[Decimal] = mapped_column(
        Numeric(10, 4),  nullable=False)   # THB per CPU core-hour
    rate_gpu:      Mapped[Decimal] = mapped_column(
        Numeric(10, 4),  nullable=False)   # THB per GPU-hour
    rate_mem:      Mapped[Decimal] = mapped_column(
        Numeric(10, 4),  nullable=False)   # THB per GB-hour
    rates_locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)

    # totals / lifecycle
    total:   Mapped[Decimal] = mapped_column(
        Numeric(18, 2),  nullable=False, default=Decimal("0"))
    status:  Mapped[str] = mapped_column(
        String, nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    paid_at:    Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    method:     Mapped[str | None] = mapped_column(String)
    tx_ref:     Mapped[str | None] = mapped_column(String)

    invoice_no:  Mapped[str | None] = mapped_column(
        String, unique=True)   # e.g. INV-202502-000123
    approved_by: Mapped[str | None] = mapped_column(
        String)                # admin username
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("total >= 0", name="ck_receipts_total_ge_0"),
        CheckConstraint("status in ('pending','paid','void')",
                        name="ck_receipts_status"),
        CheckConstraint("pricing_tier in ('mu','gov','private')",
                        name="ck_receipts_tier"),
        CheckConstraint("subtotal >= 0", name="ck_receipts_subtotal_ge_0"),
        CheckConstraint("tax_amount >= 0", name="ck_receipts_tax_ge_0"),
    )


class ReceiptItem(Base):
    __tablename__ = "receipt_items"

    receipt_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False
    )
    job_key: Mapped[str] = mapped_column(String, nullable=False)
    job_id_display: Mapped[str] = mapped_column(String, nullable=False)
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
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
    cpu: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    gpu: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    mem: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
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
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)

    # Actor & request context
    actor: Mapped[str | None] = mapped_column(String(128))
    # 'admin'|'user'|...
    actor_role: Mapped[str | None] = mapped_column(String(32))
    request_id: Mapped[str | None] = mapped_column(
        String(64))     # e.g., per-request UUID
    session_id: Mapped[str | None] = mapped_column(String(64))     # optional

    ip: Mapped[str | None] = mapped_column(
        String(64))             # anonymized if configured
    ua_fingerprint: Mapped[str | None] = mapped_column(String(64))  # hashed UA

    method: Mapped[str | None] = mapped_column(String(8))
    path: Mapped[str | None] = mapped_column(String(512))

    # Event semantics
    action: Mapped[str] = mapped_column(
        String(64), nullable=False)  # controlled vocabulary
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str | None] = mapped_column(
        String(16))          # 'success'|'failure'
    status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))

    # Structured details (small, redacted)
    extra: Mapped[dict | None] = mapped_column(JSON)

    # Tamper-evident chain
    prev_hash: Mapped[str | None] = mapped_column(String(128))
    hash: Mapped[str | None] = mapped_column(String(128))
    signature: Mapped[str | None] = mapped_column(
        String(128))       # HMAC(hash, SECRET)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2)
    key_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "outcome in ('success','failure','partial','blocked', 'noop') or outcome is null", name="ck_audit_outcome"),
        Index("idx_audit_ts", "ts"),
        Index("idx_audit_actor", "actor"),
        Index("idx_audit_action", "action"),
        Index("idx_audit_target", "target_type", "target_id"),
        Index("idx_audit_request", "request_id"),
    )


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


# --- FORUM ------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ForumThread(Base):
    __tablename__ = "forum_threads"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    author_username: Mapped[str] = mapped_column(
        String, ForeignKey("users.username", ondelete="RESTRICT"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_locked: Mapped[bool] = mapped_column(default=False, nullable=False)

    author = relationship("User", lazy="joined")
    comments = relationship(
        "ForumComment",
        back_populates="thread",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ForumComment.created_at.asc()",
    )

    __table_args__ = (
        Index("ix_forum_threads_created_at", "created_at"),
        Index("ix_forum_threads_pinned_locked", "is_pinned", "is_locked"),
    )


class ForumComment(Base):
    __tablename__ = "forum_comments"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("forum_threads.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("forum_comments.id", ondelete="CASCADE"), nullable=True
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_username: Mapped[str] = mapped_column(
        String, ForeignKey("users.username", ondelete="RESTRICT"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    # relationships
    thread = relationship("ForumThread", back_populates="comments")
    author = relationship("User", lazy="joined")
    parent = relationship("ForumComment", remote_side="ForumComment.id",
                          backref="children", passive_deletes=True)

    __table_args__ = (
        Index("ix_forum_comments_thread_id", "thread_id"),
        Index("ix_forum_comments_parent_id", "parent_id"),
    )
