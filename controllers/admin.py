from flask import jsonify
from datetime import timedelta
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

    # NEW: optional partial-user filter (case-insensitive)
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

    def _ensure_col(df, name, default_val=""):
        """Return a Series for df[name]; if missing, return a same-length Series of default_val."""
        if name in df.columns:
            return df[name]
        return pd.Series([default_val] * len(df), index=df.index)

    # ---- DASHBOARD ----
    if section == "dashboard":
        # small helper: run a callable; on error, append a note and return default
        def cap(name, fn, default):
            try:
                return fn()
            except Exception as e:
                notes.append(f"dashboard.{name}: {e!s}")
                return default

        # window
        end_d = before
        start_d = (date.fromisoformat(before) - timedelta(days=90)).isoformat()

        # fetch + cost
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
                cutoff_utc = pd.Timestamp(
                    end_d, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
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
            lambda: float(_ensure_col(df_unbilled, "Cost (฿)", 0).sum()
                          ) if not df_unbilled.empty else 0.0,
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
                    and pd.to_datetime(r.get("paid_at"), errors="coerce", utc=True)
                    >= (pd.Timestamp(end_d, tz="UTC") - pd.Timedelta(days=30))
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

        # --- Node “Top N” series + keep old top-1 chips ---
        try:
            top_k = min(max(int(request.args.get("nodes_top", 3)), 1), 10)

            node_kpi = {}
            if not df.empty and "NodeList" in df.columns:
                from services.data_sources import expand_nodelist
                df_nodes = df.copy()
                df_nodes["__nodes"] = (
                    df_nodes["NodeList"].fillna("").map(expand_nodelist)
                )
                df_nodes = df_nodes.explode("__nodes")
                df_nodes = df_nodes[df_nodes["__nodes"].notna() & (
                    df_nodes["__nodes"] != "")]

                if not df_nodes.empty:
                    # numeric safety
                    for col in ("CPU_Core_Hours", "GPU_Hours"):
                        if col in df_nodes.columns:
                            df_nodes[col] = pd.to_numeric(
                                df_nodes[col], errors="coerce").fillna(0.0)
                        else:
                            df_nodes[col] = 0.0

                    # A) by unique jobs
                    jobs_count = (
                        df_nodes.groupby("__nodes")["JobID"].nunique()
                        .sort_values(ascending=False)
                    )
                    if not jobs_count.empty:
                        top_jobs = jobs_count.head(top_k)
                        series["node_jobs_labels"] = list(top_jobs.index)
                        series["node_jobs_values"] = [
                            int(v) for v in top_jobs.values]
                        # chip (top-1)
                        node_kpi["most_used_by_jobs"] = {
                            "node": jobs_count.index[0], "jobs": int(jobs_count.iloc[0])}

                    # B) by CPU core-hours
                    cpu_sum = (
                        df_nodes.groupby("__nodes")["CPU_Core_Hours"].sum()
                        .sort_values(ascending=False)
                    )
                    if not cpu_sum.empty:
                        top_cpu = cpu_sum.head(top_k)
                        series["node_cpu_labels"] = list(top_cpu.index)
                        series["node_cpu_values"] = [
                            round(float(v), 2) for v in top_cpu.values]
                        node_kpi["most_used_by_cpu_core_hours"] = {
                            "node": cpu_sum.index[0], "core_hours": float(round(cpu_sum.iloc[0], 2))}

                    # C) by GPU hours (if any)
                    gpu_sum = (
                        df_nodes.groupby("__nodes")["GPU_Hours"].sum()
                        .sort_values(ascending=False)
                    )
                    if gpu_sum.sum() > 0:
                        top_gpu = gpu_sum.head(top_k)
                        series["node_gpu_labels"] = list(top_gpu.index)
                        series["node_gpu_values"] = [
                            round(float(v), 2) for v in top_gpu.values]
                        node_kpi["most_used_by_gpu_hours"] = {
                            "node": gpu_sum.index[0], "gpu_hours": float(round(gpu_sum.iloc[0], 2))}

            kpis["nodes"] = node_kpi
        except Exception as e:
            notes.append(f"node_kpi: {e}")

        # --- Energy & Throughput series (NEW) ---
        try:
            # Energy per user (top 10)
            def _energy_user():
                if df.empty or "Energy_kJ" not in df.columns or "User" not in df.columns:
                    return ([], [])
                s = df.groupby("User")["Energy_kJ"].sum(
                ).sort_values(ascending=False).head(10)
                return list(s.index), [round(float(v), 2) for v in s.values]

            series["energy_user_labels"], series["energy_user_values"] = cap(
                "series.energy_user", _energy_user, ([], [])
            )

            # Energy efficiency per user: kJ per CPU-hour (ratio-of-sums)
            def _energy_eff_user():
                if df.empty or "Energy_kJ" not in df.columns or "CPU_Core_Hours" not in df.columns:
                    return ([], [])
                g = df.groupby("User").agg(energy=("Energy_kJ", "sum"),
                                           cpu=("CPU_Core_Hours", "sum"))
                g = g[g["cpu"] > 0]
                eff = (g["energy"] / g["cpu"]
                       ).sort_values(ascending=False).head(10)
                return list(eff.index), [round(float(v), 3) for v in eff.values]

            series["energy_eff_user_labels"], series["energy_eff_user_values"] = cap(
                "series.energy_eff_user", _energy_eff_user, ([], [])
            )

            # Energy per node (top 10) — reuse exploded df_nodes if you created it; else build locally
            def _energy_node_top():
                if df.empty or "NodeList" not in df.columns or "Energy_kJ" not in df.columns:
                    return ([], [])
                from services.data_sources import expand_nodelist
                d = df.copy()
                d["__nodes"] = d["NodeList"].fillna("").map(expand_nodelist)
                d = d.explode("__nodes")
                d = d[d["__nodes"].notna() & (d["__nodes"] != "")]
                if d.empty:
                    return ([], [])
                d["Energy_kJ"] = pd.to_numeric(
                    d["Energy_kJ"], errors="coerce").fillna(0.0)
                s = d.groupby("__nodes")["Energy_kJ"].sum(
                ).sort_values(ascending=False).head(10)
                return list(s.index), [round(float(v), 2) for v in s.values]

            series["energy_node_labels"], series["energy_node_values"] = cap(
                "series.energy_node", _energy_node_top, ([], [])
            )

            # ---- Throughput / reliability ----

            def _succ_fail_by(col: str):
                if df.empty or col not in df.columns or "State" not in df.columns:
                    return ([], [], [])
                g = (
                    df.groupby([col, "State"])["JobID"]
                    .nunique()
                    .unstack(fill_value=0)
                )
                success = g["COMPLETED"] if "COMPLETED" in g.columns else (
                    g.sum(axis=1) * 0)
                total = g.sum(axis=1)
                fail = (total - success).astype(int)
                # focus on busiest groups
                top_idx = total.sort_values(ascending=False).head(10).index
                return (
                    list(top_idx),
                    [int(success.loc[i]) for i in top_idx],
                    [int(fail.loc[i]) for i in top_idx],
                )

            series["succ_user_labels"], series["succ_user_success"], series["succ_user_fail"] = cap(
                "series.succ_user", lambda: _succ_fail_by("User"), ([], [], [])
            )
            series["succ_part_labels"], series["succ_part_success"], series["succ_part_fail"] = cap(
                "series.succ_part", lambda: _succ_fail_by(
                    "Partition"), ([], [], [])
            )
            series["succ_qos_labels"], series["succ_qos_success"], series["succ_qos_fail"] = cap(
                "series.succ_qos", lambda: _succ_fail_by("QOS"), ([], [], [])
            )

            # Top failure exit codes (use DerivedExitCode then ExitCode), only for non-COMPLETED
            def _fail_exit_top():
                if "State" not in df.columns:
                    return ([], [])
                codes = df.get("DerivedExitCode")
                if codes is None or codes.empty:
                    codes = df.get("ExitCode")
                codes = codes.fillna("")
                mask = df["State"].fillna("").ne("COMPLETED")
                c = codes[mask].replace("", pd.NA).dropna()
                if c.empty:
                    return ([], [])
                s = c.value_counts().head(10)
                return list(s.index), [int(v) for v in s.values]

            series["fail_exit_labels"], series["fail_exit_values"] = cap(
                "series.fail_exit", _fail_exit_top, ([], [])
            )

            # Failure reason share: PREEMPTED / TIMEOUT / OTHER_FAILS (counts)
            def _fail_state_share():
                if "State" not in df.columns:
                    return ([], [])
                s = df["State"].fillna("")
                pre = int((s == "PREEMPTED").sum())
                tout = int((s == "TIMEOUT").sum())
                non_ok = int((s != "COMPLETED").sum())
                other = max(non_ok - pre - tout, 0)
                return (["PREEMPTED", "TIMEOUT", "OTHER_FAILS"], [pre, tout, other])

            series["fail_state_labels"], series["fail_state_values"] = cap(
                "series.fail_states", _fail_state_share, ([], [])
            )

            def _hide_if_all_zero(labels_key, values_key):
                vals = series.get(values_key, [])
                if not vals or sum(float(v or 0) for v in vals) == 0:
                    series[labels_key], series[values_key] = [], []

            _hide_if_all_zero("energy_user_labels", "energy_user_values")
            _hide_if_all_zero("energy_eff_user_labels",
                              "energy_eff_user_values")
            _hide_if_all_zero("energy_node_labels", "energy_node_values")

        except Exception as e:
            notes.append(f"energy_throughput_series: {e}")

        return render_template(
            "admin/dashboard.html",
            current_user=current_user,
            before=before, start=start_d, end=end_d,
            kpis=kpis, series=series, data_source=data_source, notes=notes,
            tot_cpu=tot_cpu, tot_gpu=tot_gpu, tot_mem=tot_mem, tot_elapsed=tot_elapsed,
            url_for=url_for, all_rates=rates,
        )

    try:
        if section == "usage":
            # -------- fetch RAW (parents + steps) --------
            df_raw, data_source, notes = fetch_jobs_with_fallbacks(
                start_d, end_d)

            if not df_raw.empty:
                if "End" in df_raw.columns:
                    end_series = pd.to_datetime(
                        df_raw["End"], errors="coerce", utc=True)
                    cutoff_utc = pd.Timestamp(end_d, tz="UTC") + \
                        pd.Timedelta(hours=23, minutes=59, seconds=59)
                    df_raw = df_raw[end_series.notna() & (
                        end_series <= cutoff_utc)]
                    df_raw["End"] = end_series

                # NEW: build the complete user list BEFORE applying q filter
                if "User" in df_raw.columns:
                    all_users = sorted(
                        u for u in df_raw["User"].astype(str).fillna("").str.strip().unique()
                        if u
                    )

                # filter by partial username (parent-based) AFTER collecting all_users
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

                # raw table AFTER filtering
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
        current=rates.get(tier, {"cpu": 0, "gpu": 0, "memory": 0}),
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
        start_d, end_d, username=current_user.username
    )
    df = compute_costs(df)

    # UTC-aware cutoff to match the rest of the app
    if "End" in df.columns:
        end_series = pd.to_datetime(df["End"], errors="coerce", utc=True)
        cutoff_utc = pd.Timestamp(end_d, tz="UTC") + \
            pd.Timedelta(hours=23, minutes=59, seconds=59)
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


