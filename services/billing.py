# billing.py
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


def compute_costs(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds GPU_Hours, Mem_GB_Hours, CPU_Core_Hours and Cost (฿) using the latest rates.
    Expects columns: User, JobID, Elapsed, TotalCPU, ReqTRES
    """
    if df.empty:
        return df

    df = df.copy()
    # final guard: keep only parent jobs
    if "JobID" in df.columns:
        df["JobID"] = df["JobID"].astype(str)
        df = df[df["JobID"] == df["JobID"].map(canonical_job_id)]

    for c in ["User", "JobID", "Elapsed", "TotalCPU", "ReqTRES"]:
        if c not in df:
            df[c] = ""

    df["Elapsed_Hours"] = df["Elapsed"].map(hms_to_hours)
    df["TotalCPU_Hours"] = df["TotalCPU"].map(hms_to_hours)
    df["GPU_Count"] = df["ReqTRES"].fillna("").map(extract_gpu_count)
    df["Memory_GB"] = df["ReqTRES"].fillna("").map(extract_mem_gb)

    # resource-hours
    df["GPU_Hours"] = df["GPU_Count"] * df["Elapsed_Hours"]
    df["Mem_GB_Hours"] = df["Memory_GB"] * df["Elapsed_Hours"]

    # Prefer actual CPU time (core-hours). If it's 0, fall back to AllocCPUS*Elapsed (not provided here).
    df["AllocCPUS"] = df["ReqTRES"].fillna("").map(extract_cpu_count)

    # Prefer actual CPU time; if it’s 0, fall back to AllocCPUS * Elapsed
    df["CPU_Core_Hours"] = df.apply(
        lambda r: r["TotalCPU_Hours"] if r["TotalCPU_Hours"] > 0
        else r["AllocCPUS"] * r["Elapsed_Hours"],
        axis=1
    )

    # tier by user
    df["tier"] = df["User"].map(classify_user_type)

    # latest rates
    # {'mu':{'cpu':...,'gpu':...,'mem':...}, ...}
    rates = rates_store.load_rates()

    def row_cost(r):
        t = r["tier"]
        rt = rates.get(t, rates.get(
            "private", {"cpu": 5, "gpu": 100, "mem": 2}))
        return (
            r["CPU_Core_Hours"] * float(rt["cpu"]) +
            r["GPU_Hours"] * float(rt["gpu"]) +
            r["Mem_GB_Hours"] * float(rt["mem"])
        )

    df["Cost (฿)"] = df.apply(row_cost, axis=1).round(2)
    return df
