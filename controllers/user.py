# user_ui.py
import pandas as pd
from models.billing_store import list_receipts, get_receipt_with_items
from flask import Blueprint, render_template, request, Response, url_for
from flask_login import login_required, current_user
from datetime import date
import io
from services.data_sources import fetch_jobs_with_fallbacks
from services.billing import compute_costs
from models.billing_store import billed_job_ids, canonical_job_id
from flask import redirect
from models.billing_store import create_receipt_from_rows
from models.billing_store import list_billed_items_for_user
from models.audit_store import audit
from services.metrics import CSV_DOWNLOADS, RECEIPT_CREATED

user_bp = Blueprint("user", __name__)


def _to_utc_day_end(ts_date: str) -> pd.Timestamp:
    return pd.Timestamp(ts_date, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)


def _ensure_num(df: pd.DataFrame, name: str, default=0.0) -> pd.Series:
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
    if df.empty or "End" not in df.columns:
        return [], 0.0
    for c in ["CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours_Used", "Cost (฿)"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        else:
            df[c] = 0.0
    df["_month"] = df["End"].dt.month
    g = (
        df.groupby("_month")
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


@user_bp.get("/me/receipts")
@login_required
def my_receipts():
    return render_template("user/receipts.html", receipts=list_receipts(current_user.username))


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
    if getattr(current_user, "is_admin", False):
        # Admins land on admin UI
        return redirect(url_for("admin.admin_form"))

    EPOCH_START = "1970-01-01"
    before = request.args.get("before") or date.today().isoformat()
    start_d, end_d = EPOCH_START, before

    view = (request.args.get("view") or "detail").lower()
    # add 'trend' and 'billed'
    if view not in {"detail", "aggregate", "billed", "trend"}:
        view = "detail"

    # trend params
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

    rows, agg_rows = [], []
    data_source = None
    notes: list[str] = []
    total_cost = 0.0
    pending: list[dict] = []
    paid: list[dict] = []
    sum_pending = sum_paid = 0.0

    raw_cols: list[str] = []
    raw_rows: list[dict] = []
    header_classes: dict[str, str] = {}

    try:
        if view in {"detail", "aggregate"}:
            # RAW fetch (self)
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

            # Computed (parents)
            df = compute_costs(
                df_raw.copy() if df_raw is not None else pd.DataFrame())

            # Hide already billed
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
                total_cost = float(_ensure_num(df, "Cost (฿)", 0).sum())

            if view == "aggregate" and not df.empty:
                agg_row = {
                    "jobs": int(len(df)),
                    "CPU_Core_Hours": float(_ensure_num(df, "CPU_Core_Hours", 0).sum()),
                    "GPU_Hours": float(_ensure_num(df, "GPU_Hours", 0).sum()),
                    "Mem_GB_Hours_Used": float(_ensure_num(df, "Mem_GB_Hours_Used", 0).sum()),
                    "Cost (฿)": float(_ensure_num(df, "Cost (฿)", 0).sum()),
                }
                agg_rows = [agg_row]

        elif view == "billed":
            pending = list_billed_items_for_user(
                current_user.username, "pending")
            paid = list_billed_items_for_user(current_user.username, "paid")
            sum_pending = float(sum(i["cost"]
                                for i in pending)) if pending else 0.0
            sum_paid = float(sum(i["cost"] for i in paid)) if paid else 0.0

        elif view == "trend":
            # YTD or full year if past year; include ALL jobs (billed & unbilled) for accuracy
            ystart, yend = _month_range_for_year(year)
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                ystart, yend, username=current_user.username)
            df = compute_costs(df_raw)

            if "End" in df.columns:
                end_series = pd.to_datetime(
                    df["End"], errors="coerce", utc=True)
                cutoff_utc = _to_utc_day_end(yend)
                df = df[end_series.notna() & (end_series <= cutoff_utc)].copy()
                df["End"] = end_series

            monthly_agg, year_total = _monthly_aggregate(df)

            month_detail_rows = []
            tot_cpu_m = tot_gpu_m = tot_mem_m = month_total = 0.0
            if month:
                d_m = df[df["End"].dt.month == int(month)].copy()
                for c in ["CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours_Used", "Cost (฿)"]:
                    if c in d_m.columns:
                        d_m[c] = pd.to_numeric(
                            d_m[c], errors="coerce").fillna(0.0)
                    else:
                        d_m[c] = 0.0
                if not d_m.empty:
                    tot_cpu_m = float(d_m["CPU_Core_Hours"].sum())
                    tot_gpu_m = float(d_m["GPU_Hours"].sum())
                    tot_mem_m = float(d_m["Mem_GB_Hours_Used"].sum(
                    )) if "Mem_GB_Hours_Used" in d_m.columns else 0.0
                    month_total = float(d_m["Cost (฿)"].sum())
                    cols = [
                        "JobID", "Elapsed", "End", "State",
                        "CPU_Core_Hours", "GPU_Count", "GPU_Hours",
                        "Memory_GB", "Mem_GB_Hours_Used", "Mem_GB_Hours_Alloc",
                        "tier", "Cost (฿)"
                    ]
                    for c in cols:
                        if c not in d_m.columns:
                            d_m[c] = ""
                    month_detail_rows = d_m[cols].to_dict(orient="records")

            return render_template(
                "user/usage.html",
                current_user=current_user,
                start=ystart, end=yend, view=view, before=before,
                rows=rows, agg_rows=agg_rows,
                pending=pending, paid=paid, sum_pending=sum_pending, sum_paid=sum_paid,
                data_source=data_source, notes=notes,
                total_cost=0.0,  # not used in trend
                raw_cols=[], raw_rows=[], header_classes={},
                url_for=url_for,
                # trend context
                year=year, current_year=current_year, month=month,
                monthly_agg=monthly_agg, year_total=year_total,
                month_detail_rows=month_detail_rows,
                tot_cpu_m=tot_cpu_m, tot_gpu_m=tot_gpu_m, tot_mem_m=tot_mem_m, month_total=month_total,
            )

    except Exception as e:
        notes.append(str(e))

    # fallback render (detail/aggregate/billed)
    return render_template(
        "user/usage.html",
        current_user=current_user,
        start=start_d, end=end_d, view=view, before=before,
        rows=rows,
        agg_rows=agg_rows,
        pending=pending, paid=paid, sum_pending=sum_pending, sum_paid=sum_paid,
        data_source=data_source, notes=notes,
        total_cost=total_cost,
        raw_cols=raw_cols, raw_rows=raw_rows, header_classes=header_classes,
        url_for=url_for,
        # trend defaults
        year=date.today().year, current_year=date.today().year, month=None,
        monthly_agg=[], year_total=0.0,
        month_detail_rows=[], tot_cpu_m=0.0, tot_gpu_m=0.0, tot_mem_m=0.0, month_total=0.0,
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


@user_bp.post("/me/receipt")
@login_required
def create_receipt():
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
        audit(
            action="receipt.create.noop",
            target=f"user={current_user.username}",
            status=204,
            extra={"start": start_d, "end": end_d,
                   "reason": "No unbilled jobs"}
        )
        return redirect(url_for("user.my_usage", start=start_d, end=end_d, view="detail"))

    rid, total, items = create_receipt_from_rows(
        current_user.username, start_d, end_d, df.to_dict(orient="records")
    )
    audit(
        action="receipt.create",
        target=f"receipt={rid}",
        status=200,
        extra={
            "user": current_user.username,
            "start": start_d,
            "end": end_d,
            "jobs": int(len(df)),
            "total": float(total),
            "items": list(items) if items else [],
        },
    )
    RECEIPT_CREATED.labels(scope="user").inc()
    return redirect(url_for("user.my_receipts"))