@admin_bp.get("/admin/simulate_rates.json")
@login_required
@admin_required
def simulate_rates_json():
    """
    Read-only: fetch last 90 days, compute usage with compute_costs(),
    build components, then simulate current vs candidate rates.
    Query params (optional): cpu_mu, gpu_mu, mem_mu, cpu_gov, ... cpu_private, ...
      Example: /admin/simulate_rates.json?cpu_mu=1&gpu_mu=6&mem_mu=0.5
    """
    try:
        # Window: last 90 days up to ?before=YYYY-MM-DD (default today)
        before = (request.args.get("before")
                  or date.today().isoformat()).strip()
        start_d = (date.fromisoformat(before) - timedelta(days=90)).isoformat()
        end_d = before

        # Fetch + compute with live logic (unchanged)
        raw_df, data_source, _ = fetch_jobs_with_fallbacks(start_d, end_d)
        costed = compute_costs(raw_df)

        # Cutoff at local day end (same as dashboard)
        if "End" in costed.columns:
            end_series = pd.to_datetime(
                costed["End"], errors="coerce", utc=True)
            cutoff_utc = pd.Timestamp(
                end_d, tz="UTC") + pd.Timedelta(hours=23, minutes=59, seconds=59)
            costed = costed[end_series.notna() & (
                end_series <= cutoff_utc)].copy()
            costed["End"] = end_series

        # Build read-only components
        comps = build_pricing_components(costed)

        # Current DB rates
        current_rates = rates_store.load_rates()

        # Candidate rates from query params; fallback to current if missing
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
