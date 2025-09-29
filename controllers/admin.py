from sqlalchemy import select
from models.gl import AccountingPeriod
from services.accounting_export import run_formal_gl_export
from services.gl_posting import close_period, reopen_period, post_service_accruals_for_period, bootstrap_periods
from datetime import date, datetime, timezone
from weasyprint import HTML
from flask import current_app, flash, make_response
# add at top if not imported
from models.billing_store import get_receipt_with_items, list_receipts, revert_receipt_to_pending
from calendar import monthrange
from services.forecast import build_daily_series, multi_horizon_forecast
from services.accounting import derive_journal, trial_balance, income_statement, balance_sheet
from flask import jsonify
from datetime import timedelta
from services.org_info import ORG_INFO, ORG_INFO_TH
from services.pricing_sim import build_pricing_components, simulate_vs_current
import io
from datetime import date
import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for, Response
from flask_login import fresh_login_required, login_required, current_user

from controllers.auth import admin_required
from models import rates_store
from models.rates_store import save_rates
from services.data_sources import fetch_jobs_with_fallbacks
from services.billing import compute_costs
from models.billing_store import (
    billed_job_ids, canonical_job_id,
    admin_list_receipts, mark_receipt_paid, paid_receipts_csv,
    list_receipts, create_receipt_from_rows,
)
from models.audit_store import audit
from models.audit_store import list_audit, export_csv
from services.metrics import (
    RECEIPT_MARKED_PAID, CSV_DOWNLOADS, RECEIPT_CREATED
)
from datetime import date, timedelta
import pandas as pd
import json
from services.datetimex import APP_TZ
from models.tiers_store import load_overrides_dict
from services.billing import classify_user_type
from models.base import session_scope
from models.schema import User
from services.data_sources import fetch_jobs_with_fallbacks
from models.billing_store import bulk_void_pending_invoices_for_month
from models.billing_store import _tax_cfg
from services.gl_posting import (
    post_receipt_issued, post_receipt_paid, reverse_receipt_postings,
    close_period, reopen_period
)

admin_bp = Blueprint("admin", __name__)


def _to_utc_day_end(ts_date: str) -> pd.Timestamp:
    """Inclusive day end in UTC (23:59:59)."""
    return pd.Timestamp(ts_date, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)


def _ensure_col(df: pd.DataFrame, name: str, default=0.0) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce").fillna(default)
    return pd.Series([default] * len(df), index=df.index)


def _month_range_for_year(year: int) -> tuple[str, str]:
    ystart = date(year, 1, 1).isoformat()
    if year == date.today().year:
        yend = date.today().isoformat()
    else:
        yend = date(year, 12, 31).isoformat()
    return ystart, yend


def _monthly_aggregate(df: pd.DataFrame) -> tuple[list[dict], float]:
    """Return ([{month, jobs, CPU_Core_Hours, GPU_Hours, Mem_GB_Hours_Used, Cost (฿)}], year_total_cost)."""
    if df.empty or "End" not in df.columns:
        return [], 0.0
    # numeric safety
    cols_num = ["CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours_Used", "Cost (฿)"]
    for c in cols_num:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        else:
            df[c] = 0.0

    df["_month"] = df["End"].dt.month
    g = (
        df.groupby("_month", dropna=True)
        .agg(
            jobs=("JobID", "nunique"),
            CPU_Core_Hours=("CPU_Core_Hours", "sum"),
            GPU_Hours=("GPU_Hours", "sum"),
            Mem_GB_Hours_Used=("Mem_GB_Hours_Used", "sum"),
            Cost=("Cost (฿)", "sum"),
        )
        .reset_index()
        .rename(columns={"Cost": "Cost (฿)", "_month": "month"})
        .sort_values("month")
    )
    rows = g.to_dict(orient="records")
    total = float(g["Cost (฿)"].sum()) if not g.empty else 0.0
    return rows, total


def _filter_month(df: pd.DataFrame, m: int) -> pd.DataFrame:
    if df.empty or "End" not in df.columns:
        return df.iloc[0:0]
    return df[df["End"].dt.month == int(m)].copy()


def _collect_all_users_for_datalist(before_iso: str) -> list[str]:
    """Union of DB users, overrides, and users seen in jobs in last N days (default 365)."""
    names = set()

    # DB users
    try:
        with session_scope() as s:
            db_users = [u[0] for u in s.query(User.username).all()]
            names.update(u for u in db_users if (u or "").strip())
    except Exception:
        pass

    # overrides
    try:
        ov = load_overrides_dict() or {}
        names.update(ov.keys())
    except Exception:
        pass

    # job users (last 365d)
    try:
        lookback_days = 365
        jobs_start = (date.fromisoformat(before_iso) -
                      timedelta(days=lookback_days)).isoformat()
        jobs_end = before_iso
        df_jobs, _, _ = fetch_jobs_with_fallbacks(jobs_start, jobs_end)
        if not df_jobs.empty and "User" in df_jobs.columns:
            for u in df_jobs["User"].astype(str).fillna("").str.strip().unique().tolist():
                if u:
                    names.add(u)
    except Exception:
        pass

    return sorted(names, key=lambda s: s.lower())


# controllers/admin.py  (ADD)
def _load_posted_journal(start_iso: str, end_iso: str) -> pd.DataFrame:
    """Read posted GL (gl_entries joined to gl_batches) for the window."""
    from sqlalchemy import select
    from models.gl import JournalBatch, GLEntry

    def _to_utc_start(diso: str) -> datetime:
        # naive date -> UTC 00:00:00
        return datetime.fromisoformat(diso).replace(tzinfo=timezone.utc)

    def _to_utc_end_inclusive(diso: str) -> datetime:
        # naive date -> UTC 23:59:59
        return datetime.fromisoformat(diso).replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59, seconds=59)

    start_utc = _to_utc_start(start_iso)
    end_utc = _to_utc_end_inclusive(end_iso)

    with session_scope() as s:
        rows = s.execute(
            select(
                GLEntry.date,
                GLEntry.ref,
                GLEntry.memo,
                GLEntry.account_id,
                GLEntry.account_name,
                GLEntry.account_type,
                GLEntry.debit,
                GLEntry.credit,
            )
            .join(JournalBatch, GLEntry.batch_id == JournalBatch.id)
            .where(GLEntry.date >= start_utc, GLEntry.date <= end_utc)
            .order_by(GLEntry.date, GLEntry.ref, GLEntry.account_id)
        ).all()

    df = pd.DataFrame(rows, columns=[
        "date", "ref", "memo", "account_id", "account_name",
        "account_type", "debit", "credit"
    ])
    if df.empty:
        return df
    # Pretty dates + numeric safety
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.date.astype(str)
    df["debit"] = pd.to_numeric(df["debit"], errors="coerce").fillna(0.0)
    df["credit"] = pd.to_numeric(df["credit"], errors="coerce").fillna(0.0)
    return df


