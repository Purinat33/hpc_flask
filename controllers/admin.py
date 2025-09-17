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

admin_bp = Blueprint("admin", __name__)


@admin_bp.get("/admin")
@login_required
@admin_required
def admin_form():
    rates = rates_store.load_rates()

    # section: rates | usage (all users) | myusage (this admin) | billing
    section = (request.args.get("section") or "usage").lower()
    if section not in {"rates", "usage", "billing", "myusage", "dashboard"}:
        section = "usage"

    tier = (request.args.get("type") or "mu").lower()
    if tier not in rates:
        tier = "mu"

    view = (request.args.get("view") or "detail").lower()
    if section == "myusage":
        if view not in {"detail", "aggregate", "billed"}:
            view = "detail"
    else:
        if view not in {"detail", "aggregate"}:
            view = "detail"

    EPOCH_START = "1970-01-01"
    before = request.args.get("before") or date.today().isoformat()
    start_d = EPOCH_START
    end_d = before

    # defaults
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

    # for the raw (unaggregated) fetched data preview
    raw_cols: list[str] = []
    raw_rows: list[dict] = []
    # style classes for header highlighting
    header_classes: dict[str, str] = {}

    # Dedicated section for dashboard
    kpis = {}

    series = {"daily_cost": [], "daily_labels": [], "tier_labels": [],
              "tier_values": [], "top_users_labels": [], "top_users_values": []}

    try:
        if section == "dashboard":
            # Window: last 90 days ending at `before`
            end_d = before
            start_d = (date.fromisoformat(before) -
                       timedelta(days=90)).isoformat()

            # Pull jobs (all users), compute costs
            df, data_source, notes = fetch_jobs_with_fallbacks(start_d, end_d)
            df = compute_costs(df)

            # Enforce End cutoff (defensive, same as other sections)
            if "End" in df.columns:
                df["End"] = pd.to_datetime(df["End"], errors="coerce")
                cutoff = pd.to_datetime(
                    end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
                df = df[df["End"].notna() & (df["End"] <= cutoff)]

            # Build UNBILLED view (exclude already-billed job_keys)
            df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
            already = billed_job_ids()
            df_unbilled = df[~df["JobKey"].isin(already)].copy()

            # KPIs
            # 1) Unbilled cost right now
            kpis["unbilled_cost"] = float(
                df_unbilled["Cost (฿)"].sum()) if not df_unbilled.empty else 0.0

            # 2) Pending receivables (sum of pending receipts)
            pending = admin_list_receipts(status="pending")
            kpis["pending_receivables"] = float(
                sum(float(r["total"]) for r in pending)) if pending else 0.0

            # 3) Paid in last 30 days (by paid_at)
            paid = admin_list_receipts(status="paid")
            paid_30 = 0.0
            if paid:
                cutoff_30 = pd.to_datetime(end_d) - pd.Timedelta(days=30)
                for r in paid:
                    ts = pd.to_datetime(r.get("paid_at"), errors="coerce")
                    if pd.notna(ts) and ts >= cutoff_30:
                        paid_30 += float(r["total"])
            kpis["paid_last_30d"] = float(paid_30)

            # 4) Jobs in last 30 days (all usage)
            jobs_30d = 0
            if not df.empty:
                cutoff_30 = pd.to_datetime(end_d) - pd.Timedelta(days=30)
                jobs_30d = int((df["End"] >= cutoff_30).sum())
            kpis["jobs_last_30d"] = jobs_30d

            # Time-series: daily total cost (all usage) over window
            if not df.empty:
                daily = (
                    df.groupby(df["End"].dt.date)["Cost (฿)"]
                    .sum()
                    .sort_index()
                )
                series["daily_labels"] = [d.isoformat() for d in daily.index]
                series["daily_cost"] = [round(float(v), 2)
                                        for v in daily.values]

                # Mix by tier (pie)
                tier = df.groupby("tier", dropna=False)[
                    "Cost (฿)"].sum().sort_values(ascending=False)
                series["tier_labels"] = [str(i).upper() for i in tier.index]
                series["tier_values"] = [
                    round(float(v), 2) for v in tier.values]

                # Top users by total cost (bar)
                top = (
                    df.groupby("User")["Cost (฿)"]
                    .sum()
                    .sort_values(ascending=False)
                    .head(10)
                )
                series["top_users_labels"] = list(top.index)
                series["top_users_values"] = [
                    round(float(v), 2) for v in top.values]

            # Totals chips (optional)
            tot_cpu = float(df["CPU_Core_Hours"].sum()
                            ) if "CPU_Core_Hours" in df else 0.0
            tot_gpu = float(df["GPU_Hours"].sum()
                            ) if "GPU_Hours" in df else 0.0
            tot_mem = float(df["Mem_GB_Hours"].sum()
                            ) if "Mem_GB_Hours" in df else 0.0
            tot_elapsed = float(df.get("Elapsed_Hours", 0).sum()
                                ) if "Elapsed_Hours" in df else 0.0

            # Render dedicated template
            return render_template(
                "admin/dashboard.html",
                current_user=current_user,
                before=before, start=start_d, end=end_d,
                kpis=kpis, series=series, data_source=data_source, notes=notes,
                tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
                url_for=url_for,
            )
    except Exception as e:
        notes.append(str(e))

    try:
        if section == "usage":
            # -------- fetch RAW (parents + steps) --------
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d)
            # keep a small sample for display (avoid huge tables)
            if not df_raw.empty:
                # enforce End cutoff defensively (matches normal display)
                if "End" in df_raw.columns:
                    df_raw["End"] = pd.to_datetime(
                        df_raw["End"], errors="coerce")
                    cutoff = pd.to_datetime(
                        end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
                    df_raw = df_raw[df_raw["End"].notna() & (
                        df_raw["End"] <= cutoff)]

                raw_cols = list(df_raw.columns)
                raw_rows = df_raw.head(200).to_dict(
                    orient="records")  # cap rows for UI

            # -------- compute COSTED (aggregated to parents) --------
            df = compute_costs(
                df_raw.copy() if df_raw is not None else pd.DataFrame())

            # hide already billed parents
            if not df.empty:
                df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
                already = billed_job_ids()  # set/list of all receipt_items.job_key
                df = df[~df["JobKey"].isin(already)]

            # totals
            if not df.empty:
                tot_cpu = float(df["CPU_Core_Hours"].sum())
                tot_gpu = float(df["GPU_Hours"].sum())
                # NOTE: used-mem sum (not allocated)
                tot_mem = float(df["Mem_GB_Hours_Used"].sum()
                                ) if "Mem_GB_Hours_Used" in df else 0.0
                tot_elapsed = float(
                    df.get("Elapsed_Hours", 0).sum()) if "Elapsed_Hours" in df else 0.0

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

            # -------- header highlighting map for RAW table --------
            # Primary / fallback semantics:
            # CPU: primary=TotalCPU, fb1=CPUTimeRAW, fb2=AllocTRES+ReqTRES+Elapsed
            # MEM: primary=AveRSS,    fb2=AllocTRES+ReqTRES+Elapsed
            # GPU: primary=AllocTRES, fb2=ReqTRES+Elapsed
            header_classes = {c: "" for c in raw_cols}
            for c in raw_cols:
                if c == "TotalCPU":
                    header_classes[c] = "hl-primary"
                elif c == "CPUTimeRAW":
                    header_classes[c] = "hl-fallback1"
                elif c in {"AllocTRES", "ReqTRES", "Elapsed"}:
                    # these are 2nd-level fallbacks for CPU/MEM/GPU
                    header_classes[c] = "hl-fallback2"
                elif c == "AveRSS":
                    # memory primary “used” source (on steps)
                    header_classes[c] = "hl-primary"
                # keep others default ""

        elif section == "myusage":
            # Admin's own usage (copy of /me, but lives under Admin)
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d, username=current_user.username)

            if not df_raw.empty:
                if "End" in df_raw.columns:
                    df_raw["End"] = pd.to_datetime(
                        df_raw["End"], errors="coerce")
                    cutoff = pd.to_datetime(
                        end_d) + pd.Timedelta(hours=23, minutes=59, seconds=59)
                    df_raw = df_raw[df_raw["End"].notna() & (
                        df_raw["End"] <= cutoff)]
                raw_cols = list(df_raw.columns)
                raw_rows = df_raw.head(200).to_dict(orient="records")

            df = compute_costs(
                df_raw.copy() if df_raw is not None else pd.DataFrame())

            if view in {"detail", "aggregate"}:
                # hide already billed
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

                # totals
                tot_cpu = float(df["CPU_Core_Hours"].sum()
                                ) if "CPU_Core_Hours" in df else 0.0
                tot_gpu = float(df["GPU_Hours"].sum()
                                ) if "GPU_Hours" in df else 0.0
                tot_mem = float(df["Mem_GB_Hours_Used"].sum()
                                ) if "Mem_GB_Hours_Used" in df else 0.0
                tot_elapsed = float(
                    df.get("Elapsed_Hours", 0).sum()) if "Elapsed_Hours" in df else 0.0
                grand_total = float(df["Cost (฿)"].sum(
                )) if "Cost (฿)" in df and not df.empty else 0.0

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
        current=rates.get(tier, {"cpu": 0, "gpu": 0, "mem": 0}),
        tier=tier,
        tiers=["mu", "gov", "private"],
        current_user=current_user,
        start=start_d, end=end_d, view=view, before=before,
        rows=rows, agg_rows=agg_rows, grand_total=grand_total,
        data_source=data_source, notes=notes,
        tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
        pending=pending, paid=paid,
        # myusage (billed view) context:
        my_pending_receipts=my_pending_receipts,
        my_paid_receipts=my_paid_receipts,
        sum_pending=sum_pending,
        sum_paid=sum_paid,
        # NEW: raw preview + header styles
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
