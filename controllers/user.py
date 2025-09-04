# user_ui.py
import pandas as pd
from models.billing_store import list_receipts, get_receipt_with_items
from flask import Blueprint, render_template, request, render_template_string, Response, url_for
from flask_login import login_required, current_user
from datetime import date, timedelta
import io
from services.ui_base import nav as render_nav
from services.data_sources import fetch_jobs_with_fallbacks
# your existing function that adds CPU_Core_Hours, GPU_Hours, Mem_GB_Hours, tier, Cost (฿)
from services.billing import compute_costs
from models.billing_store import billed_job_ids, canonical_job_id
from flask import flash, redirect
from models.billing_store import create_receipt_from_rows
# add to imports at top
from models.billing_store import list_billed_items_for_user


user_bp = Blueprint("user", __name__)


@user_bp.get("/me/receipts")
@login_required
def my_receipts():
    return render_template(
        "user/receipts.html",
        NAV=render_nav("usage"),
        receipts=list_receipts(current_user.username)
    )


@user_bp.get("/me/receipts/<int:rid>")
@login_required
def view_receipt(rid: int):
    rec, items = get_receipt_with_items(rid)
    if not rec or rec.get("username") != current_user.username:
        return redirect(url_for("user.my_receipts"))
    # very simple detail view
    rows = items
    return render_template("user/receipt_detail.html", NAV=render_nav("usage"), r=rec, rows=rows)


@user_bp.get("/me")
@login_required
def my_usage():
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

    try:
        if view in {"detail", "aggregate"}:
            df, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username)
            df = compute_costs(df)

            # Enforce End cutoff defensively
            if "End" in df.columns:
                df["End"] = pd.to_datetime(df["End"], errors="coerce")
                cutoff = pd.to_datetime(
                    end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df = df[df["End"].notna() & (df["End"] <= cutoff)]

            # Hide jobs that are already billed/pending
            df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
            already = billed_job_ids()
            df = df[~df["JobKey"].isin(already)]

            # detailed rows
            cols = ["JobID", "Elapsed", "TotalCPU", "ReqTRES", "State",
                    "CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours", "tier", "Cost (฿)"]
            for c in cols:
                if c not in df.columns:
                    df[c] = ""
            rows = df[cols].to_dict(orient="records")
            total_cost = float(df["Cost (฿)"].sum()) if not df.empty else 0.0

            # aggregate (single row)
            if not df.empty:
                agg_row = {
                    "user": current_user.username,
                    "tier": (df["tier"].mode()[0] if not df["tier"].mode().empty else ""),
                    "jobs": int(len(df)),
                    "CPU_Core_Hours": float(df["CPU_Core_Hours"].sum()),
                    "GPU_Hours": float(df["GPU_Hours"].sum()),
                    "Mem_GB_Hours": float(df["Mem_GB_Hours"].sum()),
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
        NAV=render_nav("usage"),
        current_user=current_user,
        start=start_d, end=end_d, view=view, before=before,
        rows=rows,                 # detailed
        agg_rows=agg_rows,         # aggregate
        pending=pending, paid=paid, sum_pending=sum_pending, sum_paid=sum_paid,  # billed
        data_source=data_source, notes=notes,
        total_cost=total_cost,
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
        return redirect(url_for("user.my_usage", start=start_d, end=end_d))

    rid, total, skipped = create_receipt_from_rows(
        current_user.username, start_d, end_d, df.to_dict(orient="records"))
    msg = f"Created receipt #{rid} for ฿{total:.2f}"
    if skipped:
        msg += f" (skipped {len(skipped)} already billed job(s))"
    # flash(msg)
    return redirect(url_for("user.my_receipts"))