@admin_bp.get("/admin")
@login_required
@admin_required
def admin_form():
    rates = rates_store.load_rates()

    # ---- parse & sanitize query params ----
    section = (request.args.get("section") or "usage").strip().lower()
    if section not in {"rates", "usage", "billing", "myusage", "dashboard", "tiers"}:
        section = "usage"

    tier = (request.args.get("type") or "mu").strip().lower()
    if tier not in rates:
        tier = "mu"

    view = (request.args.get("view") or "detail").strip().lower()
    if section == "myusage":
        if view not in {"detail", "aggregate"}:
            view = "detail"
    elif section == "usage":
        if view not in {"detail", "aggregate", "trend"}:
            view = "detail"
    else:
        if view not in {"detail", "aggregate"}:
            view = "detail"

    # legacy usage pages
    EPOCH_START = "1970-01-01"
    before = request.args.get("before") or date.today().isoformat()
    start_d, end_d = EPOCH_START, before

    # usage trend inputs
    selected_user = (request.args.get("u") or "").strip()
    try:
        year = int(request.args.get("year") or date.today().year)
    except Exception:
        year = date.today().year
    month = request.args.get("month")
    try:
        month = int(month) if month else None
    except Exception:
        month = None
    current_year = date.today().year

    # NEW: dashboard month filters (YYYY-MM)
    m1 = (request.args.get("m1") or "").strip()  # e.g. "2025-02"
    m2 = (request.args.get("m2") or "").strip()  # optional compare month

    # optional local filter for usage tables
    q_user = (request.args.get("q") or "").strip()

    # ---- shared defaults for template context ----
    rows: list[dict] = []
    agg_rows: list[dict] = []
    grand_total = 0.0
    data_source = None
    notes: list[str] = []
    tot_cpu = tot_gpu = tot_mem = tot_elapsed = 0.0
    pending: list[dict] = []
    paid: list[dict] = []
    my_pending_receipts: list[dict] = []
    my_paid_receipts: list[dict] = []
    sum_pending = 0.0
    sum_paid = 0.0

    raw_cols: list[str] = []
    raw_rows: list[dict] = []
    header_classes: dict[str, str] = {}
    all_users: list[str] = []

    # usage-trend context
    monthly_agg = []
    month_detail_rows = []
    year_total = 0.0
    tot_cpu_m = tot_gpu_m = tot_mem_m = month_total = 0.0

    # Dashboard-only context
    kpis: dict = {}
    # Single-month or default aggregate series
    series = {
        "daily_cost": [], "daily_labels": [],
        "tier_labels": [], "tier_values": [],
        "top_users_labels": [], "top_users_values": [],
        "node_jobs_labels": [], "node_jobs_values": [],
        "node_cpu_labels": [],  "node_cpu_values": [],
        "node_gpu_labels": [],  "node_gpu_values": [],
        "energy_user_labels": [], "energy_user_values": [],
        "energy_node_labels": [], "energy_node_values": [],
        "energy_eff_user_labels": [], "energy_eff_user_values": [],
        "succ_user_labels": [], "succ_user_success": [], "succ_user_fail": [],
        "fail_exit_labels": [], "fail_exit_values": [],
        "fail_state_labels": [], "fail_state_values": [],
    }
    # Optional: comparison month series (second panel)
    series_b = {k: ([] if "labels" in k or "values" in k or isinstance(
        v, list) else []) for k, v in series.items()}

    # small helpers
    def _to_utc_day_end(ts_date: str) -> pd.Timestamp:
        return pd.Timestamp(ts_date, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)

    def _ensure_col(df: pd.DataFrame, name: str, default=0.0) -> pd.Series:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce").fillna(default)
        return pd.Series([default] * len(df), index=df.index)

    def _safe_float(x, default=0.0):
        try:
            if x is None:
                return default
            s = str(x).strip().replace("฿", "").replace(",", "")
            return float(s) if s else default
        except Exception:
            return default

    def _month_bounds(ym: str) -> tuple[str, str]:
        """Return ISO start/end for the month (local calendar)."""
        y, m = [int(p) for p in ym.split("-", 1)]
        last = monthrange(y, m)[1]
        return date(y, m, 1).isoformat(), date(y, m, last).isoformat()

    def _filter_month(df: pd.DataFrame, ym: str) -> pd.DataFrame:
        if df.empty or "End" not in df.columns or not ym:
            return df.iloc[0:0]
        y, m = [int(p) for p in ym.split("-", 1)]
        return df[(df["End"].dt.year == y) & (df["End"].dt.month == m)].copy()

    def _build_all_series(df: pd.DataFrame) -> dict:
        """Build the same ‘series’ dict as above for a given filtered dataframe."""
        out = {k: ([] if isinstance(v, list) else [])
               for k, v in series.items()}

        if df.empty:
            return out

        # Daily total cost
        if "End" in df.columns and "Cost (฿)" in df.columns:
            daily = df.groupby(df["End"].dt.date)[
                "Cost (฿)"].sum().sort_index()
            out["daily_labels"] = [d.isoformat() for d in daily.index]
            out["daily_cost"] = [round(float(v), 2) for v in daily.values]

        # Cost by tier
        if "tier" in df.columns and "Cost (฿)" in df.columns:
            tier_sum = df.groupby("tier", dropna=False)[
                "Cost (฿)"].sum().sort_values(ascending=False)
            out["tier_labels"] = [str(i).upper() for i in tier_sum.index]
            out["tier_values"] = [round(float(v), 2) for v in tier_sum.values]

        # Top 10 Users by Cost
        if "User" in df.columns and "Cost (฿)" in df.columns:
            top = df.groupby("User")["Cost (฿)"].sum(
            ).sort_values(ascending=False).head(10)
            out["top_users_labels"] = list(top.index)
            out["top_users_values"] = [round(float(v), 2) for v in top.values]

        # Nodes: jobs, cpu core-hrs, gpu-hrs
        if "NodeList" in df.columns:
            d = df.copy()
            d["NodeList"] = d["NodeList"].astype(str).fillna("").str.strip()

            jobs = d[d["NodeList"] != ""].groupby(
                "NodeList")["JobID"].nunique().sort_values(ascending=False).head(10)
            cpu = d.groupby("NodeList")["CPU_Core_Hours"].sum(
            ).sort_values(ascending=False).head(10)
            gpu = d.groupby("NodeList")["GPU_Hours"].sum(
            ).sort_values(ascending=False).head(10)

            out["node_jobs_labels"], out["node_jobs_values"] = list(
                jobs.index), [int(v) for v in jobs.values]
            out["node_cpu_labels"],  out["node_cpu_values"] = list(
                cpu.index),  [round(float(v), 2) for v in cpu.values]
            out["node_gpu_labels"],  out["node_gpu_values"] = list(
                gpu.index),  [round(float(v), 2) for v in gpu.values]

        # Success vs Fail — User
        if "User" in df.columns and "State" in df.columns:
            d = df.copy()
            d["User"] = d["User"].astype(str).fillna("").str.strip()
            d["ok"] = d["State"].astype(
                str).str.upper().str.startswith("COMPLETED")
            g_ok = d.groupby("User")["ok"].sum()
            g_n = d.groupby("User")["ok"].count()
            g_fail = (g_n - g_ok)
            top = (g_ok + g_fail).sort_values(ascending=False).head(10).index
            out["succ_user_labels"] = list(top)
            out["succ_user_success"] = [int(g_ok.get(u, 0)) for u in top]
            out["succ_user_fail"] = [int(g_fail.get(u, 0)) for u in top]

        # Failure exit codes + reasons
        if "State" in df.columns:
            d = df.copy()
            d["State"] = d["State"].astype(
                str).fillna("").str.strip().str.upper()
            if "ExitCode" in d.columns:
                ex = d["ExitCode"].astype(str).str.split(
                    ":", n=1, expand=True)[0]
                d["_exit"] = ex.where(ex.str.match(r"^\d+$"), other="0")
            else:
                d["_exit"] = "0"
            fail_mask = ~d["State"].str.startswith("COMPLETED")
            df_fail = d[fail_mask]
            exit_top = df_fail.groupby("_exit")["_exit"].count(
            ).sort_values(ascending=False).head(8)
            reason_top = df_fail.groupby("State")["State"].count(
            ).sort_values(ascending=False).head(8)
            out["fail_exit_labels"],  out["fail_exit_values"] = list(
                exit_top.index),  [int(v) for v in exit_top.values]
            out["fail_state_labels"], out["fail_state_values"] = list(
                reason_top.index), [int(v) for v in reason_top.values]

        return out

    # ---- DASHBOARD ----
    if section == "dashboard":
        def cap(name, fn, default):
            try:
                return fn()
            except Exception as e:
                notes.append(f"dashboard.{name}: {e!s}")
                return default

        # Base KPIs scope stays as-is (last 90d window to compute daily series; KPIs remain all-time/30d as before)
        end_d = before
        start_90 = (date.fromisoformat(before) -
                    timedelta(days=90)).isoformat()

        # If the admin picked month(s), fetch a larger YTD (or spanning) window so we can slice months precisely.
        # Otherwise keep your original 90d window behavior.
        def _fetch_window_for_months():
            if not m1 and not m2:
                return start_90, end_d  # default
            # Build the minimal window that covers both months (YTD is fine, but we'll be tight here)
            months = [ym for ym in [m1, m2] if ym]
            years = sorted({int(ym.split("-", 1)[0]) for ym in months})
            y_start = min(years)
            y_end = max(years)
            # inclusive to the end of last selected month
            last_m = int((sorted(months)[-1]).split("-", 1)[1])
            last_day = monthrange(y_end, last_m)[1]
            return date(y_start, 1, 1).isoformat(), date(y_end, last_m, last_day).isoformat()

        fetch_start, fetch_end = _fetch_window_for_months()

        df, data_source, ds_notes = cap(
            "fetch",
            lambda: fetch_jobs_with_fallbacks(fetch_start, fetch_end),
            (pd.DataFrame(), None, []),
        )
        notes.extend(ds_notes or [])
        df = cap("compute_costs", lambda: compute_costs(df), pd.DataFrame())

        def _cutoff_df(d_in: pd.DataFrame, end_iso: str):
            if "End" in d_in.columns:
                end_series = pd.to_datetime(
                    d_in["End"], errors="coerce", utc=True)
                cutoff_utc = _to_utc_day_end(end_iso)
                out = d_in[end_series.notna() & (
                    end_series <= cutoff_utc)].copy()
                out["End"] = end_series
                return out
            out = d_in.copy()
            out["End"] = pd.NaT
            return out

        df = cap("cutoff", lambda: _cutoff_df(df, fetch_end), df)

        # KPIs (unchanged semantics)
        def _unbilled():
            d = df.copy()
            d["JobKey"] = d["JobID"].astype(str).map(canonical_job_id)
            already = set(billed_job_ids())
            return d[~d["JobKey"].isin(already)]

        df_unbilled = cap("unbilled", _unbilled, pd.DataFrame())
        kpis["unbilled_cost"] = cap(
            "kpi.unbilled_cost",
            lambda: float(_ensure_col(df_unbilled, "Cost (฿)",
                          0).sum()) if not df_unbilled.empty else 0.0,
            0.0,
        )

        pending = cap("q.pending_receipts", lambda: admin_list_receipts(
            status="pending") or [], [])
        kpis["pending_receivables"] = cap(
            "kpi.pending_receivables",
            lambda: float(sum(_safe_float(r.get("total")) for r in pending)),
            0.0,
        )

        paid = cap("q.paid_receipts", lambda: admin_list_receipts(
            status="paid") or [], [])
        kpis["paid_last_30d"] = cap(
            "kpi.paid_last_30d",
            lambda: sum(
                _safe_float(r.get("total"))
                for r in paid
                if pd.notna(pd.to_datetime(r.get("paid_at"), errors="coerce", utc=True))
                and pd.to_datetime(r.get("paid_at"), errors="coerce", utc=True)
                >= (pd.Timestamp(fetch_end, tz="UTC") - pd.Timedelta(days=30))
            ),
            0.0,
        )

        kpis["jobs_last_30d"] = cap(
            "kpi.jobs_last_30d",
            lambda: int((df["End"] >= (pd.Timestamp(
                fetch_end, tz="UTC") - pd.Timedelta(days=30))).sum())
            if "End" in df.columns else 0,
            0,
        )

        # === Single-month vs. month-compare data preparation ===
        if m1:
            df_a = _filter_month(df, m1)
            series.update(_build_all_series(df_a))
        else:
            # No month specified → keep prior behavior but still use last 90 days for the timeseries line
            df_90 = df[(df["End"].dt.date >= pd.to_datetime(
                start_90).date())] if "End" in df.columns and not df.empty else df
            series.update(_build_all_series(df_90))

        if m2:
            df_b = _filter_month(df, m2)
            series_b.update(_build_all_series(df_b))

        # Totals chips (computed from whichever view is in primary panel)
        base_df_for_totals = df_a if m1 else (df[(df["End"].dt.date >= pd.to_datetime(
            start_90).date())] if "End" in df.columns else df)
        tot_cpu = cap("totals.cpu", lambda: float(_ensure_col(
            base_df_for_totals, "CPU_Core_Hours", 0).sum()), 0.0)
        tot_gpu = cap("totals.gpu", lambda: float(
            _ensure_col(base_df_for_totals, "GPU_Hours", 0).sum()), 0.0)
        tot_mem = cap(
            "totals.mem",
            lambda: float(
                _ensure_col(base_df_for_totals, "Mem_GB_Hours", 0).sum()
                if "Mem_GB_Hours" in base_df_for_totals.columns else _ensure_col(base_df_for_totals, "Mem_GB_Hours_Used", 0).sum()
            ),
            0.0,
        )
        tot_elapsed = cap("totals.elapsed", lambda: float(
            _ensure_col(base_df_for_totals, "Elapsed_Hours", 0).sum()), 0.0)

        return render_template(
            "admin/dashboard.html",
            current_user=current_user,
            before=before, start=start_d, end=fetch_end,
            kpis=kpis, series=series, series_b=series_b, data_source=data_source, notes=notes,
            tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
            url_for=url_for, all_rates=rates,
            m1=m1, m2=m2,
        )

    # ---- USAGE / MYUSAGE / BILLING / TIERS ----
    try:
        if section == "usage":
            # --- three subviews: detail | aggregate | trend ---
            if view in {"detail", "aggregate"}:
                # RAW (parents+steps)
                df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                    start_d, end_d)

                if not df_raw.empty:
                    if "End" in df_raw.columns:
                        end_series = pd.to_datetime(
                            df_raw["End"], errors="coerce", utc=True)
                        cutoff_utc = _to_utc_day_end(end_d)
                        df_raw = df_raw[end_series.notna() & (
                            end_series <= cutoff_utc)]
                        df_raw["End"] = end_series

                    # all users (for datalist) BEFORE q filter
                    if "User" in df_raw.columns:
                        all_users = sorted(
                            u for u in df_raw["User"].astype(str).fillna("").str.strip().unique() if u
                        )

                    # optional partial-user filter
                    if q_user and "User" in df_raw.columns and "JobID" in df_raw.columns:
                        df_raw["JobKey"] = df_raw["JobID"].astype(
                            str).map(canonical_job_id)
                        parents = df_raw[df_raw["JobID"].astype(
                            str) == df_raw["JobKey"]].copy()
                        user_str = parents["User"].astype(str).fillna("")
                        keep_keys = set(
                            parents.loc[user_str.str.contains(
                                q_user, case=False, regex=False), "JobKey"]
                        )
                        df_raw = df_raw[df_raw["JobKey"].isin(
                            keep_keys)].drop(columns=["JobKey"])

                    raw_cols = list(df_raw.columns)
                    raw_rows = df_raw.head(200).to_dict(orient="records")

                # computed (parent-aggregated), hide already billed
                df = compute_costs(
                    df_raw.copy() if df_raw is not None else pd.DataFrame())
                if not df.empty:
                    df["JobKey"] = df["JobID"].astype(
                        str).map(canonical_job_id)
                    already = billed_job_ids()
                    df = df[~df["JobKey"].isin(already)]

                # totals
                tot_cpu = float(_ensure_col(df, "CPU_Core_Hours", 0).sum())
                tot_gpu = float(_ensure_col(df, "GPU_Hours", 0).sum())
                tot_mem = float(_ensure_col(df, "Mem_GB_Hours_Used", 0).sum())
                tot_elapsed = float(_ensure_col(df, "Elapsed_Hours", 0).sum())

                # detail rows
                cols = [
                    "User", "JobID", "Elapsed", "End", "State",
                    "CPU_Core_Hours",
                    "GPU_Count", "GPU_Hours",
                    "Memory_GB", "Mem_GB_Hours_Used", "Mem_GB_Hours_Alloc",
                    "tier", "Cost (฿)"
                ]
                for c in cols:
                    if c not in df.columns:
                        df[c] = ""
                rows = df[cols].to_dict(orient="records")

                # aggregate rows
                if not df.empty:
                    agg = (
                        df.groupby(["User", "tier"], dropna=False)
                        .agg(
                            jobs=("JobID", "count"),
                            CPU_Core_Hours=("CPU_Core_Hours", "sum"),
                            GPU_Hours=("GPU_Hours", "sum"),
                            Mem_GB_Hours_Used=("Mem_GB_Hours_Used", "sum"),
                            Cost=("Cost (฿)", "sum"),
                        ).reset_index()
                    )
                    agg.rename(columns={"Cost": "Cost (฿)"}, inplace=True)
                    agg_rows = agg[["User", "tier", "jobs", "CPU_Core_Hours",
                                    "GPU_Hours", "Mem_GB_Hours_Used", "Cost (฿)"]].to_dict(orient="records")
                    grand_total = float(agg["Cost (฿)"].sum())
                else:
                    grand_total = 0.0

                header_classes = {c: "" for c in raw_cols}
                for c in raw_cols:
                    if c == "TotalCPU":
                        header_classes[c] = "hl-primary"
                    elif c == "CPUTimeRAW":
                        header_classes[c] = "hl-fallback1"
                    elif c in {"AllocTRES", "ReqTRES", "Elapsed"}:
                        header_classes[c] = "hl-fallback2"
                    elif c == "AveRSS":
                        header_classes[c] = "hl-primary"

            else:  # view == "trend"
                try:
                    y = int(year or current_year)
                except Exception:
                    y = current_year
                ym_start = f"{y}-01-01"
                ym_end = (date.today().isoformat() if y ==
                          date.today().year else f"{y}-12-31")

                # Fetch all users, compute costs
                df_raw, data_source, ds_notes = fetch_jobs_with_fallbacks(
                    ym_start, ym_end)
                notes.extend(ds_notes or [])
                df = compute_costs(df_raw)

                if "End" in df.columns:
                    end_series = pd.to_datetime(
                        df["End"], errors="coerce", utc=True)
                    cutoff_utc = pd.Timestamp(
                        ym_end, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
                    df = df[end_series.notna() & (
                        end_series <= cutoff_utc)].copy()
                    df["End"] = end_series
                else:
                    df["End"] = pd.NaT

                # Build "all_users" list (for datalist)
                if "User" in df_raw.columns:
                    all_users = sorted(u for u in df_raw["User"].astype(
                        str).fillna("").str.strip().unique() if u)

                # Filter to selected user (optional)
                if selected_user:
                    df = df[df["User"].astype(str).str.strip(
                    ).str.lower() == selected_user.lower()]

                # Monthly aggregate
                if not df.empty and selected_user:
                    df["_month"] = df["End"].dt.month
                    g = (
                        df.groupby("_month", dropna=True)
                        .agg(
                            jobs=("JobID", "count"),
                            CPU_Core_Hours=("CPU_Core_Hours", "sum"),
                            GPU_Hours=("GPU_Hours", "sum"),
                            Mem_GB_Hours_Used=("Mem_GB_Hours_Used", "sum"),
                            Cost=("Cost (฿)", "sum"),
                        )
                        .reset_index()
                        .sort_values("_month")
                    )
                    g.rename(columns={"Cost": "Cost (฿)",
                             "_month": "month"}, inplace=True)
                    monthly_agg = g[["month", "jobs", "CPU_Core_Hours", "GPU_Hours",
                                     "Mem_GB_Hours_Used", "Cost (฿)"]].to_dict("records")
                    year_total = float(g["Cost (฿)"].sum())

                    # Optional: month detail if ?month= supplied
                    if month:
                        try:
                            m = int(month)
                        except Exception:
                            m = None
                        if m and 1 <= m <= 12 and "End" in df.columns:
                            dmonth = df[df["End"].dt.month == m].copy()
                            cols = [
                                "JobID", "Elapsed", "End", "State",
                                "CPU_Core_Hours", "GPU_Count", "GPU_Hours",
                                "Memory_GB", "Mem_GB_Hours_Used", "Mem_GB_Hours_Alloc",
                                "tier", "Cost (฿)"
                            ]
                            for c in cols:
                                if c not in dmonth.columns:
                                    dmonth[c] = ""
                            month_detail_rows = dmonth[cols].to_dict("records")
                            # chips
                            tot_cpu_m = float(pd.to_numeric(dmonth.get(
                                "CPU_Core_Hours"), errors="coerce").fillna(0).sum())
                            tot_gpu_m = float(pd.to_numeric(dmonth.get(
                                "GPU_Hours"), errors="coerce").fillna(0).sum())
                            mem_col = "Mem_GB_Hours_Used" if "Mem_GB_Hours_Used" in dmonth.columns else "Mem_GB_Hours"
                            tot_mem_m = float(pd.to_numeric(dmonth.get(
                                mem_col), errors="coerce").fillna(0).sum())
                            month_total = float(pd.to_numeric(dmonth.get(
                                "Cost (฿)"), errors="coerce").fillna(0).sum())

        elif section == "myusage":
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username)

            if not df_raw.empty and "End" in df_raw.columns:
                end_series = pd.to_datetime(
                    df_raw["End"], errors="coerce", utc=True)
                cutoff_utc = _to_utc_day_end(end_d)
                df_raw = df_raw[end_series.notna() & (
                    end_series <= cutoff_utc)]
                df_raw["End"] = end_series
                raw_cols = list(df_raw.columns)
                raw_rows = df_raw.head(200).to_dict(orient="records")
            elif not df_raw.empty:
                raw_cols = list(df_raw.columns)
                raw_rows = df_raw.head(200).to_dict(orient="records")

            df = compute_costs(
                df_raw.copy() if df_raw is not None else pd.DataFrame())

            if view in {"detail", "aggregate"}:
                df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
                already = billed_job_ids()
                df = df[~df["JobKey"].isin(already)]

                cols = [
                    "JobID", "Elapsed", "End", "State",
                    "CPU_Core_Hours", "GPU_Count", "GPU_Hours",
                    "Memory_GB", "Mem_GB_Hours_Used", "Mem_GB_Hours_Alloc",
                    "tier", "Cost (฿)"
                ]
                for c in cols:
                    if c not in df.columns:
                        df[c] = ""
                rows = df[cols].to_dict(orient="records")

                if not df.empty:
                    agg = (
                        df.groupby(["tier"], dropna=False)
                        .agg(
                            jobs=("JobID", "count"),
                            CPU_Core_Hours=("CPU_Core_Hours", "sum"),
                            GPU_Hours=("GPU_Hours", "sum"),
                            Mem_GB_Hours_Used=("Mem_GB_Hours_Used", "sum"),
                            Cost=("Cost (฿)", "sum"),
                        ).reset_index()
                    )
                    agg.rename(columns={"Cost": "Cost (฿)"}, inplace=True)
                    agg_rows = agg[["tier", "jobs", "CPU_Core_Hours",
                                    "GPU_Hours", "Mem_GB_Hours_Used", "Cost (฿)"]].to_dict(orient="records")

                tot_cpu = float(_ensure_col(df, "CPU_Core_Hours", 0).sum())
                tot_gpu = float(_ensure_col(df, "GPU_Hours", 0).sum())
                tot_mem = float(_ensure_col(df, "Mem_GB_Hours_Used", 0).sum())
                tot_elapsed = float(_ensure_col(df, "Elapsed_Hours", 0).sum())
                grand_total = float(_ensure_col(df, "Cost (฿)", 0).sum())

        elif section == "billing":
            # Invoices only (trend moved to usage)
            pending = admin_list_receipts(status="pending") or []
            paid = admin_list_receipts(status="paid") or []

        elif section == "tiers":
            notes = []
            # 1) DB users
            with session_scope() as s:
                db_users = [u[0] for u in s.query(User.username).all()]

            # 2) Existing overrides
            ov = load_overrides_dict() or {}

            # 3) Users observed in jobs (last N days; default 365)
            try:
                lookback_days = int(request.args.get(
                    "tiers_lookback_days", 365))
            except Exception:
                lookback_days = 365
            jobs_start = (date.fromisoformat(before) -
                          timedelta(days=lookback_days)).isoformat()
            jobs_end = before

            job_users: list[str] = []
            try:
                df_jobs, _, _ = fetch_jobs_with_fallbacks(jobs_start, jobs_end)
                if not df_jobs.empty and "User" in df_jobs.columns:
                    job_users = [
                        u for u in df_jobs["User"].astype(str).fillna("").str.strip().unique().tolist()
                        if u
                    ]
            except Exception as e:
                notes.append(f"tiers.jobs: {e}")

            try:
                rcpt_users = [r["username"] for r in admin_list_receipts()]
            except Exception:
                rcpt_users = []

            def idx(lst):
                return {(s or "").strip().lower(): s for s in lst if (s or "").strip()}

            db_map = idx(db_users)
            job_map = idx(job_users)
            ov_map = idx(list(ov.keys()))
            keys = set(db_map) | set(job_map) | set(ov_map)
            usernames = sorted((db_map.get(k) or job_map.get(
                k) or ov_map.get(k) or k) for k in keys)

            def current_tier_for(u: str) -> str:
                t = ov.get(u.strip().lower())
                return t if t else classify_user_type(u)

            tier_rows = [
                {"username": u, "tier": current_tier_for(
                    u), "overridden": (u.strip().lower() in ov)}
                for u in usernames
            ]

            return render_template(
                "admin/tiers.html",
                current_user=current_user,
                rows=tier_rows,
                notes=notes,
                tiers=["mu", "gov", "private"],
                url_for=url_for,
                section="tiers",
            )

    except Exception as e:
        notes.append(str(e))

    tax_enabled, tax_label, tax_rate, tax_inclusive = _tax_cfg()
    TAX_UI = {
        "enabled": bool(tax_enabled and (tax_rate or 0) > 0),
        "label": tax_label,
        "rate": float(tax_rate or 0),
        "inclusive": bool(tax_inclusive),
    }
    return render_template(
        "admin/page.html",
        section=section,
        all_rates=rates,
        current=rates.get(tier, {"cpu": 0, "gpu": 0, "mem": 0}),
        tier=tier,
        tiers=["mu", "gov", "private"],
        current_user=current_user,
        start=start_d, end=end_d, view=view, before=before,
        rows=rows, agg_rows=agg_rows, grand_total=grand_total,
        data_source=data_source, notes=notes,
        tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
        pending=pending, paid=paid,
        my_pending_receipts=my_pending_receipts,
        my_paid_receipts=my_paid_receipts,
        sum_pending=sum_pending,
        sum_paid=sum_paid,
        raw_cols=raw_cols, raw_rows=raw_rows, header_classes=header_classes,
        url_for=url_for, q=q_user, all_users=all_users,
        # usage trend context
        selected_user=selected_user,
        year=year,
        current_year=current_year,
        month=month,
        monthly_agg=monthly_agg,
        month_detail_rows=month_detail_rows,
        year_total=year_total,
        tot_cpu_m=tot_cpu_m,
        tot_gpu_m=tot_gpu_m,
        tot_mem_m=tot_mem_m,
        month_total=month_total,
        TAX_UI=TAX_UI,
    )


