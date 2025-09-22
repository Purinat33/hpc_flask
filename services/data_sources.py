# data_sources.py
from shutil import which
from functools import lru_cache
from services.datetimex import ensure_utc_series, APP_TZ, local_day_end_utc
import os
import subprocess
import pandas as pd
import requests
from io import StringIO
from datetime import date, datetime, timedelta
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
        "ReqTRES,AllocTRES,AveRSS,MaxRSS,TRESUsageInTot,TRESUsageOutTot,End,State,"
        "ExitCode,DerivedExitCode,"
        "ConsumedEnergyRaw,ConsumedEnergy,"
        "NodeList,AllocNodes"
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
        # sacct End is local cluster time without tz; localize then convert to UTC
        df["End"] = ensure_utc_series(df["End"], assume_local=APP_TZ)
        cutoff_utc = local_day_end_utc(date.fromisoformat(end_date))
        df = df[df["End"].notna() & (df["End"] <= cutoff_utc)]

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
            # try parse with tz-aware; if no tz, assume local then UTC
            s = pd.to_datetime(df["End"], errors="coerce", utc=False)
            if getattr(s.dtype, "tz", None) is None:
                s = s.dt.tz_localize(APP_TZ)
            df["End"] = s.dt.tz_convert("UTC")

            cutoff_utc = local_day_end_utc(date.fromisoformat(end_date))
            df = df[df["End"].notna() & (df["End"] <= cutoff_utc)]
        # df = drop_steps(df)
        return df, "test.csv", notes
    except Exception as e:
        notes.append(f"test.csv: {e}")
        raise


def _expand_bracket_chunk(prefix: str, spec: str) -> list[str]:
    out = []
    for token in spec.split(","):
        token = token.strip()
        if "-" in token:
            a, b = token.split("-", 1)
            width = max(len(a), len(b))
            for i in range(int(a), int(b) + 1):
                out.append(f"{prefix}{i:0{width}d}")
        else:
            # preserve zero padding from token, if any
            width = len(token)
            out.append(f"{prefix}{int(token):0{width}d}")
    return out


# @lru_cache(maxsize=4096)
# def expand_nodelist(nodelist: str) -> list[str]:
#     n = (nodelist or "").strip()
#     if not n or n.lower().startswith("none"):
#         return []

#     # 1) Fast paths that don't need Slurm
#     # simple single host
#     if "[" not in n and "," not in n:
#         return [n]
#     # simple comma-separated list: foo,bar,baz
#     if "[" not in n and "," in n:
#         return [p.strip() for p in n.split(",") if p.strip()]

#     # bracketed forms like gpu[01-03,07], node[1-2], tau[1], alpha,beta[01-02]
#     parts = []
#     chunks = [c.strip() for c in n.split(",") if c.strip()]
#     for chunk in chunks:
#         m = re.match(r"^(?P<prefix>[^\[]+)\[(?P<spec>[^\]]+)\]$", chunk)
#         if m:
#             parts.extend(_expand_bracket_chunk(
#                 m.group("prefix"), m.group("spec")))
#         else:
#             parts.append(chunk)
#     if parts:
#         return parts

#     # 2) Last resort: if we're actually on a Slurm node, try scontrol
#     if which("scontrol"):
#         try:
#             out = _run(["scontrol", "show", "hostnames", n])
#             return [ln.strip() for ln in out.splitlines() if ln.strip()]
#         except Exception:
#             pass

#     return []

@lru_cache(maxsize=4096)
def expand_nodelist(nodelist: str) -> list[str]:
    n = (nodelist or "").strip()
    if not n:
        return []
    try:
        out = _run(["scontrol", "show", "hostnames", n])
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        return names or [n]           # <-- fallback to original token
    except Exception:
        # naive expansion: node[01-03] -> node01,node02,node03 ; else return the token
        import re
        m = re.match(r"^([^\[]+)\[(\d+)-(\d+)\]$", n)
        if m:
            prefix, a, b = m.group(1), int(m.group(2)), int(m.group(3))
            width = len(m.group(2))
            return [f"{prefix}{i:0{width}d}" for i in range(a, b+1)]
        return [n]
