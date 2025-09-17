# data_sources.py
import os
import subprocess
import pandas as pd
import requests
from io import StringIO
from datetime import datetime, timedelta
from flask import current_app, has_app_context
from services.billing import canonical_job_id
import re
# ---------- utilities ----------


def sec_to_hms(sec: int) -> str:
    try:
        sec = int(sec or 0)
    except Exception:
        sec = 0
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "command failed")
    return res.stdout

# ---------- fetchers ----------


def drop_steps(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "JobID" not in df.columns:
        return df
    df = df.copy()
    df["JobID"] = df["JobID"].astype(str)
    # keep only parent rows: JobID equals its canonical (no ".step")
    df = df[df["JobID"] == df["JobID"].map(canonical_job_id)]
    return df


def fetch_via_rest(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Uses slurmrestd if available.
    Env:
      SLURMRESTD_URL   e.g. http://slurmctld:6820
      SLURMRESTD_TOKEN optional (X-SLURM-USER-TOKEN)
    """
    base = os.environ.get("SLURMRESTD_URL")
    if not base:
        raise RuntimeError("SLURMRESTD_URL not set")

    headers = {}
    token = os.environ.get("SLURMRESTD_TOKEN")
    if token:
        headers["X-SLURM-USER-TOKEN"] = token

    # v0.0.39+; adjust if your cluster differs
    url = f"{base.rstrip('/')}/slurm/v0.0.39/jobs"
    params = {
        "start_time": f"{start_date}T00:00:00",
        "end_time":   f"{end_date}T23:59:59",
    }
    r = requests.get(url, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    js = r.json()
    rows = []
    for j in js.get("jobs", []):
        user = j.get("user_name") or j.get("user")
        jobid = j.get("job_id") or j.get("jobid")
        # seconds -> HH:MM:SS
        elapsed_s = j.get("elapsed") or j.get("time", {}).get("elapsed")
        totalcpu_s = j.get("stats", {}).get("total_cpu")
        tres_string = j.get("tres_req_str") or j.get(
            "tres_req") or j.get("tres_fmt") or ""

        rows.append({
            "User":     user,
            "JobID":    jobid,
            "Elapsed":  elapsed_s if isinstance(elapsed_s, str) else sec_to_hms(elapsed_s or 0),
            "TotalCPU": totalcpu_s if isinstance(totalcpu_s, str) else sec_to_hms(totalcpu_s or 0),
            "ReqTRES":  tres_string,
        })
    if not rows:
        raise RuntimeError("slurmrestd returned no jobs")
    return pd.DataFrame(rows)
    return drop_steps(pd.DataFrame(rows))


def fetch_from_sacct(start_date: str, end_date: str, username: str | None = None) -> pd.DataFrame:
    cmd = [
        "sacct",
        "--parsable2",  # keep header so pandas sees names
        "-S", start_date,
        "-E", end_date,
        # "-X",  # ← REMOVE: we want steps (.batch, .0, etc.)
        "-L",
        "--state=COMPLETED,FAILED,CANCELLED,TIMEOUT,PREEMPTED,NODE_FAIL,BOOT_FAIL,DEADLINE",
        "--format="
        "User,JobID,JobName,Elapsed,TotalCPU,CPUTime,CPUTimeRAW,"
        "ReqTRES,AllocTRES,AveRSS,MaxRSS,TRESUsageInTot,TRESUsageOutTot,End,State"
    ]
    if username:
        cmd += ["-u", username]
    else:
        cmd += ["--allusers"]

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out = result.stdout
    if not out.strip():
        return pd.DataFrame()

    df = pd.read_csv(StringIO(out), sep="|")

    if "End" in df.columns:
        df["End"] = pd.to_datetime(df["End"], errors="coerce")
        cutoff = pd.to_datetime(end_date) + \
            pd.Timedelta(hours=23, minutes=59, seconds=59)
        df = df[df["End"].notna() & (df["End"] <= cutoff)]

    return df


def _fallback_csv_path():
    if has_app_context():
        return current_app.config.get("FALLBACK_CSV", os.path.join(current_app.instance_path, "test.csv"))
    # allow CLI / pure unit tests
    return os.environ.get("FALLBACK_CSV", os.path.join(os.getcwd(), "instance", "test.csv"))


def fetch_via_fallback() -> pd.DataFrame:
    df = pd.read_csv(_fallback_csv_path(), sep="|",
                     keep_default_na=False, dtype=str)
    # df = drop_steps(df)
    return df


def fetch_from_slurmrestd(start_date: str, end_date: str, username: str | None = None) -> pd.DataFrame:
    """
    If your slurmrestd helper exists, query here; otherwise raise to trigger sacct fallback.
    You can filter server-side if your endpoint supports it, otherwise filter locally.
    """
    raise RuntimeError("slurmrestd not configured")  # or implement


def fetch_jobs_with_fallbacks(start_date: str, end_date: str, username: str | None = None):
    notes = []
    # 1) slurmrestd
    try:
        df = fetch_from_slurmrestd(start_date, end_date, username=username)
        if username:
            df = df[df["User"].str.strip().lower() == username.strip().lower()]
        return df, "slurmrestd", notes
    except Exception as e:
        notes.append(f"slurmrestd: {e}")

    # 2) sacct
    try:
        df = fetch_from_sacct(start_date, end_date, username=username)
        return df, "sacct", notes
    except Exception as e:
        notes.append(f"sacct: {e}")

    # 3) test.csv fallback
    try:
        path = current_app.config.get("FALLBACK_CSV")
        df = pd.read_csv(path, sep="|", keep_default_na=False, dtype=str)
        if username:
            u = username.strip().lower()
            # canonical parent key for every row
            df["JobKey"] = df["JobID"].astype(str).map(canonical_job_id)
            # find parent rows (JobID == canonical) owned by this user
            parents = df[df["JobID"].astype(str) == df["JobKey"]].copy()
            parents["_u"] = parents["User"].fillna("").str.strip().str.lower()
            keep = set(parents.loc[parents["_u"] == u, "JobKey"])
            # keep all rows (parent + steps) belonging to those parents
            df = df[df["JobKey"].isin(keep)].drop(columns=["JobKey"])

        if "End" in df.columns:
            df["End"] = pd.to_datetime(df["End"], errors="coerce")
            cutoff = pd.to_datetime(end_date) + \
                pd.Timedelta(hours=23, minutes=59, seconds=59)
            df = df[df["End"].notna() & (df["End"] <= cutoff)]
        # df = drop_steps(df)
        return df, "test.csv", notes
    except Exception as e:
        notes.append(f"test.csv: {e}")
        raise