@admin_bp.post("/admin")
@login_required
@admin_required
def admin_update():
    # Update rates and stay on the rates section
    tier = (request.form.get("type") or "").lower()
    try:
        cpu = float(request.form.get("cpu", "0"))
        gpu = float(request.form.get("gpu", "0"))
        mem = float(request.form.get("mem", "0"))
    except Exception:
        return redirect(url_for("admin.admin_form", section="rates", type=tier or "mu"))

    if tier not in {"mu", "gov", "private"}:
        return redirect(url_for("admin.admin_form", section="rates"))

    if min(cpu, gpu, mem) < 0:
        return redirect(url_for("admin.admin_form", section="rates", type=tier))

    r = rates_store.load_rates()
    r[tier] = {"cpu": cpu, "gpu": gpu, "mem": mem}
    save_rates(r)
    audit(
        "rate.update",
        target_type="tier", target_id=tier,
        outcome="success", status=200,
        extra={"new": {"cpu": cpu, "gpu": gpu, "mem": mem}}
    )
    return redirect(url_for("admin.admin_form", section="rates", type=tier))


@admin_bp.post("/admin/receipts/<int:rid>/paid")
@login_required
@fresh_login_required
@admin_required
def mark_paid(rid: int):
    ok = mark_receipt_paid(rid, current_user.username)
    if ok:
        RECEIPT_MARKED_PAID.labels(actor_type="admin").inc()
        try:
            gl_ok = post_receipt_paid(rid, current_user.username)
            audit("gl.payment.result", target_type="receipt", target_id=str(rid),
                  status=200 if gl_ok else 409, outcome="success" if gl_ok else "blocked",
                  extra={"reason": None if gl_ok else "period_closed"})
        except Exception as e:
            audit("gl.payment.result", target_type="receipt", target_id=str(rid),
                  status=500, outcome="failure", extra={"reason": str(e)[:200]})

    audit("invoice.mark_paid", target_type="receipt", target_id=str(rid),
          outcome="success" if ok else "failure", status=200 if ok else 404,
          extra={"reason": "manual_mark_paid"})
    return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))


