# user_ui.py
import pandas as pd
from models.billing_store import list_receipts, get_receipt_with_items
from flask import Blueprint, render_template, request, Response, url_for
from flask_login import login_required, current_user
from datetime import date, timedelta
import io
from services.data_sources import fetch_jobs_with_fallbacks
# your existing function that adds CPU_Core_Hours, GPU_Hours, Mem_GB_Hours, tier, Cost (฿)
from services.billing import compute_costs
from models.billing_store import billed_job_ids, canonical_job_id
from flask import flash, redirect
from models.billing_store import create_receipt_from_rows
# add to imports at top
from models.billing_store import list_billed_items_for_user
from models.audit_store import audit
from services.metrics import CSV_DOWNLOADS, RECEIPT_CREATED

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

    # pass ownership to the template so it can hide the Pay button for non-owners
    return render_template("user/receipt_detail.html", r=rec, rows=items,
                           is_owner=(rec.get("username") == current_user.username))


@user_bp.get("/me")
@login_required
def my_usage():
    if getattr(current_user, "is_admin", False):
        return redirect(url_for("admin.admin_form"))
    EPOCH_START = "1970-01-01"
    before = request.args.get("before") or date.today().isoformat()
    start_d = EPOCH_START
    end_d = before

    view = (request.args.get("view") or "detail").lower()
    if view not in {"detail", "aggregate", "billed"}:
        view = "detail"

    rows, agg_rows = [], []
    data_source = None
    notes = []
    total_cost = 0.0
    pending = paid = []
    sum_pending = sum_paid = 0.0

    raw_cols: list[str] = []
    raw_rows: list[dict] = []
    header_classes: dict[str, str] = {}

    try:
        if view in {"detail", "aggregate"}:
            # RAW fetch
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username)
            if not df_raw.empty and "End" in df_raw.columns:
                df_raw["End"] = pd.to_datetime(df_raw["End"], errors="coerce")
                cutoff = pd.to_datetime(
                    end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df_raw = df_raw[df_raw["End"].notna() & (
                    df_raw["End"] <= cutoff)]
            raw_cols = list(df_raw.columns) if not df_raw.empty else []
            raw_rows = df_raw.head(200).to_dict(
                orient="records") if not df_raw.empty else []

            # header style mapping for RAW table
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

            # Computed (parent-aggregated)
            df = compute_costs(df_raw.copy())

            # Hide jobs that are already billed/pending
            if not df.empty:
                df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
                already = billed_job_ids()
                df = df[~df["JobKey"].isin(already)]

            # detailed rows
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
                total_cost = float(df["Cost (฿)"].sum()
                                   ) if not df.empty else 0.0

            # aggregate (single row)
            if view == "aggregate" and not df.empty:
                agg_row = {
                    "user": current_user.username,
                    "tier": (df["tier"].mode()[0] if not df["tier"].mode().empty else ""),
                    "jobs": int(len(df)),
                    "CPU_Core_Hours": float(df["CPU_Core_Hours"].sum()),
                    "GPU_Hours": float(df["GPU_Hours"].sum()),
                    "Mem_GB_Hours_Used": float(df["Mem_GB_Hours_Used"].sum()),
                    "Cost (฿)": float(df["Cost (฿)"].sum()),
                }
                agg_rows = [agg_row]

        else:  # view == 'billed'
            pending = list_billed_items_for_user(
                current_user.username, "pending")
            paid = list_billed_items_for_user(current_user.username, "paid")
            sum_pending = float(sum(i["cost"]
                                for i in pending)) if pending else 0.0
            sum_paid = float(sum(i["cost"] for i in paid)) if paid else 0.0

    except Exception as e:
        notes.append(str(e))

    return render_template(
        "user/usage.html",
        current_user=current_user,
        start=start_d, end=end_d, view=view, before=before,
        rows=rows,                 # detailed
        agg_rows=agg_rows,         # aggregate
        pending=pending, paid=paid, sum_pending=sum_pending, sum_paid=sum_paid,  # billed
        data_source=data_source, notes=notes,
        total_cost=total_cost,
        # NEW: raw table + header styles
        raw_cols=raw_cols, raw_rows=raw_rows, header_classes=header_classes,
        url_for=url_for
    )


@user_bp.get("/me.csv")
@login_required
def my_usage_csv():

    end_d = request.args.get("end") or date.today().isoformat()
    start_d = request.args.get("start") or (
        date.today() - timedelta(days=7)).isoformat()

    before = request.args.get("before") or date.today().isoformat()
    start_d = "1970-01-01"
    end_d = before
    # fetch -> compute_costs -> output CSV (unchanged)

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
    end_d = request.form.get("end") or date.today().isoformat()
    start_d = request.form.get("start") or (
        date.today() - timedelta(days=7)).isoformat()
    before = request.form.get("before") or date.today().isoformat()
    start_d = "1970-01-01"
    end_d = before
    # re-fetch -> compute_costs -> filter out billed -> create_receipt_from_rows(username, start_d, end_d, ...)

    # Re-fetch, recompute, hide billed (server-side safety)
    df, _, _ = fetch_jobs_with_fallbacks(
        start_d, end_d, username=current_user.username)
    df = compute_costs(df)

    # Defensively enforce the "before" cutoff here too
    if "End" in df.columns:
        df["End"] = pd.to_datetime(df["End"], errors="coerce")
        cutoff = pd.to_datetime(
            end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        df = df[df["End"].notna() & (df["End"] <= cutoff)]

    df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
    df = df[~df["JobKey"].isin(billed_job_ids())]

    if df.empty:
        # flash("No unbilled jobs to create a receipt from.")
        audit(
            action="receipt.create.noop",
            target=f"user={current_user.username}",
            status=204,
            extra={"start": start_d, "end": end_d,
                   "reason": "No unbilled jobs"}
        )
        return redirect(url_for("user.my_usage", start=start_d, end=end_d))

    rid, total, items = create_receipt_from_rows(
        current_user.username, start_d, end_d, df.to_dict(orient="records"))
    msg = f"Created receipt #{rid} for ฿{total:.2f}"
    if items:
        msg += f" (Add {len(items)} to billed job(s))"
    # flash(msg)
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
