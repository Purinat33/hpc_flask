# models/gl.py
from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import (
    JSON, String, Integer, Numeric, DateTime, Text, ForeignKey,
    UniqueConstraint, CheckConstraint, Index
)
from datetime import datetime, date, timezone
from models.base import Base


class ExportRun(Base):
    __tablename__ = "gl_export_runs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 'xero_sales'|'xero_bank'|'posted_gl_csv'
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # 'running'|'success'|'noop'|'failed'
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    # {'start':'YYYY-MM-DD','end':'YYYY-MM-DD'}
    criteria: Mapped[dict] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    # re-export reason, failure reason, etc.
    reason: Mapped[str | None] = mapped_column(String(256))

    # evidence
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    file_size:   Mapped[int | None] = mapped_column(Integer)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    signature: Mapped[str | None] = mapped_column(
        String(64))            # HMAC(file_sha256)
    key_id: Mapped[str | None] = mapped_column(String(16))


class ExportRunBatch(Base):
    __tablename__ = "gl_export_run_batches"
    run_id: Mapped[int] = mapped_column(ForeignKey(
        "gl_export_runs.id", ondelete="CASCADE"), primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey(
        "gl_batches.id", ondelete="RESTRICT"), primary_key=True)
    seq: Mapped[int] = mapped_column(
        Integer, nullable=False)            # display order


class AccountingPeriod(Base):
    __tablename__ = "accounting_periods"
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)    # 2000..2100
    month: Mapped[int] = mapped_column(Integer, nullable=False)   # 1..12
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open")  # open|closed
    opened_at: Mapped[datetime] = mapped_column(DateTime(
        timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    opened_by: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_period_ym"),
        CheckConstraint("status in ('open','closed')",
                        name="ck_period_status"),
        CheckConstraint("year >= 2000 and year <= 2100",
                        name="ck_period_year"),
        CheckConstraint("month >= 1 and month <= 12", name="ck_period_month"),
        Index("idx_period_status", "status"),
    )


class JournalBatch(Base):
    __tablename__ = "gl_batches"
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    # bookkeeping
    source: Mapped[str] = mapped_column(
        String(32), nullable=False)     # 'billing'
    source_ref: Mapped[str] = mapped_column(
        String(64), nullable=False)  # e.g. 'R123'
    # 'issue'|'payment'|'reversal'|'closing'
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    posted_by: Mapped[str] = mapped_column(String(64), nullable=False)
    # period link (denormalized for simple queries)
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)

    exported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    export_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("gl_export_runs.id"))
    export_seq: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("source", "source_ref", "kind",
                         name="uq_batch_source_ref_kind"),
        Index("idx_batch_period", "period_year", "period_month"),
        CheckConstraint(
            "kind in ('accrual','issue','payment','reversal','closing', 'impairment')", name="ck_batch_kind"),
    )


class GLEntry(Base):
    __tablename__ = "gl_entries"
    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(Integer, ForeignKey(
        "gl_batches.id", ondelete="CASCADE"), nullable=False)

    # line
    # stored as tz dt; use .date() in reports
    date: Mapped[date] = mapped_column(DateTime(timezone=True), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(64))
    memo: Mapped[str | None] = mapped_column(Text)

    # '1000', '4000', ...
    account_id: Mapped[str] = mapped_column(String(8), nullable=False)
    account_name: Mapped[str] = mapped_column(
        String(64), nullable=False)  # denorm
    # ASSET|LIABILITY|EQUITY|INCOME|EXPENSE
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)

    debit: Mapped[float] = mapped_column(
        Numeric(18, 2), nullable=False, default=0)
    credit: Mapped[float] = mapped_column(
        Numeric(18, 2), nullable=False, default=0)

    # optional links
    receipt_id: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(
        timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    seq_in_batch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0)
    external_txn_id: Mapped[str | None] = mapped_column(
        String(64), unique=True)  # e.g. f"B{batch_id:08d}-L{seq_in_batch:05d}"

    __table_args__ = (
        CheckConstraint("debit >= 0 and credit >= 0", name="ck_gl_nonneg"),
        Index("idx_gl_date", "date"),
        Index("idx_gl_acct", "account_id"),
        Index("idx_gl_receipt", "receipt_id"),
    )