@admin_bp.post("/admin/receipts/<int:rid>/revert")
@login_required
@fresh_login_required
@admin_required
def revert_paid(rid: int):
    reason = (request.form.get("reason") or "").strip() or None
    ok, msg = revert_receipt_to_pending(rid, current_user.username, reason)
    if ok:
        try:
            reverse_receipt_postings(
                rid, current_user.username, kinds=("payment",))
        except Exception:
            pass
    audit("invoice.revert", target_type="receipt", target_id=str(rid),
          outcome="success" if ok else "failure", status=200 if ok else 400,
          error_code=None if ok else (msg or "revert_failed"),
          extra={"reason": reason})
    return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))


@admin_bp.get("/admin/paid.csv")
@login_required
@admin_required
def paid_csv():
    fname, csv_text = paid_receipts_csv()
    CSV_DOWNLOADS.labels(kind="admin_paid").inc()
    audit("export.paid_csv", target_type="scope",
          target_id="admin", outcome="success", status=200)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@admin_bp.get("/admin/my.csv")
@login_required
@admin_required
def my_usage_csv_admin():
    before = request.args.get("before") or date.today().isoformat()
    start_d, end_d = "1970-01-01", before
    df, _, _ = fetch_jobs_with_fallbacks(
        start_d, end_d, username=current_user.username)
    df = compute_costs(df)

    out = io.StringIO()
    df.to_csv(out, index=False)
    out.seek(0)
    filename = f"usage_{current_user.username}_{start_d}_{end_d}.csv"
    CSV_DOWNLOADS.labels(kind="my_usage").inc()
    audit("export.my_usage_csv", target_type="user",
          target_id=current_user.username, outcome="success", status=200)
    return Response(
        out.read(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@admin_bp.post("/admin/my/receipt")
@login_required
@admin_required
def create_self_receipt():
    before = request.form.get("before") or date.today().isoformat()
    start_d, end_d = "1970-01-01", before

    df, _, _ = fetch_jobs_with_fallbacks(
        start_d, end_d, username=current_user.username)
    df = compute_costs(df)

    if "End" in df.columns:
        end_series = pd.to_datetime(df["End"], errors="coerce", utc=True)
        cutoff_utc = _to_utc_day_end(end_d)
        df = df[end_series.notna() & (end_series <= cutoff_utc)]
        df["End"] = end_series

    df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
    df = df[~df["JobKey"].isin(billed_job_ids())]

    if df.empty:
        return redirect(url_for("admin.admin_form", section="myusage", before=before, view="detail"))

    rid, total, _ = create_receipt_from_rows(
        current_user.username, start_d, end_d, df.to_dict(orient="records"))
    RECEIPT_CREATED.labels(scope="admin").inc()

    # NEW: post issuance to GL (idempotent)
    try:
        post_receipt_issued(rid, current_user.username)
    except Exception:
        pass

    return redirect(url_for("admin.admin_form", section="myusage", before=before, view="detail"))


@admin_bp.get("/admin/audit")
@login_required
@admin_required
def audit_page():
    rows = list_audit(limit=100)
    return render_template(
        "admin/audit.html",
        rows=rows,
        section="audit",
        bview=None,
        before=date.today().isoformat(),
    )


@admin_bp.get("/admin/audit.csv")
@login_required
@admin_required
def audit_csv():
    fname, csv_text = export_csv()
    CSV_DOWNLOADS.labels(kind="audit").inc()
    audit("export.audit_csv", target_type="scope",
          target_id="admin", outcome="success", status=200)
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@admin_bp.get("/admin/simulate_rates.json")
@login_required
@admin_required
def simulate_rates_json():
    """
    Read-only: fetch last 90 days, compute usage with compute_costs(),
    build components, then simulate current vs candidate rates.
    Query params (optional): cpu_mu, gpu_mu, mem_mu, cpu_gov, ... cpu_private, ...
    """
    try:
        before = (request.args.get("before")
                  or date.today().isoformat()).strip()
        start_d = (date.fromisoformat(before) - timedelta(days=90)).isoformat()
        end_d = before

        raw_df, data_source, _ = fetch_jobs_with_fallbacks(start_d, end_d)
        costed = compute_costs(raw_df)

        if "End" in costed.columns:
            end_series = pd.to_datetime(
                costed["End"], errors="coerce", utc=True)
            cutoff_utc = _to_utc_day_end(end_d)
            costed = costed[end_series.notna() & (
                end_series <= cutoff_utc)].copy()
            costed["End"] = end_series

        comps = build_pricing_components(costed)

        current_rates = rates_store.load_rates()

        def pull(tier: str, key: str, default: float) -> float:
            return float(request.args.get(f"{key}_{tier}", default))

        tiers = ("mu", "gov", "private")
        candidate = {}
        for t in tiers:
            base = current_rates.get(t, {"cpu": 0.0, "gpu": 0.0, "mem": 0.0})
            candidate[t] = {
                "cpu": pull(t, "cpu", base["cpu"]),
                "gpu": pull(t, "gpu", base["gpu"]),
                "mem": pull(t, "mem", base["mem"]),
            }

        out = simulate_vs_current(comps, current_rates, candidate)
        out["data_source"] = data_source or "unknown"
        out["window"] = {"start": start_d, "end": end_d}
        out["rates"] = {"current": current_rates, "candidate": candidate}

        return jsonify(out), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@admin_bp.get("/admin/ledger")
@login_required
@admin_required
def ledger_page():
    """
    Accounting overview + safety signals for reversals.
    Adds a 'Paid Receipts (window)' table with flags that gate safe 'revert to pending':
      - has_external_payment (true if any succeeded Payment with provider != internal_admin)
      - exported_to_gl_at (optional future field on Receipt; shows if present)
      - etax_submitted_at / etax_status (optional future field(s) on Receipt; shows if present)
      - customer_sent_at (optional future field on Receipt; shows if present)
      - eligible_to_revert (derived; true iff no external payment and none of the above flags set)
    """
    from datetime import datetime, timezone, timedelta
    from models.schema import Receipt, Payment
    from sqlalchemy import select, and_
    from calendar import monthrange

    before = (request.args.get("before") or date.today().isoformat()).strip()
    end_d = before
    start_d = (date.fromisoformat(before) - timedelta(days=365)).isoformat()

    # allow overrides via query
    start_q = (request.args.get("start") or start_d).strip()
    end_q = (request.args.get("end") or end_d).strip()

    # ---- existing derived accounting artifacts (unchanged) ----
    mode = (request.args.get("mode") or "posted").strip().lower()
    if mode == "derived":
        j = derive_journal(start_q, end_q)  # preview, ignores locks
    else:
        # authoritative, respects locks
        j = _load_posted_journal(start_q, end_q)
    tb = trial_balance(j)
    pnl = income_statement(j)
    bs = balance_sheet(j)

    # ---- new: paid receipts within window + safety flags ----
    def _to_utc_start(diso: str) -> datetime:
        # naive date -> UTC 00:00:00
        return datetime.fromisoformat(diso).replace(tzinfo=timezone.utc)

    def _to_utc_end_inclusive(diso: str) -> datetime:
        # naive date -> UTC 23:59:59
        return datetime.fromisoformat(diso).replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59, seconds=59)

    start_utc = _to_utc_start(start_q)
    end_utc = _to_utc_end_inclusive(end_q)

    from sqlalchemy import func
    from models.gl import JournalBatch, GLEntry, ExportRun

    export_stats = {"batches": 0, "lines": 0, "last_run": None}
    with session_scope() as s:
        # count unexported, posted batches that have lines in the window
        q_common = (
            select(GLEntry.batch_id)
            .join(JournalBatch, GLEntry.batch_id == JournalBatch.id)
            .where(
                JournalBatch.exported_at.is_(None),
                JournalBatch.kind.in_(["accrual", "issue", "payment"]),
                GLEntry.date >= start_utc,
                GLEntry.date <= end_utc,
            )
        )
        export_stats["batches"] = int(
            s.scalar(select(func.count(func.distinct(q_common.c.batch_id)))) or 0
        )
        export_stats["lines"] = int(
            s.scalar(
                select(func.count(GLEntry.id)).where(
                    GLEntry.batch_id.in_(q_common)
                )
            ) or 0
        )

        export_stats["last_run"] = s.execute(
            select(ExportRun).order_by(ExportRun.id.desc()).limit(1)
        ).scalar_one_or_none()

    paid_receipts_window: list[dict] = []
    kpi_total_paid = 0.0
    kpi_count_paid = 0
    kpi_count_eligible = 0

    with session_scope() as s:
        # Pull paid receipts in window (by paid_at)
        rows = (
            s.query(Receipt)
            .filter(
                Receipt.status == "paid",
                Receipt.paid_at.isnot(None),
                Receipt.paid_at >= start_utc,
                Receipt.paid_at <= end_utc,
            )
            .order_by(Receipt.paid_at.desc(), Receipt.id.desc())
            .all()
        )

        for r in rows:
            # any non-internal succeeded payment?
            has_external_payment = s.execute(
                select(Payment.id).where(
                    and_(
                        Payment.receipt_id == r.id,
                        Payment.status == "succeeded",
                        Payment.provider != "internal_admin",
                    )
                ).limit(1)
            ).first() is not None

            # Optional future flags on Receipt (display if your schema later adds them)
            exported_to_gl_at = getattr(r, "exported_to_gl_at", None)
            etax_submitted_at = getattr(r, "etax_submitted_at", None)
            etax_status = getattr(r, "etax_status", None)
            customer_sent_at = getattr(r, "customer_sent_at", None)

            # Eligibility is conservative: only if no external money AND no downstream lock flags
            eligible_to_revert = (
                (not has_external_payment)
                and (exported_to_gl_at is None)
                and (etax_submitted_at is None)
                and (customer_sent_at is None)
                and (str(etax_status or "").lower() in {"", "draft", "none"})
            )

            kpi_total_paid += float(r.total or 0)
            kpi_count_paid += 1
            if eligible_to_revert:
                kpi_count_eligible += 1

            paid_receipts_window.append({
                "id": r.id,
                "username": r.username,
                "invoice_no": r.invoice_no,
                "total": float(r.total or 0),
                "paid_at": r.paid_at,
                "method": r.method,
                "tx_ref": r.tx_ref,

                # safety signals
                "has_external_payment": bool(has_external_payment),
                "exported_to_gl_at": exported_to_gl_at,
                "etax_submitted_at": etax_submitted_at,
                "etax_status": etax_status,
                "customer_sent_at": customer_sent_at,
                "eligible_to_revert": bool(eligible_to_revert),
            })

    # meta for the trial balance card (unchanged)
    tb_meta = {
        "sum_debits": tb.attrs.get("sum_debits", 0.0),
        "sum_credits": tb.attrs.get("sum_credits", 0.0),
        "out_of_balance": tb.attrs.get("out_of_balance", 0.0),
    }

    kpis = {
        "count_paid": int(kpi_count_paid),
        "sum_paid": float(kpi_total_paid),
        "count_eligible": int(kpi_count_eligible),
    }
    today_d = date.today()
    this_month_start = date(today_d.year, today_d.month, 1).isoformat()
    this_month_end = today_d.isoformat()
    prev_y = (today_d.year if today_d.month > 1 else today_d.year - 1)
    prev_m = (today_d.month - 1) if today_d.month > 1 else 12
    prev_last = monthrange(prev_y, prev_m)[1]
    last_month_start = date(prev_y, prev_m, 1).isoformat()
    last_month_end = date(prev_y, prev_m, prev_last).isoformat()
    ytd_start = date(today_d.year, 1, 1).isoformat()

    sel = date.fromisoformat(end_q)
    y, m = sel.year, sel.month
    status = _period_status(y, m)

    return render_template(
        "admin/ledger.html",
        start=start_q, end=end_q,
        journal=j.to_dict(orient="records"),
        tb=tb.to_dict(orient="records"),
        tb_meta=tb_meta,
        pnl=pnl.to_dict(orient="records")[0] if not pnl.empty else {
            "Revenue": 0, "Expenses": 0, "Net_Income": 0},
        bs=bs.to_dict(orient="records")[0] if not bs.empty else {
            "Assets": 0, "Liabilities": 0, "Equity_Including_PnL": 0, "Check(Assets - L-E)": 0
        },
        today=date.today().isoformat(),

        # NEW context
        kpis=kpis,
        paid_receipts=paid_receipts_window,
        this_month_start=this_month_start, this_month_end=this_month_end,
        last_month_start=last_month_start, last_month_end=last_month_end,
        ytd_start=ytd_start,
        period_year=y, period_month=m, period_status=(status or "open"),
        export_stats=export_stats,
    )


@admin_bp.get("/admin/ledger.csv")
@login_required
@admin_required
def ledger_csv():
    before = (request.args.get("before") or date.today().isoformat()).strip()
    end_d = before
    start_d = (date.fromisoformat(before) - timedelta(days=90)).isoformat()

    start_q = (request.args.get("start") or start_d).strip()
    end_q = (request.args.get("end") or end_d).strip()

    j = derive_journal(start_q, end_q)
    out = io.StringIO()
    j.to_csv(out, index=False)
    out.seek(0)
    fname = f"journal_{start_q}_{end_q}.csv"
    return Response(out.read(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# --- Export endpoints (CSV / Xero) ---
@admin_bp.get("/admin/export/ledger.csv")
@login_required
@admin_required
def export_ledger_csv():
    start = (request.args.get("start") or "1970-01-01").strip()
    end = (request.args.get("end") or date.today().isoformat()).strip()
    # Posted GL (respects locks)
    df = _load_posted_journal(start, end)
    out = io.StringIO()
    (df if not df.empty else pd.DataFrame(
        columns=["date", "ref", "memo", "account_id",
                 "account_name", "account_type", "debit", "credit"]
    )).to_csv(out, index=False)
    out.seek(0)
    return Response(
        out.read(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename=posted_general_ledger_{start}_to_{end}.csv'},
    )


@admin_bp.get("/admin/export/xero_bank.csv")
@login_required
@admin_required
def export_xero_bank_csv():
    from services.accounting_export import build_xero_bank_csv
    start = (request.args.get("start") or "1970-01-01").strip()
    end = (request.args.get("end") or date.today().isoformat()).strip()
    fname, csv_text = build_xero_bank_csv(start, end)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@admin_bp.get("/admin/export/xero_sales.csv")
@login_required
@admin_required
def export_xero_sales_csv():
    from services.accounting_export import build_xero_sales_csv
    start = (request.args.get("start") or "1970-01-01").strip()
    end = (request.args.get("end") or date.today().isoformat()).strip()
    fname, csv_text = build_xero_sales_csv(start, end)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@admin_bp.post("/admin/tiers")
@login_required
@fresh_login_required
@admin_required
def save_tiers():
    from models.tiers_store import upsert_override, clear_override, load_overrides_dict
    from services.billing import classify_user_type

    changed = 0
    removed = 0

    existing = {k.strip().lower(): v for k, v in (
        load_overrides_dict() or {}).items()}

    for k, v in request.form.items():
        if not k.startswith("tier_"):
            continue
        username_raw = k[len("tier_"):]
        username = username_raw.strip()
        desired = (v or "").strip().lower()
        if desired not in {"mu", "gov", "private"}:
            audit(
                "tier.override.invalid",
                target_type="user", target_id=username,
                outcome="failure", status=400,
                error_code="invalid_tier",
                extra={"desired": desired}
            )
            continue

        natural = classify_user_type(username)
        prev_override = existing.get(username.lower())
        prev_effective = prev_override if prev_override else natural

        if desired == natural:
            if prev_override is not None:
                clear_override(username)
                removed += 1
                audit(
                    "tier.override.clear",
                    target_type="user", target_id=username,
                    outcome="success", status=200,
                    extra={"from": prev_effective,
                           "to": natural, "natural": natural}
                )
        else:
            upsert_override(username, desired)
            changed += 1
            audit(
                "tier.override.set",
                target_type="user", target_id=username,
                outcome="success", status=200,
                extra={"from": prev_effective,
                       "to": desired, "natural": natural}
            )

    audit(
        "tier.override.summary",
        target_type="summary", target_id="tiers",
        outcome="success", status=200,
        extra={"changed": int(changed), "removed": int(removed)}
    )
    return redirect(url_for("admin.admin_form", section="tiers"))


@admin_bp.get("/admin/forecast.json")
@login_required
@admin_required
def forecast_json():
    """
    Returns a multi-horizon forecast for a dashboard metric.
    Query:
      ?metric=cost|jobs|cpu|gpu|mem   (default: cost)
      ?before=YYYY-MM-DD              (default: today)
      ?train_days=180                 (default: 180)
    """
    try:
        metric = (request.args.get("metric") or "cost").strip().lower()
        before = (request.args.get("before")
                  or date.today().isoformat()).strip()
        train_days = int(request.args.get("train_days") or 180)
    except Exception:
        return jsonify({"error": "bad parameters"}), 400

    start_d = (date.fromisoformat(before) -
               timedelta(days=train_days-1)).isoformat()
    raw_df, _, _ = fetch_jobs_with_fallbacks(start_d, before)
    costed = compute_costs(raw_df)

    if "End" in costed.columns:
        end_series = pd.to_datetime(costed["End"], errors="coerce", utc=True)
        cutoff_utc = _to_utc_day_end(before)
        costed = costed[end_series.notna() & (end_series <= cutoff_utc)].copy()
        costed["End"] = end_series

    daily = build_daily_series(
        costed, metric=metric, end_date=before, train_days=train_days)
    if daily.empty:
        return jsonify({"metric": metric, "history": {"labels": [], "values": []}, "forecasts": {}}), 200

    f = multi_horizon_forecast(daily, horizons=(30, 60, 90))
    return jsonify({
        "metric": metric,
        "history": {"labels": f.history_labels, "values": f.history_values},
        "forecasts": {str(k): v for k, v in f.horizons.items()},
    }), 200


@admin_bp.post("/admin/invoices/create_month")
@login_required
@fresh_login_required
@admin_required
def create_month_invoices():
    try:
        y = int((request.form.get("year") or "").strip())
        m = int((request.form.get("month") or "").strip())
        if not (2000 <= y <= 2100 and 1 <= m <= 12):
            raise ValueError("Invalid year/month")
    except Exception:
        return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))

    start_d = date(y, m, 1).isoformat()
    last_day = monthrange(y, m)[1]
    end_d = date(y, m, last_day).isoformat()

    try:
        df_raw, _, _ = fetch_jobs_with_fallbacks(start_d, end_d)
        df = compute_costs(df_raw)

        if "End" in df.columns:
            end_series = pd.to_datetime(df["End"], errors="coerce", utc=True)
            lo = pd.Timestamp(start_d, tz="UTC")
            hi = pd.Timestamp(end_d, tz="UTC") + \
                pd.Timedelta(hours=23, minutes=59, seconds=59)
            df = df[end_series.notna() & (end_series >= lo) &
                    (end_series <= hi)].copy()
            df["End"] = end_series

        if not df.empty:
            df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
            already = set(billed_job_ids())
            df = df[~df["JobKey"].isin(already)]

        if df.empty:
            return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))

        created = skipped = failed = 0
        # 🚧 group safely; skip blank/NaN usernames
        for raw_user, duser in df.groupby(df["User"].astype(str)):
            user_name = (raw_user or "").strip()
            if not user_name or user_name.lower() in {"nan", "none"}:
                audit("invoice.create_month.skip_user",
                      target_type="user", target_id=(user_name or "blank"),
                      outcome="failure", status=400,
                      extra={"reason": "empty_or_nan_username", "year": y, "month": m})
                skipped += 1
                continue

            try:
                existing = [
                    r for r in (list_receipts(user_name) or [])
                    if r.get("start") and r.get("end")
                    and getattr(r["start"], "year", None) == y and getattr(r["start"], "month", None) == m
                    and getattr(r["end"], "year", None) == y and getattr(r["end"], "month", None) == m
                    and r.get("status") in ("pending", "paid")
                ]
            except Exception:
                existing = []
            if existing:
                skipped += 1
                continue

            # 🔐 per-user try/except so one failure doesn't nuke the batch
            try:
                rid, total, _items = create_receipt_from_rows(
                    user_name, start_d, end_d,
                    duser.drop(columns=["JobKey"], errors="ignore").to_dict(
                        orient="records")
                )
                RECEIPT_CREATED.labels(scope="admin_bulk").inc()

                ok = False
                try:
                    ok = post_receipt_issued(rid, current_user.username)
                    audit("invoice.create_month.summary", target_type="month", target_id=f"{y}-{m:02d}",
                          status=200 if failed == 0 else 207, outcome="success" if failed == 0 else "partial",
                          extra={"count": {  # <-- 'count' is allowed by _ALLOWED_EXTRA_KEYS
                              "created": int(created),
                              "skipped": int(skipped),
                              "failed": int(failed),
                              "rid": rid,
                          }})
                except Exception:
                    pass
                audit("gl.post_issue",
                      target_type="receipt", target_id=str(rid),
                      outcome="success" if ok else "blocked",
                      status=200 if ok else 409,
                      extra={"reason": "period_closed" if not ok else "ok",
                             "year": y, "month": m})
                created += 1

            except Exception as e:
                failed += 1
                audit("invoice.create_month.user_failed",
                      target_type="user", target_id=user_name,
                      outcome="failure", status=500,
                      extra={"year": y, "month": m, "reason": str(e)[:256]})
                continue

        # final summary (treat partial as 207)
        audit(
            "invoice.create_month.summary",
            target_type="month", target_id=f"{y}-{m:02d}",
            outcome="success" if failed == 0 else "partial",
            status=200 if failed == 0 else 207,
            extra={"count": {  # <-- 'count' is allowed by _ALLOWED_EXTRA_KEYS
                "created": int(created),
                "skipped": int(skipped),
                "failed": int(failed),
            }}
        )

    except Exception as e:
        audit("invoice.create_month",
              target_type="month", target_id=f"{y}-{m:02d}",
              outcome="failure", status=500, error_code="create_month_error",
              extra={"reason": str(e)[:256]})

    return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))


