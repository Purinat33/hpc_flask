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
from services.datetimex import local_day_end_utc, APP_TZ
admin_bp = Blueprint("admin", __name__)


@admin_bp.get("/admin")
@login_required
@admin_required
def admin_form():
    rates = rates_store.load_rates()

    # ---- parse & sanitize query params ----
    section = (request.args.get("section") or "usage").strip().lower()
    if section not in {"rates", "usage", "billing", "myusage", "dashboard"}:
        section = "usage"

    tier = (request.args.get("type") or "mu").strip().lower()
    if tier not in rates:
        tier = "mu"

    view = (request.args.get("view") or "detail").strip().lower()
    if section == "myusage":
        if view not in {"detail", "aggregate", "billed"}:
            view = "detail"
    else:
        if view not in {"detail", "aggregate"}:
            view = "detail"

    EPOCH_START = "1970-01-01"
    before = request.args.get("before") or date.today().isoformat()
    start_d, end_d = EPOCH_START, before

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

    # Dashboard-only context
    kpis: dict = {}
    series = {
        "daily_cost": [],
        "daily_labels": [],
        "tier_labels": [],
        "tier_values": [],
        "top_users_labels": [],
        "top_users_values": [],
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

    def _ensure_col(df, name, default_val=""):
        """Return a Series for df[name]; if missing, return a same-length Series of default_val."""
        if name in df.columns:
            return df[name]
        return pd.Series([default_val] * len(df), index=df.index)

    # ---- DASHBOARD ----
    if section == "dashboard":
        try:
            # 90-day window ending at `before`
            end_d = before
            start_d = (date.fromisoformat(before) -
                       timedelta(days=90)).isoformat()

            df, data_source, ds_notes = fetch_jobs_with_fallbacks(
                start_d, end_d)
            notes.extend(ds_notes or [])
            if df is None:
                df = pd.DataFrame()
            df = compute_costs(df)

            # Enforce end cutoff in **UTC-aware** terms
            if "End" in df.columns:
                end_series = pd.to_datetime(
                    df["End"], errors="coerce", utc=True)
                cutoff_utc = pd.Timestamp(
                    end_d, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df = df[end_series.notna() & (end_series <= cutoff_utc)]
                df["End"] = end_series
            else:
                df["End"] = pd.NaT

            # Unbilled view
            jobid = _ensure_col(df, "JobID", "")
            df["JobKey"] = jobid.astype(str).map(canonical_job_id)
            already = set(billed_job_ids())
            df_unbilled = df[~df["JobKey"].isin(already)].copy()

            # KPIs
            kpis["unbilled_cost"] = (
                float(_ensure_col(df_unbilled, "Cost (฿)", 0).sum()
                      ) if not df_unbilled.empty else 0.0
            )

            pending = admin_list_receipts(status="pending") or []
            kpis["pending_receivables"] = float(
                sum(_safe_float(r.get("total")) for r in pending))

            paid = admin_list_receipts(status="paid") or []
            cutoff_30_utc = pd.Timestamp(
                end_d, tz="UTC") - pd.Timedelta(days=30)
            paid_30 = 0.0
            for r in paid:
                ts = pd.to_datetime(
                    r.get("paid_at"), errors="coerce", utc=True)
                if pd.notna(ts) and ts >= cutoff_30_utc:
                    paid_30 += _safe_float(r.get("total"))
            kpis["paid_last_30d"] = float(paid_30)

            kpis["jobs_last_30d"] = int(
                (df["End"] >= cutoff_30_utc).sum()) if "End" in df.columns else 0

            # Series
            if not df.empty:
                if "End" in df.columns and "Cost (฿)" in df.columns:
                    daily = df.groupby(df["End"].dt.date)[
                        "Cost (฿)"].sum().sort_index()
                    series["daily_labels"] = [d.isoformat()
                                              for d in daily.index]
                    series["daily_cost"] = [
                        round(float(v), 2) for v in daily.values]

                if "tier" in df.columns and "Cost (฿)" in df.columns:
                    tier_sum = df.groupby("tier", dropna=False)[
                        "Cost (฿)"].sum().sort_values(ascending=False)
                    series["tier_labels"] = [
                        str(i).upper() for i in tier_sum.index]
                    series["tier_values"] = [
                        round(float(v), 2) for v in tier_sum.values]

                if "User" in df.columns and "Cost (฿)" in df.columns:
                    top = df.groupby("User")["Cost (฿)"].sum(
                    ).sort_values(ascending=False).head(10)
                    series["top_users_labels"] = list(top.index)
                    series["top_users_values"] = [
                        round(float(v), 2) for v in top.values]

            # Totals chips (use _ensure_col to avoid int.sum())
            tot_cpu = float(_ensure_col(df, "CPU_Core_Hours", 0).sum())
            tot_gpu = float(_ensure_col(df, "GPU_Hours", 0).sum())
            tot_mem = float(
                (_ensure_col(df, "Mem_GB_Hours", 0).sum()
                 if "Mem_GB_Hours" in df.columns else _ensure_col(df, "Mem_GB_Hours_Used", 0).sum())
            )
            tot_elapsed = float(_ensure_col(df, "Elapsed_Hours", 0).sum())

        except Exception as e:
            notes.append(f"dashboard_error: {e!s}")

        return render_template(
            "admin/dashboard.html",
            current_user=current_user,
            before=before, start=start_d, end=end_d,
            kpis=kpis, series=series, data_source=data_source, notes=notes,
            tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
            url_for=url_for,
        )

    try:
        if section == "usage":
            # -------- fetch RAW (parents + steps) --------
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d)

            if not df_raw.empty and "End" in df_raw.columns:
                end_series = pd.to_datetime(
                    df_raw["End"], errors="coerce", utc=True)
                cutoff_utc = pd.Timestamp(
                    end_d, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df_raw = df_raw[end_series.notna() & (
                    end_series <= cutoff_utc)]
                df_raw["End"] = end_series

                raw_cols = list(df_raw.columns)
                raw_rows = df_raw.head(200).to_dict(orient="records")
            elif not df_raw.empty:
                raw_cols = list(df_raw.columns)
                raw_rows = df_raw.head(200).to_dict(orient="records")

            # -------- compute COSTED (aggregated to parents) --------
            df = compute_costs(
                df_raw.copy() if df_raw is not None else pd.DataFrame())

            # hide already billed parents
            if not df.empty:
                df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
                already = billed_job_ids()
                df = df[~df["JobKey"].isin(already)]

            # totals (safe even when df is empty/no columns)
            tot_cpu = float(_ensure_col(df, "CPU_Core_Hours", 0).sum())
            tot_gpu = float(_ensure_col(df, "GPU_Hours", 0).sum())
            tot_mem = float(_ensure_col(df, "Mem_GB_Hours_Used", 0).sum())
            tot_elapsed = float(_ensure_col(df, "Elapsed_Hours", 0).sum())

            # detailed table (computed)
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

            # aggregate table
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

            # header highlighting for RAW table (unchanged)
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
                start_d, end_d, username=current_user.username)

            if not df_raw.empty and "End" in df_raw.columns:
                end_series = pd.to_datetime(
                    df_raw["End"], errors="coerce", utc=True)
                cutoff_utc = pd.Timestamp(
                    end_d, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
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

                # totals (safe regardless of emptiness)
                tot_cpu = float(_ensure_col(df, "CPU_Core_Hours", 0).sum())
                tot_gpu = float(_ensure_col(df, "GPU_Hours", 0).sum())
                tot_mem = float(_ensure_col(df, "Mem_GB_Hours_Used", 0).sum())
                tot_elapsed = float(_ensure_col(df, "Elapsed_Hours", 0).sum())
                grand_total = float(_ensure_col(df, "Cost (฿)", 0).sum())

            else:  # 'billed'
                pending_items = list_billed_items_for_user(
                    current_user.username, "pending")
                paid_items = list_billed_items_for_user(
                    current_user.username, "paid")
                sum_pending = float(
                    sum(i["cost"] for i in pending_items)) if pending_items else 0.0
                sum_paid = float(sum(i["cost"]
                                 for i in paid_items)) if paid_items else 0.0

                my_all_receipts = list_receipts(current_user.username)
                my_pending_receipts = [
                    r for r in my_all_receipts if r["status"] == "pending"]
                my_paid_receipts = [
                    r for r in my_all_receipts if r["status"] == "paid"]

        elif section == "billing":
            pending = admin_list_receipts(status="pending")
            paid = admin_list_receipts(status="paid")

    except Exception as e:
        notes.append(str(e))

    return render_template(
        "admin/page.html",
        section=section,
        all_rates=rates,
        current=rates.get(tier, {"cpu": 0, "gpu": 0}),
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
        url_for=url_for,
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
    return redirect(url_for("admin.admin_form", section="billing"))


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
        df["End"] = pd.to_datetime(df["End"], errors="coerce")
        cutoff = pd.to_datetime(
            end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        df = df[df["End"].notna() & (df["End"] <= cutoff)]

    df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
    df = df[~df["JobKey"].isin(billed_job_ids())]

    if df.empty:
        return redirect(url_for("admin.admin_form", section="myusage", before=before, view="detail"))

    rid, total, _ = create_receipt_from_rows(
        current_user.username, start_d, end_d, df.to_dict(orient="records")
    )
    RECEIPT_CREATED.labels(scope="admin").inc()
    return redirect(url_for("admin.admin_form", section="myusage", before=before, view="billed"))


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
