# controllers/user.py
from flask import current_app, make_response
from weasyprint import HTML
import pandas as pd
from models.billing_store import list_receipts, get_receipt_with_items
from flask import Blueprint, render_template, request, Response, url_for, redirect
from flask_login import login_required, current_user
from datetime import date
import io
from services.data_sources import fetch_jobs_with_fallbacks
from services.billing import compute_costs
from models.billing_store import billed_job_ids, canonical_job_id
from models.audit_store import audit
from services.datetimex import APP_TZ
from services.metrics import CSV_DOWNLOADS
from services.org_info import ORG_INFO, ORG_INFO_TH
from models.billing_store import _tax_cfg
user_bp = Blueprint("user", __name__)


@user_bp.get("/me/receipts")
@login_required
def my_receipts():
    return render_template(
        "user/receipts.html",
        receipts=list_receipts(current_user.username)
    )


@user_bp.get("/me/receipts/<int:rid>")
@login_required
def view_receipt(rid: int):
    rec, items = get_receipt_with_items(rid)
    is_admin = getattr(current_user, "is_admin", False)
    if not rec or (rec.get("username") != current_user.username and not is_admin):
        audit(action="receipt.view.denied",
              target=f"receipt={rid}", status=403,
              extra={"actor": current_user.username})
        return redirect(url_for("user.my_receipts"))
    return render_template("user/receipt_detail.html", r=rec, rows=items,
                           is_owner=(rec.get("username") == current_user.username))