@admin_bp.get("/admin/receipts/<int:rid>.pdf")
@login_required
@admin_required
def admin_receipt_pdf(rid: int):
    rec, items = get_receipt_with_items(rid)
    if not rec:
        audit(
            "invoice.pdf",
            target_type="receipt", target_id=str(rid),
            outcome="failure", status=404,
            error_code="not_found"
        )
        return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))

    html = render_template(
        "invoices/invoice.html",
        r=rec,
        rows=items,
        org=ORG_INFO(),
        DISPLAY_TZ=APP_TZ,
    )
    pdf = HTML(string=html, base_url=current_app.static_folder).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename=invoice_{rec["id"]}.pdf'
    audit("invoice.pdf", target_type="receipt", target_id=str(rid),
          outcome="success", status=200)
    return resp


@admin_bp.post("/admin/invoices/revert_month")
@login_required
@fresh_login_required
@admin_required
def bulk_revert_month_invoices():
    try:
        y = int((request.form.get("year") or "").strip())
        m = int((request.form.get("month") or "").strip())
        if not (2000 <= y <= 2100 and 1 <= m <= 12):
            raise ValueError("Invalid year/month")
    except Exception:
        return redirect(url_for("admin.admin_form", section="billing"))

    reason = (request.form.get("reason")
              or "").strip() or "bulk revert pending"

    try:
        voided, skipped, ids = bulk_void_pending_invoices_for_month(
            y, m, current_user.username, reason)
        for rid in ids:
            try:
                reverse_receipt_postings(
                    rid, current_user.username, kinds=("issue",))
            except Exception:
                pass

        audit("invoice.bulk_revert_month", target_type="month", target_id=f"{y}-{m:02d}",
              outcome="success" if voided > 0 else "failure", status=200,
              extra={"voided": int(voided), "skipped": int(skipped), "reason": reason})
    except Exception as e:
        audit("invoice.bulk_revert_month", target_type="month", target_id=f"{y}-{m:02d}",
              outcome="failure", status=500, error_code="exception", extra={"reason": reason})
    return redirect(url_for("admin.admin_form", section="billing"))


