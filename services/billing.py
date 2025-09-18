# billing.py
import numpy as np
import re
import os
import pandas as pd
from models.rates_store import load_rates
from datetime import datetime
from models import rates_store

# -----------------------------------------------------


def canonical_job_id(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "." in s:
        prefix = s.split(".", 1)[0]
        if re.fullmatch(r"\d+(?:_\d+)?", prefix):
            return prefix
        return s
    return s
# -----------------------------------------------------

# ---------- parsing helpers ----------


def hms_to_hours(hms: str) -> float:
    """Supports D-HH:MM:SS(.fff), HH:MM:SS(.fff), and MM:SS(.fff)."""
    if not isinstance(hms, str) or not hms.strip():
        return 0.0
    s = hms.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except:
            days = 0
    parts = s.split(":")
    try:
        if len(parts) == 3:              # HH:MM:SS(.fff)
            h = int(parts[0])
            m = int(parts[1])
            sec = float(parts[2])
        elif len(parts) == 2:            # MM:SS(.fff)
            h = 0
            m = int(parts[0])
            sec = float(parts[1])
        else:
            return 0.0
    except Exception:
        return 0.0
    return days*24 + h + m/60 + sec/3600


def extract_cpu_count(tres: str) -> int:
    try:
        for it in (tres or "").split(","):
            it = it.strip()
            if it.startswith("cpu="):
                return int(float(it.split("=", 1)[1]))
    except:
        pass
    return 0


def extract_gpu_count(tres: str) -> int:
    try:
        for it in (tres or "").split(","):
            it = it.strip()
            if it.startswith("gres/gpu="):
                return int(it.split("=", 1)[1])
    except:
        pass
    return 0


def extract_mem_gb(tres: str) -> float:
    try:
        for it in (tres or "").split(","):
            it = it.strip()
            if it.startswith("mem="):
                v = it.split("=", 1)[1].upper()
                if v.endswith("G"):
                    return float(v[:-1])
                if v.endswith("M"):
                    return float(v[:-1]) / 1024.0
    except:
        pass
    return 0.0


def classify_user_type(user) -> str:
    # normalize safely; treat non-strings and NaN as empty
    try:
        u = user if isinstance(user, str) else ""
    except Exception:
        u = ""
    u = u.strip().lower()
    if any(k in u for k in ["test", "support", "admin", "monitor", "sys"]):
        return "mu"
    if any(k in u for k in ["dip", "gits", "nstda", "nectec", ".go.", "gov"]):
        return "gov"
    if re.match(r"^[a-z]+\.[a-z]+$", u) or "ku.ac.th" in u or "mu.ac.th" in u:
        return "mu"
    if any(k in u for k in ["co.th", ".com", "corp", "inc"]):
        return "private"
    return "private"


# ---------- main ----------


def _prefer_alloc_over_req(row_or_series: pd.Series, key: str) -> str:
    """Return a TRES string that contains the key=..., preferring AllocTRES."""
    alloc = str(row_or_series.get("AllocTRES", "") or "")
    req = str(row_or_series.get("ReqTRES", "") or "")
    return alloc if f"{key}=" in alloc else req


def _rss_to_gb(x) -> float:
    """
    sacct AveRSS/MaxRSS often show like '2996K' (KiB).
    Support suffixes K/M/G/T; bare numbers treated as KiB.
    """
    s = str(x or "").strip().upper()
    if not s:
        return 0.0
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGT]?)B?$", s)
    if not m:
        # Sometimes raw KiB number without suffix
        try:
            return float(s) / (1024**2)
        except:
            return 0.0
    val = float(m.group(1))
    suf = m.group(2) or "K"
    mult = {"K": 1/(1024**2), "M": 1/1024, "G": 1.0, "T": 1024.0}[suf]
    return val * mult


