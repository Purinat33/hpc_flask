# services/pricing_sim.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Tuple, List
import pandas as pd
import numpy as np


@dataclass(frozen=True)
class RateSet:
    cpu: float
    gpu: float
    mem: float

# Helper to normalize a nested dict like:
# {"mu":{"cpu":1,"gpu":5,"mem":0.5}, "gov":{...}, "private":{...}}


def _normalize_rates(r: dict) -> Dict[str, RateSet]:
    out: Dict[str, RateSet] = {}
    for tier, v in (r or {}).items():
        out[str(tier).lower()] = RateSet(
            cpu=float(v.get("cpu", 0.0)),
            gpu=float(v.get("gpu", 0.0)),
            mem=float(v.get("mem", 0.0)),
        )
    return out


def build_pricing_components(df_costed: pd.DataFrame) -> pd.DataFrame:
    """
    From the output of services.billing.compute_costs(df):
      columns needed: 'End', 'tier', 'User',
        'CPU_Core_Hours', 'GPU_Hours', 'Mem_GB_Hours_Used'
    Returns a tidy frame with daily, per-tier, per-user sums,
    so you can apply arbitrary rates without recomputing usage.
    """
    if df_costed is None or df_costed.empty:
        return pd.DataFrame(columns=[
            "date", "tier", "User",
            "CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours_Used"
        ])

    d = df_costed.copy()

    # Make sure required cols exist
    for col in ("CPU_Core_Hours", "GPU_Hours", "Mem_GB_Hours_Used"):
        if col not in d.columns:
            d[col] = 0.0
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)

    if "tier" not in d.columns:
        d["tier"] = "mu"
    d["tier"] = d["tier"].astype(str).str.lower()

    # Coerce End to date (NaT-safe); jobs with NaT will be grouped under NaT
    if "End" in d.columns:
        end_ts = pd.to_datetime(d["End"], errors="coerce", utc=True)
        d["date"] = end_ts.dt.date
    else:
        d["date"] = pd.NaT

    # Aggregate by date/tier/user
    grp = d.groupby(["date", "tier", "User"], dropna=False).agg(
        CPU_Core_Hours=("CPU_Core_Hours", "sum"),
        GPU_Hours=("GPU_Hours", "sum"),
        Mem_GB_Hours_Used=("Mem_GB_Hours_Used", "sum"),
    ).reset_index()

    return grp


def simulate_revenue(components: pd.DataFrame, candidate_rates: dict) -> dict:
    """
    Apply candidate rates (dict of tiers -> {cpu,gpu,mem}) to the pre-aggregated
    components DataFrame from build_pricing_components().
    Returns a nested dict suitable for dashboards:
      {
        "current_like": total_thb,      # if you pass the live rates
        "sim_total": total_thb,         # with candidate rates
        "delta": diff_thb,
        "by_tier": [{"tier":"MU","thb":...}, ...],
        "by_user": [{"user":"alice","thb":...}, ...],   # top 20 by default
        "daily":   [{"date":"YYYY-MM-DD","thb":...}, ...]
      }
    """
    if components is None or components.empty:
        return {
            "current_like": 0.0,
            "sim_total": 0.0,
            "delta": 0.0,
            "by_tier": [],
            "by_user": [],
            "daily": [],
        }

    comp = components.copy()
    rates = _normalize_rates(candidate_rates)

    # Vectorized rate pick per row (default to private if missing)
    comp["_cpu_rate"] = comp["tier"].map(lambda t: rates.get(
        t, rates.get("private", RateSet(0, 0, 0))).cpu)
    comp["_gpu_rate"] = comp["tier"].map(lambda t: rates.get(
        t, rates.get("private", RateSet(0, 0, 0))).gpu)
    comp["_mem_rate"] = comp["tier"].map(lambda t: rates.get(
        t, rates.get("private", RateSet(0, 0, 0))).mem)

    # Sim THB per row
    comp["THB"] = (
        comp["CPU_Core_Hours"] * comp["_cpu_rate"]
        + comp["GPU_Hours"] * comp["_gpu_rate"]
        + comp["Mem_GB_Hours_Used"] * comp["_mem_rate"]
    )

    # Totals
    sim_total = float(comp["THB"].sum())

    # By tier
    by_tier = (
        comp.groupby("tier")["THB"].sum().sort_values(ascending=False)
        .rename_axis("tier").reset_index(name="thb")
    )
    by_tier["tier"] = by_tier["tier"].str.upper()

    # By user (top 20)
    by_user = (
        comp.groupby("User")["THB"].sum().sort_values(ascending=False)
        .head(20).rename_axis("user").reset_index(name="thb")
    )

    # Daily
    # For NaT dates, we can drop or label as "unknown"; here we drop NaT rows for charts
    daily = comp.dropna(subset=["date"]).groupby(
        "date")["THB"].sum().sort_index()
    daily_rows = [{"date": d.isoformat(), "thb": float(v)}
                  for d, v in daily.items()]

    return {
        "sim_total": float(round(sim_total, 2)),
        # The caller can pass “live” rates to also compute current_like if desired;
        # for the KPI dial you usually only need sim_total and comparisons made by caller.
        "by_tier": [{"tier": t, "thb": float(round(v, 2))} for t, v in zip(by_tier["tier"], by_tier["thb"])],
        "by_user": [{"user": u, "thb": float(round(v, 2))} for u, v in zip(by_user["user"], by_user["thb"])],
        "daily": daily_rows,
    }


def simulate_vs_current(components: pd.DataFrame, current_rates: dict, candidate_rates: dict) -> dict:
    """
    Convenience wrapper that returns current vs candidate totals and delta,
    plus the candidate breakdowns (by_tier/user/daily).
    """
    cur = simulate_revenue(components, current_rates)
    new = simulate_revenue(components, candidate_rates)
    return {
        "current_total": cur.get("sim_total", 0.0),
        "candidate_total": new.get("sim_total", 0.0),
        "delta": float(round(new.get("sim_total", 0.0) - cur.get("sim_total", 0.0), 2)),
        "candidate_by_tier": new.get("by_tier", []),
        "candidate_by_user": new.get("by_user", []),
        "candidate_daily": new.get("daily", []),
    }