@admin_bp.get("/admin/receipts/<int:rid>.etax.json")
@login_required
@admin_required
def admin_receipt_etax_json(rid: int):
    from models.billing_store import build_etax_payload
    payload = build_etax_payload(rid)
    if not payload:
        return jsonify({"error": "not_found"}), 404
    return jsonify(payload), 200


@admin_bp.get("/admin/receipts/<int:rid>.etax.zip")
@login_required
@admin_required
def admin_receipt_etax_zip(rid: int):
    from io import BytesIO
    from zipfile import ZipFile, ZIP_DEFLATED
    from weasyprint import HTML
    from models.billing_store import get_receipt_with_items, build_etax_payload
    from services.org_info import ORG_INFO
    from flask import current_app, make_response

    rec, items = get_receipt_with_items(rid)
    if not rec:
        return jsonify({"error": "not_found"}), 404

    # 1) JSON payload
    payload = build_etax_payload(rid)

    # 2) PDF (current layout)
    html = render_template("invoices/invoice.html",
                           r=rec, rows=items, org=ORG_INFO(), DISPLAY_TZ=APP_TZ)
    pdf_bytes = HTML(
        string=html, base_url=current_app.static_folder).write_pdf()

    # 3) ZIP it
    mem = BytesIO()
    with ZipFile(mem, "w", ZIP_DEFLATED) as z:
        inv_no = payload["document"]["number"]
        z.writestr(f"{inv_no}.json", json.dumps(
            payload, ensure_ascii=False, indent=2))
        z.writestr(f"{inv_no}.pdf", pdf_bytes)
        # optional: include a README with what’s inside
        z.writestr(
            "README.txt", "Unsigned export. The other team will transform/sign/submit.")
    mem.seek(0)

    resp = make_response(mem.read())
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Disposition"] = f'attachment; filename=etax_export_{rid}.zip'
    return resp


@admin_bp.get("/admin/receipts/<int:rid>.th.pdf")
@login_required
@admin_required
def admin_receipt_pdf_th(rid: int):
    rec, items = get_receipt_with_items(rid)
    if not rec:
        return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))
    html = render_template("invoices/invoice_th.html",
                           r=rec, rows=items, org=ORG_INFO_TH(), DISPLAY_TZ=APP_TZ)
    pdf = HTML(string=html, base_url=current_app.static_folder).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename=invoice_{rec["id"]}_th.pdf'
    audit("invoice.pdf_th", target_type="receipt", target_id=str(rid),
          outcome="success", status=200)
    return resp


@admin_bp.get("/admin/audit.verify.json")
@login_required
@admin_required
def audit_verify_json():
    from models.audit_store import verify_chain
    try:
        # optional ?limit= param for quick checks
        limit = request.args.get("limit", type=int)
        result = verify_chain(limit=limit)
        status = 200 if result.get("ok") else 409
        audit(
            "audit.verify_chain",
            target_type="scope", target_id="admin",
            outcome="success" if result.get("ok") else "failure",
            status=200,
            extra={
                "checked": int(result.get("checked", 0)),
                "last_ok_id": result.get("last_ok_id"),
                "first_bad_id": result.get("first_bad_id"),
                "reason": result.get("reason"),
            }
        )
        return jsonify(result), status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@admin_bp.post("/admin/periods/<int:year>-<int:month>/close")
@login_required
@fresh_login_required
@admin_required
def close_period_endpoint(year: int, month: int):
    ok = close_period(year, month, current_user.username)
    return redirect(url_for("admin.ledger_page"))


@admin_bp.post("/admin/periods/<int:year>-<int:month>/reopen")
@login_required
@fresh_login_required
@admin_required
def reopen_period_endpoint(year: int, month: int):
    ok = reopen_period(year, month, current_user.username)
    return redirect(url_for("admin.ledger_page"))


def _period_status(y: int, m: int) -> str | None:
    with session_scope() as s:
        return s.execute(
            select(AccountingPeriod.status).where(
                AccountingPeriod.year == y, AccountingPeriod.month == m
            )
        ).scalar_one_or_none()