@user_bp.get("/me")
@login_required
def my_usage():
    # Admins use the Admin UI
    if getattr(current_user, "is_admin", False):
        return redirect(url_for("admin.admin_form"))

    EPOCH_START = "1970-01-01"
    before = (request.args.get("before") or date.today().isoformat()).strip()

    view = (request.args.get("view") or "detail").lower()
    if view not in {"detail", "aggregate", "billed", "trend"}:
        view = "detail"

    # Common context
    rows, agg_rows = [], []
    data_source = None
    notes: list[str] = []
    total_cost = 0.0
    raw_cols: list[str] = []
    raw_rows: list[dict] = []
    header_classes: dict[str, str] = {}

    # Billed (invoices) context
    my_pending_receipts = []
    my_paid_receipts = []
    sum_pending = sum_paid = 0.0

    # Trend context
    current_year = date.today().year
    year = request.args.get("year")
    month = request.args.get("month")
    monthly_agg = []
    month_detail_rows = []
    year_total = 0.0
    tot_cpu_m = tot_gpu_m = tot_mem_m = month_total = 0.0

    # Helper
    def _ensure_col(df, name, default_val=0):
        if name in df.columns:
            return df[name]
        return pd.Series([default_val] * len(df), index=df.index)

    try:
        if view in {"detail", "aggregate"}:
            start_d, end_d = EPOCH_START, before
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username
            )

            if not df_raw.empty and "End" in df_raw.columns:
                end_series = pd.to_datetime(
                    df_raw["End"], errors="coerce", utc=True)
                cutoff_utc = pd.Timestamp(
                    end_d, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df_raw = df_raw[end_series.notna() & (
                    end_series <= cutoff_utc)]
                df_raw["End"] = end_series

            if not df_raw.empty:
                raw_cols = list(df_raw.columns)
                raw_rows = df_raw.head(200).to_dict(orient="records")
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

            df = compute_costs(
                df_raw.copy() if df_raw is not None else pd.DataFrame())

            # Hide already billed parents
            if not df.empty:
                df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
                already = billed_job_ids()
                df = df[~df["JobKey"].isin(already)]

            if view == "detail":
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
                total_cost = float(_ensure_col(df, "Cost (฿)", 0).sum())

            elif view == "aggregate" and not df.empty:
                agg_row = {
                    "jobs": int(len(df)),
                    "CPU_Core_Hours": float(_ensure_col(df, "CPU_Core_Hours", 0).sum()),
                    "GPU_Hours": float(_ensure_col(df, "GPU_Hours", 0).sum()),
                    "Mem_GB_Hours_Used": float(_ensure_col(df, "Mem_GB_Hours_Used", 0).sum()),
                    "Cost (฿)": float(_ensure_col(df, "Cost (฿)", 0).sum()),
                }
                agg_rows = [agg_row]

        elif view == "billed":
            my_all = list_receipts(current_user.username) or []
            my_pending_receipts = [
                r for r in my_all if r.get("status") == "pending"]
            my_paid_receipts = [r for r in my_all if r.get("status") == "paid"]
            sum_pending = float(sum(float(r.get("total") or 0)
                                for r in my_pending_receipts))
            sum_paid = float(sum(float(r.get("total") or 0)
                             for r in my_paid_receipts))

        else:  # view == "trend"
            # Parse year/month
            try:
                y = int(year) if year else current_year
            except Exception:
                y = current_year
            start_d = f"{y}-01-01"
            end_d = date.today().isoformat(
            ) if y == date.today().year else f"{y}-12-31"

            # Fetch only this user's jobs for that year
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username)
            df = compute_costs(df_raw)

            # UTC-aware cutoff
            if "End" in df.columns:
                end_series = pd.to_datetime(
                    df["End"], errors="coerce", utc=True)
                cutoff_utc = pd.Timestamp(
                    end_d, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df = df[end_series.notna() & (end_series <= cutoff_utc)].copy()
                df["End"] = end_series
            else:
                df["End"] = pd.NaT

            if not df.empty:
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

                # Month details if requested
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
                        tot_cpu_m = float(pd.to_numeric(dmonth.get(
                            "CPU_Core_Hours"), errors="coerce").fillna(0).sum())
                        tot_gpu_m = float(pd.to_numeric(dmonth.get(
                            "GPU_Hours"), errors="coerce").fillna(0).sum())
                        mem_col = "Mem_GB_Hours_Used" if "Mem_GB_Hours_Used" in dmonth.columns else "Mem_GB_Hours"
                        tot_mem_m = float(pd.to_numeric(dmonth.get(
                            mem_col), errors="coerce").fillna(0).sum())
                        month_total = float(pd.to_numeric(dmonth.get(
                            "Cost (฿)"), errors="coerce").fillna(0).sum())

            # Set string forms for template
            year = str(y)

    except Exception as e:
        notes.append(str(e))

    # Render with everything the template expects (even if empty)
    tax_enabled, tax_label, tax_rate, tax_inclusive = _tax_cfg()
    TAX_UI = {
        "enabled": bool(tax_enabled and (tax_rate or 0) > 0),
        "label": tax_label,
        "rate": float(tax_rate or 0),
        "inclusive": bool(tax_inclusive),
    }
    return render_template(
        "user/usage.html",
        current_user=current_user,
        before=before,
        view=view,
        # detail/aggregate
        rows=rows, agg_rows=agg_rows, total_cost=total_cost,
        data_source=data_source, notes=notes,
        raw_cols=raw_cols, raw_rows=raw_rows, header_classes=header_classes,
        url_for=url_for,
        # invoices
        my_pending_receipts=my_pending_receipts,
        my_paid_receipts=my_paid_receipts,
        sum_pending=sum_pending, sum_paid=sum_paid,
        # trend
        current_year=current_year,
        year=year, month=month,
        monthly_agg=monthly_agg,
        month_detail_rows=month_detail_rows,
        year_total=year_total,
        tot_cpu_m=tot_cpu_m, tot_gpu_m=tot_gpu_m, tot_mem_m=tot_mem_m, month_total=month_total,
        TAX_UI=TAX_UI,
    )


@user_bp.get("/me.csv")
@login_required
def my_usage_csv():
    before = request.args.get("before") or date.today().isoformat()
    start_d, end_d = "1970-01-01", before
    df, _, _ = fetch_jobs_with_fallbacks(
        start_d, end_d, username=current_user.username)
    df = compute_costs(df)
    out = io.StringIO()
    df.to_csv(out, index=False)
    out.seek(0)
    filename = f"usage_{current_user.username}_{start_d}_{end_d}.csv"
    CSV_DOWNLOADS.labels(kind="user_usage").inc()
    return Response(out.read(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@user_bp.get("/me/receipts/<int:rid>.pdf")
@login_required
def receipt_pdf(rid: int):
    rec, items = get_receipt_with_items(rid)
    is_admin = getattr(current_user, "is_admin", False)
    if not rec or (rec["username"] != current_user.username and not is_admin):
        audit("receipt.pdf.denied", target=f"receipt={rid}", status=403,
              extra={"actor": current_user.username})
        return redirect(url_for("user.my_receipts"))

    html = render_template(
        "invoices/invoice.html",
        r=rec,
        rows=items,
        org=ORG_INFO(),   # see helper below
        DISPLAY_TZ=APP_TZ,
    )
    pdf = HTML(string=html, base_url=current_app.static_folder).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename=invoice_{rec["id"]}.pdf'
    return resp


@user_bp.get("/me/receipts/<int:rid>.th.pdf")
@login_required
def receipt_pdf_th(rid: int):
    rec, items = get_receipt_with_items(rid)
    is_admin = getattr(current_user, "is_admin", False)
    if not rec or (rec["username"] != current_user.username and not is_admin):
        audit("receipt_th.pdf.denied", target=f"receipt={rid}", status=403,
              extra={"actor": current_user.username})
        return redirect(url_for("user.my_receipts"))

    html = render_template(
        "invoices/invoice_th.html",  # your Thai template
        r=rec,
        rows=items,
        org=ORG_INFO_TH(),           # Thai-preferred org labels
        DISPLAY_TZ=APP_TZ,
    )
    pdf = HTML(string=html, base_url=current_app.static_folder).write_pdf()
    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename=invoice_{rec["id"]}_th.pdf'
    return resp
