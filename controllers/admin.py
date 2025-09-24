from models.billing_store import list_receipts  # add at top if not imported
from flask import flash  # add at top if not imported
from calendar import monthrange
from services.forecast import build_daily_series, multi_horizon_forecast
from services.accounting import derive_journal, trial_balance, income_statement, balance_sheet
from flask import jsonify
from datetime import timedelta
from services.pricing_sim import build_pricing_components, simulate_vs_current
import io
from datetime import date, datetime
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
    list_billed_items_for_user, list_receipts, create_receipt_from_rows,
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
import calendar
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
    else:
        if view not in {"detail", "aggregate"}:
            view = "detail"

    # for legacy usage pages
    EPOCH_START = "1970-01-01"
    before = request.args.get("before") or date.today().isoformat()
    start_d, end_d = EPOCH_START, before

    # NEW: query parts for billing "trend" subview
    bview = (request.args.get("bview") or "invoices").strip().lower()
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

    # Dashboard-only context
    kpis: dict = {}
    series = {
        "daily_cost": [],
        "daily_labels": [],
        "tier_labels": [],
        "tier_values": [],
        "top_users_labels": [],
        "top_users_values": [],
        "node_jobs_labels": [], "node_jobs_values": [],
        "node_cpu_labels": [],  "node_cpu_values": [],
        "node_gpu_labels": [],  "node_gpu_values": [],
        # energy
        "energy_user_labels": [], "energy_user_values": [],
        "energy_node_labels": [], "energy_node_values": [],
        "energy_eff_user_labels": [], "energy_eff_user_values": [],
        # throughput / reliability
        "succ_user_labels": [], "succ_user_success": [], "succ_user_fail": [],
        "succ_part_labels": [], "succ_part_success": [], "succ_part_fail": [],
        "succ_qos_labels":  [], "succ_qos_success":  [], "succ_qos_fail":  [],
        "fail_exit_labels": [], "fail_exit_values": [],
        "fail_state_labels": [], "fail_state_values": [],
    }

    # helpers
    def _safe_float(x, default=0.0):
        try:
            if x is None:
                return default
            s = str(x).strip().replace("฿", "").replace(",", "")
            return float(s) if s else default
        except Exception:
            return default

    def _ensure_col_local(df, name, default_val=""):
        if name in df.columns:
            return df[name]
        return pd.Series([default_val] * len(df), index=df.index)

    # ---- DASHBOARD ----
    if section == "dashboard":
        def cap(name, fn, default):
            try:
                return fn()
            except Exception as e:
                notes.append(f"dashboard.{name}: {e!s}")
                return default

        end_d = before
        start_d = (date.fromisoformat(before) - timedelta(days=90)).isoformat()

        df, data_source, ds_notes = cap(
            "fetch",
            lambda: fetch_jobs_with_fallbacks(start_d, end_d),
            (pd.DataFrame(), None, []),
        )
        notes.extend(ds_notes or [])
        df = cap("compute_costs", lambda: compute_costs(df), pd.DataFrame())

        # cutoff (UTC-aware) + ensure End exists
        def _cutoff_df():
            if "End" in df.columns:
                end_series = pd.to_datetime(
                    df["End"], errors="coerce", utc=True)
                cutoff_utc = _to_utc_day_end(end_d)
                out = df[end_series.notna() & (
                    end_series <= cutoff_utc)].copy()
                out["End"] = end_series
                return out
            out = df.copy()
            out["End"] = pd.NaT
            return out

        df = cap("cutoff", _cutoff_df, df)

        # unbilled view
        def _unbilled():
            d = df.copy()
            d["JobKey"] = d["JobID"].astype(str).map(canonical_job_id)
            already = set(billed_job_ids())
            return d[~d["JobKey"].isin(already)]

        df_unbilled = cap("unbilled", _unbilled, pd.DataFrame())

        # ---- KPIs ----
        kpis["unbilled_cost"] = cap(
            "kpi.unbilled_cost",
            lambda: float(_ensure_col_local(df_unbilled, "Cost (฿)",
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
            lambda: (
                sum(
                    _safe_float(r.get("total"))
                    for r in paid
                    if pd.notna(pd.to_datetime(r.get("paid_at"), errors="coerce", utc=True))
                    and pd.to_datetime(r.get("paid_at"), errors="coerce", utc=True) >= (pd.Timestamp(end_d, tz="UTC") - pd.Timedelta(days=30))
                )
            ),
            0.0,
        )

        kpis["jobs_last_30d"] = cap(
            "kpi.jobs_last_30d",
            lambda: int(
                (df["End"] >= (pd.Timestamp(end_d, tz="UTC") - pd.Timedelta(days=30))).sum())
            if "End" in df.columns else 0,
            0,
        )

        # ---- Series ----
        def _series_daily():
            if df.empty or "End" not in df.columns or "Cost (฿)" not in df.columns:
                return ([], [])
            daily = df.groupby(df["End"].dt.date)[
                "Cost (฿)"].sum().sort_index()
            return [d.isoformat() for d in daily.index], [round(float(v), 2) for v in daily.values]

        series["daily_labels"], series["daily_cost"] = cap(
            "series.daily", _series_daily, ([], []))

        def _series_tier():
            if df.empty or "tier" not in df.columns or "Cost (฿)" not in df.columns:
                return ([], [])
            tier_sum = df.groupby("tier", dropna=False)[
                "Cost (฿)"].sum().sort_values(ascending=False)
            return [str(i).upper() for i in tier_sum.index], [round(float(v), 2) for v in tier_sum.values]

        series["tier_labels"], series["tier_values"] = cap(
            "series.tier", _series_tier, ([], []))

        def _series_top_users():
            if df.empty or "User" not in df.columns or "Cost (฿)" not in df.columns:
                return ([], [])
            top = df.groupby("User")["Cost (฿)"].sum(
            ).sort_values(ascending=False).head(10)
            return list(top.index), [round(float(v), 2) for v in top.values]

        series["top_users_labels"], series["top_users_values"] = cap(
            "series.top_users", _series_top_users, ([], []))

        # ---- Totals chips ----
        tot_cpu = cap("totals.cpu", lambda: float(
            _ensure_col(df, "CPU_Core_Hours", 0).sum()), 0.0)
        tot_gpu = cap("totals.gpu", lambda: float(
            _ensure_col(df, "GPU_Hours", 0).sum()), 0.0)
        tot_mem = cap(
            "totals.mem",
            lambda: float(
                _ensure_col(df, "Mem_GB_Hours", 0).sum()
                if "Mem_GB_Hours" in df.columns else _ensure_col(df, "Mem_GB_Hours_Used", 0).sum()
            ),
            0.0,
        )
        tot_elapsed = cap("totals.elapsed", lambda: float(
            _ensure_col(df, "Elapsed_Hours", 0).sum()), 0.0)

        # (node/energy/throughput series left as-is for brevity)

        return render_template(
            "admin/dashboard.html",
            current_user=current_user,
            before=before, start=start_d, end=end_d,
            kpis=kpis, series=series, data_source=data_source, notes=notes,
            tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
            url_for=url_for, all_rates=rates,
        )

    # ---- USAGE / MYUSAGE / BILLING / TIERS ----
    try:
        if section == "usage":
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
                df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
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

            # header emphasis for RAW
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

        elif section == "myusage":
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username
            )

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

                # totals (safe)
                tot_cpu = float(_ensure_col(df, "CPU_Core_Hours", 0).sum())
                tot_gpu = float(_ensure_col(df, "GPU_Hours", 0).sum())
                tot_mem = float(_ensure_col(df, "Mem_GB_Hours_Used", 0).sum())
                tot_elapsed = float(_ensure_col(df, "Elapsed_Hours", 0).sum())
                grand_total = float(_ensure_col(df, "Cost (฿)", 0).sum())

            # (no billed view here anymore for admin's own usage)

        elif section == "billing":
            # === New admin Billing views ===
            # bview=invoices | trend
            bview = (request.args.get("bview") or "invoices").strip().lower()
            year = request.args.get("year")
            month = request.args.get("month")
            selected_user = (request.args.get("u") or "").strip()

            # Always show invoice lists
            pending = admin_list_receipts(status="pending") or []
            paid = admin_list_receipts(status="paid") or []

            # Trend view (admin can see any user)
            monthly_agg = []
            month_detail_rows = []
            year_total = 0.0
            tot_cpu_m = tot_gpu_m = tot_mem_m = month_total = 0.0
            current_year = date.today().year

            if bview == "trend":
                try:
                    # Inputs & window
                    try:
                        y = int(year or current_year)
                    except Exception:
                        y = current_year
                    ym_start = f"{y}-01-01"
                    ym_end = (date.today().isoformat() if y == date.today().year
                            else f"{y}-12-31")

                    # Fetch all users, compute costs, then filter by user if selected
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

                    if selected_user:
                        df = df[df["User"].astype(str).str.strip(
                        ).str.lower() == selected_user.lower()]

                    # Build "all_users" list (for datalist)
                    if "User" in df_raw.columns:
                        all_users = sorted(u for u in df_raw["User"].astype(
                            str).fillna("").str.strip().unique() if u)

                    # Monthly aggregate for this user (or empty selection)
                    if not df.empty and selected_user:
                        # Month number from End (local months based on timestamp date)
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
                                # prefer used memory column when present
                                mem_col = "Mem_GB_Hours_Used" if "Mem_GB_Hours_Used" in dmonth.columns else "Mem_GB_Hours"
                                tot_mem_m = float(pd.to_numeric(dmonth.get(
                                    mem_col), errors="coerce").fillna(0).sum())
                                month_total = float(pd.to_numeric(dmonth.get(
                                    "Cost (฿)"), errors="coerce").fillna(0).sum())

                except Exception as e:
                    notes.append(f"billing.trend: {e}")

            # Render with additional context used by template
            return render_template(
                "admin/page.html",
                section="billing",
                current_user=current_user,
                pending=pending, paid=paid,
                # trend context
                bview=bview,
                selected_user=selected_user,
                year=(year or ""),
                month=(month or ""),
                current_year=current_year,
                monthly_agg=monthly_agg,
                month_detail_rows=month_detail_rows,
                year_total=year_total,
                tot_cpu_m=tot_cpu_m, tot_gpu_m=tot_gpu_m, tot_mem_m=tot_mem_m, month_total=month_total,
                all_users=all_users,
                notes=notes,
                url_for=url_for,
            )

            # default invoices view
            pending = admin_list_receipts(status="pending")
            paid = admin_list_receipts(status="paid")

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
                        u for u in
                        df_jobs["User"].astype(str).fillna(
                            "").str.strip().unique().tolist()
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
                {
                    "username": u,
                    "tier": current_tier_for(u),
                    "overridden": (u.strip().lower() in ov),
                }
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
        # trend defaults (for when section!=billing or bview!=trend)
        bview=(locals().get("bview") or "invoices"),
        selected_user=locals().get("selected_user", ""),
        year=locals().get("year", date.today().year),
        current_year=date.today().year,
        month=locals().get("month"),
        monthly_agg=locals().get("monthly_agg", []),
        year_total=locals().get("year_total", 0.0),
        month_detail_rows=locals().get("month_detail_rows", []),
        tot_cpu_m=locals().get("tot_cpu_m", 0.0),
        tot_gpu_m=locals().get("tot_gpu_m", 0.0),
        tot_mem_m=locals().get("tot_mem_m", 0.0),
        month_total=locals().get("month_total", 0.0),
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
    audit("rates.update.form", target=f"type={tier}", status=200,
          extra={"cpu": cpu, "gpu": gpu, "mem": mem})
    return redirect(url_for("admin.admin_form", section="rates", type=tier))


@admin_bp.post("/admin/receipts/<int:rid>/paid")
@login_required
@fresh_login_required
@admin_required
def mark_paid(rid: int):
    ok = mark_receipt_paid(rid, current_user.username)
    if ok:
        RECEIPT_MARKED_PAID.labels(actor_type="admin").inc()
    audit(
        "receipt.paid.admin",
        target=f"receipt={rid}",
        status=200 if ok else 404,
        extra={"actor": current_user.username, "reason": "manual_mark_paid"}
    )
    return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))