@admin_bp.post("/admin/periods/<int:year>/<int:month>/close")
@login_required
@fresh_login_required
@admin_required
def ui_close_period(year, month):
    from calendar import monthrange
    from datetime import datetime, timezone
    from sqlalchemy import select, func
    from models.schema import Receipt
    from models.gl import JournalBatch, GLEntry

    actor = getattr(request, "user", None) and request.user.username or "admin"

    # 1) Run accruals for the period (idempotent)
    try:
        _created = post_service_accruals_for_period(year, month, actor)
    except Exception as e:
        audit("period.close", target_type="period", target_id=f"{year}-{month:02d}",
              outcome="failure", status=500, error_code="accrual_run_error",
              extra={"reason": str(e)[:256]})
        flash(f"Close blocked: failed to run accruals — {e}", "error")
        return redirect(request.referrer or url_for("admin.ledger_page"))

    # 2) Verify coverage: every *non-zero-net* receipt with service END in (y,m)
    #    has an accrual batch. Zero-net receipts do not require accruals.
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    last = datetime(year, month, monthrange(year, month)
                    [1], 23, 59, 59, tzinfo=timezone.utc)

    missing_ids = []
    with session_scope() as s:
        # All candidate receipts whose service period ends in y-m and have non-zero net.
        # If you also track a status, you can AND an "active" status filter here.
        candidate_ids = s.execute(
            select(Receipt.id).where(
                Receipt.end >= first,
                Receipt.end <= last,
                # mirror post_service_accrual_for_receipt(): skip zero/negative gross
                func.coalesce(Receipt.total, 0) > 0
                # , Receipt.status.in_(("open","issued"))   # (optional)
            )
        ).scalars().all()

        if candidate_ids:
            # receipts that actually have an accrual batch in that period
            accrued_ids = s.execute(
                select(GLEntry.receipt_id)
                .join(JournalBatch, GLEntry.batch_id == JournalBatch.id)
                .where(
                    JournalBatch.kind == "accrual",
                    JournalBatch.period_year == year,
                    JournalBatch.period_month == month,
                    GLEntry.receipt_id.isnot(None),
                )
            ).scalars().all()

            missing_ids = sorted(set(candidate_ids) - set(accrued_ids))

    if missing_ids:
        audit("period.close_blocked", target_type="period", target_id=f"{year}-{month:02d}",
              outcome="failure", status=409, error_code="missing_accruals",
              extra={"missing_count": len(missing_ids), "sample": missing_ids[:20]})
        flash(
            f"Close blocked: {len(missing_ids)} receipt(s) in {year}-{month:02d} missing accrual postings.", "error")
        # bounce back to ledger set to that month for quick inspection
        return redirect(url_for("admin.ledger_page",
                                start=f"{year:04d}-{month:02d}-01",
                                end=f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"))

    # 3) All good → close the period
    ok = close_period(year, month, actor)
    audit("period.close", target_type="period", target_id=f"{year}-{month:02d}",
          outcome="success" if ok else "failure", status=200 if ok else 409)
    return redirect(request.referrer or url_for("admin.ledger_page",
                                                start=f"{year:04d}-{month:02d}-01",
                                                end=f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"))


@admin_bp.post("/admin/periods/<int:year>/<int:month>/reopen")
@login_required
@fresh_login_required
@admin_required
def ui_reopen_period(year, month):
    actor = getattr(request, "user", None) and request.user.username or "admin"
    ok = reopen_period(year, month, actor)
    return redirect(request.referrer or url_for("admin.ledger_page"))


@admin_bp.post("/admin/periods/bootstrap")
@login_required
@fresh_login_required
@admin_required
def ui_bootstrap_periods():
    actor = getattr(request, "user", None) and request.user.username or "admin"
    n = bootstrap_periods(actor)
    return redirect(request.referrer or url_for("admin.ledger_page"))


@admin_bp.post("/admin/export/gl/formal.zip")
@login_required
@fresh_login_required
@admin_required
def export_gl_formal_zip():
    start = (request.form.get("start") or "1970-01-01").strip()
    end = (request.form.get("end") or date.today().isoformat()).strip()
    fname, blob = run_formal_gl_export(
        start, end, current_user.username, kind="posted_gl_csv")
    if not blob:
        flash("Nothing to export for the selected window.", "info")
        return redirect(url_for("admin.ledger_page", start=start, end=end))
    audit("export.formal.download", target_type="window", target_id=f"{start}:{end}",
          outcome="success", status=200, extra={"filename": fname})
    return Response(blob, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@admin_bp.get("/admin/export/runs")
@login_required
@admin_required
def list_export_runs():
    from sqlalchemy import select, desc
    from models.gl import ExportRun, ExportRunBatch, JournalBatch
    with session_scope() as s:
        runs = s.execute(select(ExportRun).order_by(
            desc(ExportRun.id)).limit(100)).scalars().all()
        rows = []
        for r in runs:
            rows.append({
                "id": r.id, "kind": r.kind, "status": r.status, "actor": r.actor,
                "criteria": r.criteria, "started_at": r.started_at, "finished_at": r.finished_at,
                "file_sha256": r.file_sha256, "file_size": r.file_size, "key_id": r.key_id
            })
    return render_template("admin/export_runs.html", rows=rows, section="exports")


@admin_bp.get("/admin/export/runs/<int:run_id>.zip")
@login_required
@admin_required
def redownload_export_run(run_id: int):
    """
    Re-download the EXACT same file (no re-selection; no double-export).
    """
    from sqlalchemy import select
    from models.gl import ExportRun, ExportRunBatch, GLEntry
    import io
    import zipfile
    import json
    with session_scope() as s:
        run = s.execute(select(ExportRun).where(
            ExportRun.id == run_id)).scalar_one_or_none()
        if not run:
            return jsonify({"error": "not_found"}), 404

        # rebuild from *the exact batches linked to this run*
        bids = [rb.batch_id for rb in s.execute(select(ExportRunBatch).where(
            ExportRunBatch.run_id == run_id)).scalars().all()]
        if not bids:
            return jsonify({"error": "run_empty"}), 404

        # deterministic order: (batch_id, seq_in_batch, id)
        lines = s.execute(
            select(
                GLEntry.date, GLEntry.ref, GLEntry.memo,
                GLEntry.account_id, GLEntry.account_name, GLEntry.account_type,
                GLEntry.debit, GLEntry.credit, GLEntry.batch_id, GLEntry.seq_in_batch,
                GLEntry.external_txn_id
            ).where(GLEntry.batch_id.in_(bids))
             .order_by(GLEntry.batch_id, GLEntry.seq_in_batch, GLEntry.id)
        ).all()

        import csv
        o = io.StringIO()
        w = csv.writer(o)
        w.writerow(["date", "ref", "memo", "account_id", "account_name", "account_type",
                   "debit", "credit", "batch_id", "line_seq", "external_txn_id"])
        for r in lines:
            w.writerow([
                r.date.date().isoformat(), r.ref, r.memo, r.account_id, r.account_name, r.account_type,
                float(r.debit or 0), float(
                    r.credit or 0), r.batch_id, int(r.seq_in_batch or 0),
                r.external_txn_id or f"B{r.batch_id:08d}-L{int(r.seq_in_batch or 0):05d}",
            ])
        csv_bytes = o.getvalue().encode("utf-8")

        # include the original evidence stored on the run
        manifest = {
            "run_id": run.id, "kind": run.kind, "criteria": run.criteria,
            "file_sha256": run.file_sha256, "manifest_sha256": run.manifest_sha256,
            "signature": run.signature, "key_id": run.key_id, "redownloaded_at": datetime.now(timezone.utc).isoformat()
        }

        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"gl_export_run_{run.id}.csv", csv_bytes)
            z.writestr(f"manifest_run_{run.id}.json", json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            z.writestr(
                f"signature_run_{run.id}.txt", f"key_id={run.key_id}\nsha256={run.file_sha256}\nsignature={run.signature}\n")
        mem.seek(0)
        return Response(mem.read(), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="gl_export_run_{run.id}_redownload.zip"'})


@admin_bp.get("/admin/export/ledger.pdf")
@login_required
@admin_required
def export_ledger_pdf():
    from flask import current_app
    from hashlib import sha256
    import secrets
    import socket
    import csv
    import io
    import json
    from datetime import datetime, timezone, date
    from weasyprint import HTML

    # local imports (pandas etc.)
    import pandas as pd

    # --- inputs / mode ---
    start = (request.args.get("start") or "1970-01-01").strip()
    end = (request.args.get("end") or date.today().isoformat()).strip()
    mode = (request.args.get("mode") or "posted").strip().lower()
    is_preview = (mode == "derived")

    # --- source data (respecting mode) ---
    if is_preview:
        df = derive_journal(start, end)
    else:
        df = _load_posted_journal(start, end)

    # empty guard (render a stub page so it still prints a memo)
    if df is None or df.empty:
        df = pd.DataFrame(columns=[
            "date", "ref", "memo", "account_id", "account_name",
            "account_type", "debit", "credit"
        ])

    # --- canonical sort ---
    df = df.sort_values(["date", "ref", "account_id"],
                        kind="mergesort").reset_index(drop=True)

    # numeric safety
    for col in ("debit", "credit"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col].fillna(0), errors="coerce").fillna(0.0)

    # --- canonical CSV bytes for hashing (mode/window included) ---
    csv_buf = io.StringIO()
    w = csv.writer(csv_buf)
    w.writerow(["date", "ref", "memo", "account_id",
                "account_name", "account_type", "debit", "credit"])
    for _, r in df.iterrows():
        w.writerow([
            r.get("date", ""), r.get("ref", ""), r.get("memo", ""),
            r.get("account_id", ""), r.get("account_name", ""),
            r.get("account_type", ""), float(r.get("debit") or 0.0),
            float(r.get("credit") or 0.0)
        ])
    csv_bytes = csv_buf.getvalue().encode("utf-8")

    # --- run metadata ---
    now = datetime.now(timezone.utc)
    run_id = f"GLPDF-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    criteria = {"start": start, "end": end,
                "mode": "derived" if is_preview else "posted"}
    doc_digest = sha256(csv_bytes + json.dumps(criteria,
                        sort_keys=True).encode("utf-8")).hexdigest()
    doc_digest_short = doc_digest[:12]

    # --- paginate: predictable rows per logical "page" (each becomes a physical page)
    # To keep one physical page per chunk, clamp row height (single line with ellipsis).
    ROWS_PER_PAGE = 34  # tune with your header/footer/watermark layout
    pages = []
    for i in range(0, len(df), ROWS_PER_PAGE):
        chunk = df.iloc[i:i+ROWS_PER_PAGE].copy()
        page_index = len(pages) + 1
        first_ref = str(chunk.iloc[0]["ref"]) if not chunk.empty else "-"
        last_ref = str(chunk.iloc[-1]["ref"]) if not chunk.empty else "-"
        page_hash = sha256(
            f"{run_id}|{page_index}|{doc_digest}|{first_ref}|{last_ref}".encode(
                "utf-8")
        ).hexdigest()[:12]
        pages.append({
            "index": page_index,
            "rows": chunk.to_dict(orient="records"),
            "first_ref": first_ref,
            "last_ref": last_ref,
            "hash": page_hash,
        })

    # add a final control line (0.00/0.00) at the end of the very last ledger table
    if pages:
        pages[-1]["rows"].append({
            "date": "",
            "ref": "",
            "memo": "— End of ledger —",
            "account_id": "",
            "account_name": "",
            "account_type": "",
            "debit": 0.00,
            "credit": 0.00
        })

    total_pages = max(1, len(pages))

    # --- build summary aggregates (for the final summary page) ---
    # Trial Balance by account (window totals)
    tb = (df.groupby(["account_id", "account_name", "account_type"], dropna=False)
            .agg(debit_sum=("debit", "sum"), credit_sum=("credit", "sum"))
            .reset_index())
    tb["debit_sum"] = tb["debit_sum"].round(2)
    tb["credit_sum"] = tb["credit_sum"].round(2)
    tb_total_dr = float(tb["debit_sum"].sum().round(2) if hasattr(
        tb["debit_sum"].sum(), "round") else round(tb["debit_sum"].sum(), 2))
    tb_total_cr = float(tb["credit_sum"].sum().round(2) if hasattr(
        tb["credit_sum"].sum(), "round") else round(tb["credit_sum"].sum(), 2))

    # P&L snapshot
    inc = tb[tb["account_type"] == "INCOME"]
    exp = tb[tb["account_type"] == "EXPENSE"]
    total_income = round(
        float(inc["credit_sum"].sum() - inc["debit_sum"].sum()), 2)
    total_expense = round(
        float(exp["debit_sum"].sum() - exp["credit_sum"].sum()), 2)
    net_income = round(total_income - total_expense, 2)
    pl = {
        "income": total_income,
        "expense": total_expense,
        "net_income": net_income
    }

    # Balance Sheet movement snapshot (window deltas)
    asg = tb[tb["account_type"] == "ASSET"]
    lbg = tb[tb["account_type"] == "LIABILITY"]
    eqg = tb[tb["account_type"] == "EQUITY"]
    assets_delta = round(
        float(asg["debit_sum"].sum() - asg["credit_sum"].sum()), 2)
    liab_delta = round(
        float(lbg["credit_sum"].sum() - lbg["debit_sum"].sum()), 2)
    equity_delta = round(
        float(eqg["credit_sum"].sum() - eqg["debit_sum"].sum()), 2)
    bs = {
        "assets_delta": assets_delta,
        "liabilities_delta": liab_delta,
        "equity_delta": equity_delta
    }

    # COA legend (only accounts present in window)
    coa_legend = (df[["account_id", "account_name", "account_type"]]
                  .drop_duplicates()
                  .sort_values(["account_type", "account_id"])
                  .to_dict(orient="records"))

    # --- optional persisted manifest (tamper-evidence) ---
    manifest = {
        "run_id": run_id,
        "mode": "derived" if is_preview else "posted",
        "window": {"start": start, "end": end},
        "doc_sha256": doc_digest,
        "total_lines": int(len(df)),
        "total_pages": int(total_pages),
        "page_hashes": [{"page": p["index"], "hash": p["hash"], "first_ref": p["first_ref"], "last_ref": p["last_ref"]} for p in pages],
        "generated_at": now.isoformat(),
        "host": socket.gethostname(),
        "generated_by": current_user.username,
    }
    audit("export.ledger_pdf", target_type="window", target_id=f"{start}:{end}",
          outcome="success", status=200,
          extra={"run_id": run_id, "mode": criteria["mode"], "doc_sha256": doc_digest})

    # --- render HTML -> PDF ---
    html = render_template(
        "admin/ledger_pdf.html",
        pages=pages,
        total_pages=total_pages,
        doc_digest=doc_digest,
        doc_digest_short=doc_digest_short,
        run_id=run_id,
        start=start, end=end,
        is_preview=is_preview,
        printed_on=now,
        printed_by=current_user.username,
        watermark_text=("PREVIEW ONLY" if is_preview else "CONFIDENTIAL"),
        # summary payload
        tb_rows=tb.to_dict(orient="records"),
        tb_total_dr=tb_total_dr,
        tb_total_cr=tb_total_cr,
        pl=pl,
        bs=bs,
        coa_legend=coa_legend,
    )
    pdf = HTML(string=html, base_url=current_app.static_folder).write_pdf()

    fname = f"general_ledger_{criteria['mode']}_{start}_to_{end}_{run_id}.pdf"
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@admin_bp.get("/admin/export/ledger_th.pdf")
@login_required
@admin_required
def export_ledger_th_pdf():
    from flask import current_app
    from hashlib import sha256
    import secrets
    import socket
    import csv
    import io
    import json
    from datetime import datetime, timezone, date
    from weasyprint import HTML
    import pandas as pd

    # --- inputs / mode ---
    start = (request.args.get("start") or "1970-01-01").strip()
    end = (request.args.get("end") or date.today().isoformat()).strip()
    mode = (request.args.get("mode") or "posted").strip().lower()
    is_preview = (mode == "derived")

    # --- source data (respecting mode) ---
    df = derive_journal(
        start, end) if is_preview else _load_posted_journal(start, end)
    if df is None or df.empty:
        df = pd.DataFrame(columns=["date", "ref", "memo", "account_id",
                          "account_name", "account_type", "debit", "credit"])

    # --- canonical sort ---
    df = df.sort_values(["date", "ref", "account_id"],
                        kind="mergesort").reset_index(drop=True)

    # numeric safety
    for col in ("debit", "credit"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col].fillna(0), errors="coerce").fillna(0.0)

    import re

    MEMO_TEMPLATES = {
        "REVENUE_RECOGNIZED": {
            "en": "Revenue recognized for {user} (service period)",
            "th": "บันทึกรายได้สำหรับ {user} (งวดให้บริการ)"
        },
        "RECEIPT_ISSUED": {
            "en": "Receipt issued for {user}",
            "th": "ออกใบรับสำหรับ {user}"
        },
        "RECEIPT_ISSUED_CA": {
            "en": "— service {period} < issue {issue}; recognize via Contract Asset",
            "th": "— บริการ {period} < ออก {issue}; รับรู้ผ่านสินทรัพย์ตามสัญญา"
        },
        "PERIOD_CLOSE_INCOME": {
            "en": "Close INCOME to Retained Earnings",
            "th": "ปิดบัญชีรายได้ไปยังกำไรสะสม"
        },
        "PERIOD_CLOSE_EXPENSE": {
            "en": "Close EXPENSE to Retained Earnings",
            "th": "ปิดบัญชีค่าใช้จ่ายไปยังกำไรสะสม"
        },
        "PERIOD_CLOSE": {
            "en": "Close to Retained Earnings",
            "th": "ปิดยอดไปยังกำไรสะสม"
        },
        "ECL_PROVISION": {
            "en": "ECL provision (contract assets) {period} — provision matrix",
            "th": "ตั้งค่าเผื่อ ECL (สินทรัพย์ตามสัญญา) {period} — เมทริกซ์การกันสำรอง"
        },
        "RECEIPT_PAID": {
            "en": "Receipt paid by {user}",
            "th": "รับชำระเงินจาก {user}"
        },
    }

    # Regex rules for when memo_id/args aren't available
    RX_RULES = [
        # Revenue recognized for alice (service period)
        (re.compile(r'^Revenue recognized for (?P<user>[\w.\-]+) \(service period\)$', re.U),
         lambda m: f"บันทึกรายได้สำหรับ {m['user']} (งวดให้บริการ)"),

        # Receipt issued for alice
        (re.compile(r'^Receipt issued for (?P<user>[\w.\-]+)$', re.U),
         lambda m: f"ออกใบรับสำหรับ {m['user']}"),

        # Receipt paid by alice
        (re.compile(r'^Receipt paid by (?P<user>[\w.\-]+)$', re.U),
         lambda m: f"รับชำระเงินจาก {m['user']}"),

        # Receipt issued for alice — service 2025-01 < issue 2025-09; recognize via Contract Asset
        (re.compile(
            r'^Receipt issued for (?P<user>[\w.\-]+)\s*[—–-]\s*service\s*(?P<period>\d{4}-\d{2})\s*<\s*issue\s*(?P<issue>\d{4}-\d{2});\s*recognize via Contract Asset$',
            re.U),
         lambda m: f"ออกใบรับสำหรับ {m['user']} — บริการ {m['period']} < ออก {m['issue']}; รับรู้ผ่านสินทรัพย์ตามสัญญา"),

        # Close ... to Retained Earnings
        (re.compile(r'^Close INCOME to Retained Earnings$', re.U),
         lambda m: "ปิดบัญชีรายได้ไปยังกำไรสะสม"),
        (re.compile(r'^Close EXPENSE to Retained Earnings$', re.U),
         lambda m: "ปิดบัญชีค่าใช้จ่ายไปยังกำไรสะสม"),
        (re.compile(r'^Close to Retained Earnings$', re.U),
         lambda m: "ปิดยอดไปยังกำไรสะสม"),

        # ECL provision (contract assets) 2025-01 — provision matrix
        (re.compile(r'^ECL provision \(contract assets\)\s*(?P<period>\d{4}-\d{2})\s*[—–-]\s*provision matrix$', re.U),
         lambda m: f"ตั้งค่าเผื่อ ECL (สินทรัพย์ตามสัญญา) {m['period']} — เมทริกซ์การกันสำรอง"),
    ]

    def memo_localize(memo_id, memo_args, lang="th"):
        tpl_key = memo_id or ""
        # Map a few generic IDs to our table
        key_map = {
            "PERIOD_CLOSE_INCOME": "PERIOD_CLOSE_INCOME",
            "PERIOD_CLOSE_EXPENSE": "PERIOD_CLOSE_EXPENSE",
            "PERIOD_CLOSE": "PERIOD_CLOSE",
            "REVENUE_RECOGNIZED": "REVENUE_RECOGNIZED",
            "RECEIPT_ISSUED": "RECEIPT_ISSUED",
            "RECEIPT_PAID": "RECEIPT_PAID",
            "ECL_PROVISION": "ECL_PROVISION",
            "RECEIPT_ISSUED_CA": "RECEIPT_ISSUED_CA",
        }
        tpl = MEMO_TEMPLATES.get(key_map.get(tpl_key, ""), {})
        s = tpl.get(lang) or tpl.get("en") or ""
        try:
            return s.format(**(memo_args or {}))
        except Exception:
            return s

    def memo_heuristic_th(memo_text: str) -> str:
        if not memo_text:
            return memo_text
        # collapse internal whitespace and linebreaks to improve matching
        oneline = " ".join(str(memo_text).split())
        for rx, render in RX_RULES:
            m = rx.match(oneline)
            if m:
                return render(m)
        return memo_text  # fallback

    def build_memo_th(row):
        mid = row.get("memo_id")
        if pd.notna(mid):
            return memo_localize(mid, row.get("memo_args") or {}, "th")
        return memo_heuristic_th(row.get("memo"))

    df["memo_th"] = df.apply(build_memo_th, axis=1)

    # --- canonical CSV bytes for hashing (keep source memo for stability) ---
    csv_buf = io.StringIO()
    w = csv.writer(csv_buf)
    w.writerow(["date", "ref", "memo", "account_id",
               "account_name", "account_type", "debit", "credit"])
    for _, r in df.iterrows():
        w.writerow([
            r.get("date", ""), r.get("ref", ""), r.get("memo", ""),
            r.get("account_id", ""), r.get("account_name", ""),
            r.get("account_type", ""),
            float(r.get("debit") or 0.0), float(r.get("credit") or 0.0)
        ])
    csv_bytes = csv_buf.getvalue().encode("utf-8")

    # --- run metadata ---
    now = datetime.now(timezone.utc)
    run_id = f"GLPDF-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    criteria = {"start": start, "end": end,
                "mode": "derived" if is_preview else "posted"}
    doc_digest = sha256(csv_bytes + json.dumps(criteria,
                        sort_keys=True).encode("utf-8")).hexdigest()
    doc_digest_short = doc_digest[:12]

    # --- paginate (slightly fewer rows for 12pt) ---
    ROWS_PER_PAGE = 28  # was 34
    pages = []
    for i in range(0, len(df), ROWS_PER_PAGE):
        chunk = df.iloc[i:i+ROWS_PER_PAGE].copy()
        page_index = len(pages) + 1
        first_ref = str(chunk.iloc[0]["ref"]) if not chunk.empty else "-"
        last_ref = str(chunk.iloc[-1]["ref"]) if not chunk.empty else "-"
        page_hash = sha256(f"{run_id}|{page_index}|{doc_digest}|{first_ref}|{last_ref}".encode(
            "utf-8")).hexdigest()[:12]
        pages.append({
            "index": page_index,
            "rows": chunk.to_dict(orient="records"),
            "first_ref": first_ref,
            "last_ref": last_ref,
            "hash": page_hash,
        })

    # final control line
    if pages:
        pages[-1]["rows"].append({
            "date": "", "ref": "", "memo": "— End of ledger —", "memo_th": "— สิ้นสุดสมุดบัญชี —",
            "account_id": "", "account_name": "", "account_type": "", "debit": 0.00, "credit": 0.00
        })

    total_pages = max(1, len(pages))

    # --- summary aggregates ---
    tb = (df.groupby(["account_id", "account_name", "account_type"], dropna=False)
            .agg(debit_sum=("debit", "sum"), credit_sum=("credit", "sum"))
            .reset_index())
    tb["debit_sum"] = tb["debit_sum"].round(2)
    tb["credit_sum"] = tb["credit_sum"].round(2)
    tb_total_dr = float(round(tb["debit_sum"].sum(), 2))
    tb_total_cr = float(round(tb["credit_sum"].sum(), 2))

    inc = tb[tb["account_type"] == "INCOME"]
    exp = tb[tb["account_type"] == "EXPENSE"]
    total_income = round(
        float(inc["credit_sum"].sum() - inc["debit_sum"].sum()), 2)
    total_expense = round(
        float(exp["debit_sum"].sum() - exp["credit_sum"].sum()), 2)
    pl = {"income": total_income, "expense": total_expense,
          "net_income": round(total_income - total_expense, 2)}

    asg = tb[tb["account_type"] == "ASSET"]
    lbg = tb[tb["account_type"] == "LIABILITY"]
    eqg = tb[tb["account_type"] == "EQUITY"]
    bs = {
        "assets_delta":      round(float(asg["debit_sum"].sum() - asg["credit_sum"].sum()), 2),
        "liabilities_delta": round(float(lbg["credit_sum"].sum() - lbg["debit_sum"].sum()), 2),
        "equity_delta":      round(float(eqg["credit_sum"].sum() - eqg["debit_sum"].sum()), 2),
    }

    coa_legend = (df[["account_id", "account_name", "account_type"]]
                  .drop_duplicates()
                  .sort_values(["account_type", "account_id"])
                  .to_dict(orient="records"))

    manifest = {
        "run_id": run_id,
        "mode": criteria["mode"],
        "window": {"start": start, "end": end},
        "doc_sha256": doc_digest,
        "total_lines": int(len(df)),
        "total_pages": int(total_pages),
        "page_hashes": [{"page": p["index"], "hash": p["hash"], "first_ref": p["first_ref"], "last_ref": p["last_ref"]} for p in pages],
        "generated_at": now.isoformat(),
        "host": socket.gethostname(),
        "generated_by": current_user.username,
    }
    audit("export.ledger_th_pdf", target_type="window", target_id=f"{start}:{end}",
          outcome="success", status=200,
          extra={"run_id": run_id, "mode": criteria["mode"], "doc_sha256": doc_digest})

    # --- render HTML -> PDF ---
    html = render_template(
        "admin/ledger_pdf_th.html",
        pages=pages,
        total_pages=total_pages,
        doc_digest=doc_digest,
        doc_digest_short=doc_digest_short,
        run_id=run_id,
        start=start, end=end,
        is_preview=is_preview,
        printed_on=now,
        printed_by=current_user.username,
        watermark_text=(
            "ใช้สำหรับการภายในเท่านั้น" if is_preview else "ลับสุดยอด"),
        tb_rows=tb.to_dict(orient="records"),
        tb_total_dr=tb_total_dr,
        tb_total_cr=tb_total_cr,
        pl=pl,
        bs=bs,
        coa_legend=coa_legend,
    )

    pdf = HTML(string=html, base_url=current_app.static_folder).write_pdf()
    fname = f"general_ledger_{criteria['mode']}_{start}_to_{end}_{run_id}.pdf"
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename=\"{fname}\"'})
