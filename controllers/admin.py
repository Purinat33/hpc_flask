# admin_ui.py
from flask import Blueprint, render_template, request, render_template_string, redirect, url_for, flash
from flask_login import login_required, current_user
import pandas as pd
from controllers.auth import admin_required
from models.rates_store import load_rates, save_rates
from data_sources import fetch_jobs_with_fallbacks
from billing import compute_costs
from datetime import date, timedelta
from ui_base import nav as render_nav
from models.billing_store import billed_job_ids, canonical_job_id
from models.billing_store import admin_list_receipts, mark_receipt_paid, paid_receipts_csv
from flask import Response

admin_bp = Blueprint("admin", __name__)


@admin_bp.get("/admin")
@login_required
@admin_required
def admin_form():
    rates = load_rates()

    section = (request.args.get("section") or "usage").lower()
    if section not in {"rates", "usage", "billing"}:
        section = "usage"

    tier = (request.args.get("type") or "mu").lower()
    if tier not in rates:
        tier = "mu"

    view = (request.args.get("view") or "detail").lower()
    if view not in {"detail", "aggregate"}:
        view = "detail"

    EPOCH_START = "1970-01-01"
    before = request.args.get("before") or date.today().isoformat()
    start_d = EPOCH_START
    end_d = before

    # defaults
    rows, agg_rows = [], []
    grand_total = 0.0
    data_source = None
    notes = []
    tot_cpu = tot_gpu = tot_mem = tot_elapsed = 0.0
    pending = paid = []

    try:
        if section == "usage":
            df, data_source, notes = fetch_jobs_with_fallbacks(start_d, end_d)
            df = compute_costs(df)

            # Enforce End cutoff defensively
            if "End" in df.columns:
                df["End"] = pd.to_datetime(df["End"], errors="coerce")
                cutoff = pd.to_datetime(
                    end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df = df[df["End"].notna() & (df["End"] <= cutoff)]

            if not df.empty:
                tot_cpu = float(df["CPU_Core_Hours"].sum())
                tot_gpu = float(df["GPU_Hours"].sum())
                tot_mem = float(df["Mem_GB_Hours"].sum())
                tot_elapsed = float(df.get("Elapsed_Hours", 0).sum())

            cols = ["User", "JobID", "Elapsed", "TotalCPU", "ReqTRES", "State",
                    "CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours", "tier", "Cost (฿)"]
            for c in cols:
                if c not in df.columns:
                    df[c] = ""
            rows = df[cols].to_dict(orient="records")

            if not df.empty:
                agg = (
                    df.groupby(["User", "tier"], dropna=False)
                    .agg(
                        jobs=("JobID", "count"),
                        CPU_Core_Hours=("CPU_Core_Hours", "sum"),
                        GPU_Hours=("GPU_Hours", "sum"),
                        Mem_GB_Hours=("Mem_GB_Hours", "sum"),
                        Cost=("Cost (฿)", "sum"),
                    ).reset_index()
                )
                agg.rename(columns={"Cost": "Cost (฿)"}, inplace=True)
                agg_rows = agg[["User", "tier", "jobs", "CPU_Core_Hours",
                                "GPU_Hours", "Mem_GB_Hours", "Cost (฿)"]].to_dict(orient="records")
                grand_total = float(agg["Cost (฿)"].sum())

        elif section == "billing":
            pending = admin_list_receipts(status="pending")
            paid = admin_list_receipts(status="paid")

    except Exception as e:
        notes.append(str(e))

    return render_template(
        'admin/page.html',
        NAV=render_nav("usage"),
        section=section,
        all_rates=rates, current=rates.get(tier, {"cpu": 0, "gpu": 0, "mem": 0}), tier=tier, tiers=["mu", "gov", "private"],
        current_user=current_user,
        start=start_d, end=end_d, view=view, before=before,
        rows=rows, agg_rows=agg_rows, grand_total=grand_total,
        data_source=data_source, notes=notes,
        tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
        pending=pending, paid=paid,
        url_for=url_for
    )


@admin_bp.post("/admin")
@login_required
@admin_required
def admin_update():
    panel = (request.form.get("panel") or "rates").lower()
    tier = (request.form.get("type") or "").lower()
    try:
        cpu = float(request.form.get("cpu", "0"))
        gpu = float(request.form.get("gpu", "0"))
        mem = float(request.form.get("mem", "0"))
    except Exception:
        # flash("Invalid numeric input")
        return redirect(url_for("admin.admin_form", panel=panel, type=tier or "mu"))
    if tier not in {"mu", "gov", "private"}:
        # flash("Type must be one of mu|gov|private")
        return redirect(url_for("admin.admin_form", panel=panel))
    if min(cpu, gpu, mem) < 0:
        # flash("Rates must be ≥ 0")
        return redirect(url_for("admin.admin_form", panel=panel, type=tier))

    rates = load_rates()
    rates[tier] = {"cpu": cpu, "gpu": gpu, "mem": mem}
    save_rates(rates)
    # flash(f"Updated {tier} → {rates[tier]}")

    # stay on current panel; keep date range if you were on usage (not needed here)
    return redirect(url_for("admin.admin_form", panel=panel, type=tier))


@admin_bp.post("/admin/receipts/<int:rid>/paid")
@login_required
@admin_required
def mark_paid(rid: int):
    ok = mark_receipt_paid(rid, current_user.username)
    if not ok:
        # flash(f"Receipt #{rid} not found.")
        print("Receipt not found")
    else:
        # flash(f"Receipt #{rid} marked as paid.")
        print("Receipt found and marked as paid")

    return redirect(url_for("admin.admin_form", section="billing"))


@admin_bp.get("/admin/paid.csv")
@login_required
@admin_required
def paid_csv():
    fname, csv_text = paid_receipts_csv()
    return Response(csv_text, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})