def compute_costs(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    # Ensure core columns exist
    for c in ["User", "JobID", "Elapsed", "TotalCPU", "ReqTRES", "AllocTRES", "AveRSS", "CPUTimeRAW"]:
        if c not in df:
            df[c] = ""

    # Parse times
    df["Elapsed_Hours"] = df["Elapsed"].map(hms_to_hours)
    df["TotalCPU_Hours"] = df["TotalCPU"].map(hms_to_hours)
    # CPUTimeRAW is seconds integer; be robust
    df["CPUTimeRAW_Hours"] = pd.to_numeric(
        df["CPUTimeRAW"], errors="coerce").fillna(0) / 3600.0

    # Mark parent vs step and define parent key
    df["ParentID"] = df["JobID"].map(canonical_job_id)
    df["is_step"] = df["JobID"].astype(str) != df["ParentID"]

    # ---------- Step-level “used” metrics ----------
    steps = df[df["is_step"]].copy()
    parents = df[~df["is_step"]].copy()

    # Memory used at step = AveRSS_GB * Elapsed_hours (approx time-avg usage)
    steps["AveRSS_GB"] = steps["AveRSS"].map(_rss_to_gb)
    steps["Mem_GB_Hours_Used_step"] = steps["AveRSS_GB"] * \
        steps["Elapsed_Hours"]

    # CPU used at step: prefer TotalCPU, then CPUTimeRAW
    steps["CPU_Core_Hours_Used_step"] = np.where(
        steps["TotalCPU_Hours"] > 0, steps["TotalCPU_Hours"], steps["CPUTimeRAW_Hours"]
    )

    # Aggregate steps → parent
    agg = steps.groupby("ParentID").agg(
        CPU_Core_Hours_Used_steps=("CPU_Core_Hours_Used_step", "sum"),
        Mem_GB_Hours_Used_steps=("Mem_GB_Hours_Used_step", "sum"),
        # we could also bring MaxRSS across steps if you want efficiency reporting:
        MaxRSS_GB=("AveRSS_GB", "max")
    ).reset_index()

    # Merge aggregates back to parent rows
    parents = parents.merge(
        agg, how="left", left_on="JobID", right_on="ParentID")
    parents["CPU_Core_Hours_Used_steps"] = parents["CPU_Core_Hours_Used_steps"].fillna(
        0)
    parents["Mem_GB_Hours_Used_steps"] = parents["Mem_GB_Hours_Used_steps"].fillna(
        0)

    # Prefer AllocTRES for allocations; fallback to ReqTRES
    parents["_TRES_CPU"] = parents.apply(
        _prefer_alloc_over_req, axis=1, result_type=None, args=("cpu",))
    parents["_TRES_GPU"] = parents.apply(
        _prefer_alloc_over_req, axis=1, result_type=None, args=("gres/gpu",))
    parents["_TRES_MEM"] = parents.apply(
        _prefer_alloc_over_req, axis=1, result_type=None, args=("mem",))

    parents["AllocCPUS"] = parents["_TRES_CPU"].map(extract_cpu_count)
    parents["GPU_Count"] = parents["_TRES_GPU"].map(extract_gpu_count)
    parents["Memory_GB"] = parents["_TRES_MEM"].map(extract_mem_gb)

    # Allocation-based resource-hours (still used for GPU + fallback)
    parents["GPU_Hours_Alloc"] = parents["GPU_Count"] * \
        parents["Elapsed_Hours"]
    parents["Mem_GB_Hours_Alloc"] = parents["Memory_GB"] * \
        parents["Elapsed_Hours"]

    # Final CPU used (per job):
    # 1) sum of step TotalCPU (or CPUTimeRAW) if > 0
    # 2) else parent's TotalCPU_Hours if > 0
    # 3) else CPUTimeRAW_Hours
    # 4) else AllocCPUS * Elapsed
    parents["CPU_Core_Hours"] = np.where(
        parents["CPU_Core_Hours_Used_steps"] > 0,
        parents["CPU_Core_Hours_Used_steps"],
        np.where(
            parents["TotalCPU_Hours"] > 0,
            parents["TotalCPU_Hours"],
            np.where(
                parents["CPUTimeRAW_Hours"] > 0,
                parents["CPUTimeRAW_Hours"],
                parents["AllocCPUS"] * parents["Elapsed_Hours"]
            )
        )
    )

    # Final Memory used: prefer step-based used; fallback to allocated
    parents["Mem_GB_Hours_Used"] = np.where(
        parents["Mem_GB_Hours_Used_steps"] > 0,
        parents["Mem_GB_Hours_Used_steps"],
        parents["Mem_GB_Hours_Alloc"]
    )

    # GPU: remain allocation-based unless you later integrate DCGM/NVML
    parents["GPU_Hours"] = parents["GPU_Hours_Alloc"]

    # Tier + Cost
    parents["tier"] = parents["User"].map(classify_user_type)

    rates = rates_store.load_rates()

    def row_cost(r):
        rt = rates.get(r["tier"], rates.get(
            "private", {"cpu": 5, "gpu": 100, "mem": 2}))
        return (
            r["CPU_Core_Hours"] * float(rt["cpu"]) +
            r["GPU_Hours"] * float(rt["gpu"]) +
            r["Mem_GB_Hours_Used"] * float(rt["mem"])
        )

    parents["Cost (฿)"] = parents.apply(row_cost, axis=1).round(2)

    # Keep a clean job-level view (one row per parent job)
    keep_cols = [
        "User", "JobID", "Elapsed", "End", "State",
        "Elapsed_Hours",
        "CPU_Core_Hours",
        "GPU_Count", "GPU_Hours",
        "Memory_GB", "Mem_GB_Hours_Used", "Mem_GB_Hours_Alloc",
        "tier", "Cost (฿)",
        "NodeList"
    ]
    for c in keep_cols:
        if c not in parents.columns:
            parents[c] = pd.NA

    return parents[keep_cols]