@admin_bp.get("/admin/paid.csv")
@login_required
@admin_required
def paid_csv():
    fname, csv_text = paid_receipts_csv()
    CSV_DOWNLOADS.labels(kind="admin_paid").inc()
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
        current_user.username, start_d, end_d, df.to_dict(orient="records")
    )
    RECEIPT_CREATED.labels(scope="admin").inc()
    return redirect(url_for("admin.admin_form", section="myusage", before=before, view="detail"))


@admin_bp.get("/admin/audit")
@login_required
@admin_required
def audit_page():
    rows = list_audit(limit=100)
    return render_template("admin/audit.html", rows=rows)


@admin_bp.get("/admin/audit.csv")
@login_required
@admin_required
def audit_csv():
    fname, csv_text = export_csv()
    CSV_DOWNLOADS.labels(kind="audit").inc()
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
    before = (request.args.get("before") or date.today().isoformat()).strip()
    end_d = before
    start_d = (date.fromisoformat(before) - timedelta(days=365)).isoformat()

    start_q = (request.args.get("start") or start_d).strip()
    end_q = (request.args.get("end") or end_d).strip()

    j = derive_journal(start_q, end_q)
    tb = trial_balance(j)
    pnl = income_statement(j)
    bs = balance_sheet(j)

    return render_template(
        "admin/ledger.html",
        start=start_q, end=end_q,
        journal=j.to_dict(orient="records"),
        tb=tb.to_dict(orient="records"),
        tb_meta={
            "sum_debits": tb.attrs.get("sum_debits", 0.0),
            "sum_credits": tb.attrs.get("sum_credits", 0.0),
            "out_of_balance": tb.attrs.get("out_of_balance", 0.0),
        },
        pnl=pnl.to_dict(orient="records")[0] if not pnl.empty else {
            "Revenue": 0, "Expenses": 0, "Net_Income": 0},
        bs=bs.to_dict(orient="records")[0] if not bs.empty else {
            "Assets": 0, "Liabilities": 0, "Equity_Including_PnL": 0, "Check(Assets - L-E)": 0
        },
        today=date.today().isoformat(),
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
    from services.accounting_export import build_general_ledger_csv
    start = (request.args.get("start") or "1970-01-01").strip()
    end = (request.args.get("end") or date.today().isoformat()).strip()
    fname, csv_text = build_general_ledger_csv(start, end)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
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
            audit("tiers.set.invalid", target=f"user={username}", status=400, extra={
                  "desired": desired})
            continue

        natural = classify_user_type(username)
        prev_override = existing.get(username.lower())
        prev_effective = prev_override if prev_override else natural

        if desired == natural:
            if prev_override is not None:
                clear_override(username)
                removed += 1
                audit("tier.override.clear", target=f"user={username}", status=200,
                      extra={"from": prev_effective, "to": natural, "natural": natural})
        else:
            upsert_override(username, desired)
            changed += 1
            audit("tier.override.set", target=f"user={username}", status=200,
                  extra={"from": prev_effective, "to": desired, "natural": natural})

    audit("tiers.save.summary",
          target=f"changed={changed},removed={removed}", status=200)
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
    """
    Create monthly invoices (receipts) for ALL users who have unbilled jobs
    in the selected year-month. Each user gets one receipt covering that calendar month.
    """
    try:
        y = int((request.form.get("year") or "").strip())
        m = int((request.form.get("month") or "").strip())
        if not (2000 <= y <= 2100 and 1 <= m <= 12):
            raise ValueError("Invalid year/month")
    except Exception:
        flash("Invalid year/month", "error")
        return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))

    start_d = date(y, m, 1).isoformat()
    last_day = monthrange(y, m)[1]
    end_d = date(y, m, last_day).isoformat()

    # Fetch all users' jobs within month, compute costs
    try:
        df_raw, _, _ = fetch_jobs_with_fallbacks(start_d, end_d)
        df = compute_costs(df_raw)

        # UTC-aware month bounds
        if "End" in df.columns:
            end_series = pd.to_datetime(df["End"], errors="coerce", utc=True)
            lo = pd.Timestamp(start_d, tz="UTC")
            hi = pd.Timestamp(end_d, tz="UTC") + \
                pd.Timedelta(hours=23, minutes=59, seconds=59)
            df = df[end_series.notna() & (end_series >= lo) &
                    (end_series <= hi)].copy()
            df["End"] = end_series

        # Deduplicate against already-billed parents
        if not df.empty:
            df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
            already = set(billed_job_ids())
            df = df[~df["JobKey"].isin(already)]

        if df.empty:
            flash(f"No unbilled jobs found for {y}-{m:02d}.", "info")
            return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))

        # Group by user and create one receipt per user
        created = 0
        skipped = 0
        for user_name, duser in df.groupby(df["User"].astype(str)):
            # Skip if this exact month already has a non-void receipt for this user
            try:
                existing = [r for r in (list_receipts(user_name) or [])
                            if str(r.get("start")).startswith(f"{y}-{m:02d}-")
                            and str(r.get("end")).startswith(f"{y}-{m:02d}-")
                            and r.get("status") in ("pending", "paid")]
            except Exception:
                existing = []
            if existing:
                skipped += 1
                continue

            rid, total, _items = create_receipt_from_rows(
                user_name, start_d, end_d, duser.drop(
                    columns=["JobKey"]).to_dict(orient="records")
            )
            RECEIPT_CREATED.labels(scope="admin_bulk").inc()
            audit("receipt.create.month",
                  target=f"receipt={rid}",
                  status=200,
                  extra={"user": user_name, "year": y, "month": m, "jobs": int(len(duser)), "total": float(total)})
            created += 1

        msg = f"Created {created} invoice(s) for {y}-{m:02d}" + (
            f"; skipped {skipped} (already exist)" if skipped else "")
        flash(msg, "success")
    except Exception as e:
        audit("receipt.create.month.error",
              target=f"{y}-{m:02d}", status=500, extra={"error": str(e)})
        flash(f"Error creating invoices: {e}", "error")

    return redirect(url_for("admin.admin_form", section="billing", bview="invoices"))
