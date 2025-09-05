# services/slurm_rest.py
"""
Thin slurmrestd client that returns a Pandas DataFrame with columns
your app already consumes: User, JobID, Elapsed, TotalCPU, ReqTRES, End, State.

Configuration (env first, Flask config second):
  SLURMRESTD_URL          e.g. https://slurmctld:6820
  SLURMRESTD_TOKEN        token string -> sent as X-SLURM-USER-TOKEN
  SLURMRESTD_BEARER       bearer token (if your setup uses Authorization: Bearer)
  SLURMRESTD_BASIC        "user:pass" (if using HTTP basic)
  SLURMRESTD_API_VERSION  default: v0.0.39 (change if your cluster differs)
  SLURMRESTD_TLS_VERIFY   "true"|"false"|<path to CA pem> (default "true")
  SLURMRESTD_TIMEOUT      seconds (default 15)
  SLURMRESTD_LIMIT        optional query limiter if your API supports it

You may also provide the same keys under current_app.config["SLURMRESTD_*"].

Where to edit later:
- If your API path/params differ: tweak _build_url() / _build_params().
- If job JSON field names differ: tweak _job_to_row().
- If auth changes: tweak _build_session_headers().
"""

from __future__ import annotations
import os
import base64
from datetime import datetime
from typing import Any, Dict, List, Tuple

import pandas as pd
import requests
from flask import current_app, has_app_context


def _get(key: str, default: str | None = None) -> str | None:
    """Env first, then Flask config."""
    env = os.environ.get(key)
    if env is not None:
        return env
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def _boolish(s: str | None, default: bool = True) -> bool | str:
    """
    Interpret verify option:
      "true"/"1" -> True, "false"/"0" -> False, path -> returned as path
    """
    if s is None:
        return default
    val = s.strip().lower()
    if val in ("1", "true", "yes", "y"):
        return True
    if val in ("0", "false", "no", "n"):
        return False
    # Otherwise assume it's a CA bundle path
    return s


def _to_epoch_seconds(date_str: str, end_of_day: bool = False) -> int:
    """
    Convert 'YYYY-MM-DD' to epoch seconds.
    If end_of_day=True -> 23:59:59 of that date.
    """
    dt = datetime.fromisoformat(date_str)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(dt.timestamp())


class SlurmREST:
    def __init__(self) -> None:
        self.base_url = (_get("SLURMRESTD_URL") or "").rstrip("/")
        if not self.base_url:
            raise RuntimeError("SLURMRESTD_URL not set")

        self.api_version = _get("SLURMRESTD_API_VERSION", "v0.0.39")
        self.timeout = int(_get("SLURMRESTD_TIMEOUT", "15"))
        self.verify = _boolish(_get("SLURMRESTD_TLS_VERIFY", "true"))
        self.limit = _get("SLURMRESTD_LIMIT")  # optional

        # Auth headers
        self.headers = self._build_session_headers()

    def _build_session_headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {}
        token = _get("SLURMRESTD_TOKEN")
        if token:
            h["X-SLURM-USER-TOKEN"] = token

        bearer = _get("SLURMRESTD_BEARER")
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"

        basic = _get("SLURMRESTD_BASIC")
        if basic:
            # "user:pass" -> Basic …
            b64 = base64.b64encode(basic.encode("utf-8")).decode("ascii")
            h["Authorization"] = f"Basic {b64}"

        # Some setups want a user header too; uncomment if needed:
        # user_override = _get("SLURMRESTD_USER")
        # if user_override:
        #     h["X-SLURM-USER-NAME"] = user_override

        return h

    def _build_url(self, resource: str) -> str:
        # Typical path: /slurm/<version>/jobs
        return f"{self.base_url}/slurm/{self.api_version}/{resource.lstrip('/')}"

    def _build_params(
        self, start_date: str, end_date: str, username: str | None
    ) -> Dict[str, Any]:
        """
        Many slurmrestd builds accept epoch seconds in start_time/end_time,
        and optional user filter (user_name). If your cluster uses different
        param names, adjust here.
        """
        params: Dict[str, Any] = {
            "start_time": _to_epoch_seconds(start_date, end_of_day=False),
            "end_time": _to_epoch_seconds(end_date, end_of_day=True),
        }
        if username:
            # Name varies across versions; try the common one
            params["user_name"] = username
        if self.limit:
            params["limit"] = self.limit
        return params

    # ----- public ---------------------------------------------------------

    def fetch_jobs(
        self, start_date: str, end_date: str, username: str | None = None
    ) -> pd.DataFrame:
        """
        Returns DataFrame columns: User, JobID, Elapsed, TotalCPU, ReqTRES, End, State
        (Extra columns are OK. Downstream uses a subset + computes costs.)
        """
        url = self._build_url("jobs")
        params = self._build_params(start_date, end_date, username)

        r = requests.get(url, headers=self.headers, params=params,
                         timeout=self.timeout, verify=self.verify)
        r.raise_for_status()
        js = r.json()

        jobs = js.get("jobs") or js.get("data") or []
        if not isinstance(jobs, list):
            raise RuntimeError(
                "slurmrestd: unexpected payload (no jobs array)")

        rows: List[Dict[str, Any]] = []
        for j in jobs:
            row = self._job_to_row(j)
            if row:
                rows.append(row)

        if not rows:
            raise RuntimeError(
                "slurmrestd returned no jobs for the given window")

        df = pd.DataFrame(rows)

        # Keep only rows that finish on/before the end date if End exists
        if "End" in df.columns:
            df["End"] = pd.to_datetime(df["End"], errors="coerce")
            cutoff = pd.to_datetime(end_date).replace(
                hour=23, minute=59, second=59, microsecond=0)
            df = df[df["End"].notna() & (df["End"] <= cutoff)]

        return df

    # ----- mappers --------------------------------------------------------

    @staticmethod
    def _sec_to_hms(val: Any) -> str:
        try:
            s = int(val or 0)
        except Exception:
            return "00:00:00"
        h = s // 3600
        m = (s % 3600) // 60
        sc = s % 60
        return f"{h:02d}:{m:02d}:{sc:02d}"

    @staticmethod
    def _epoch_to_iso(val: Any) -> str | None:
        try:
            sec = int(val)
            return datetime.utcfromtimestamp(sec).isoformat(timespec="seconds") + "Z"
        except Exception:
            return None

    def _job_to_row(self, j: Dict[str, Any]) -> Dict[str, Any] | None:
        """
        Normalize differing slurmrestd schemas into one row. Update here only
        if your cluster’s JSON fields differ.
        """
        user = j.get("user_name") or j.get("user")
        jobid = j.get("job_id") or j.get("jobid") or j.get("id")

        # elapsed seconds may live at job["time"]["elapsed"] or top-level "elapsed"
        elapsed_s = (
            (j.get("time") or {}).get("elapsed")
            if isinstance(j.get("time"), dict) else j.get("elapsed")
        )

        # total cpu seconds may be under stats.total_cpu or time.total_cpu
        totalcpu_s = None
        stats = j.get("stats") or j.get("statistics") or {}
        if isinstance(stats, dict):
            totalcpu_s = stats.get("total_cpu")
        if totalcpu_s is None and isinstance(j.get("time"), dict):
            totalcpu_s = (j["time"]).get("total_cpu")

        # TRES string can be named differently
        tres_string = (
            j.get("tres_req_str")
            or j.get("tres_req")
            or j.get("tres_fmt")
            or j.get("tres")  # last resort
            or ""
        )

        # End time & State
        end_epoch = None
        t = j.get("time") or {}
        if isinstance(t, dict):
            end_epoch = t.get("end") or t.get("end_time")
        end_iso = self._epoch_to_iso(end_epoch)

        state = j.get("job_state") or j.get(
            "state") or j.get("job_state_reason") or ""

        if not user or not jobid:
            return None

        return {
            "User":     user,
            "JobID":    jobid,
            "Elapsed":  elapsed_s if isinstance(elapsed_s, str) else self._sec_to_hms(elapsed_s),
            "TotalCPU": totalcpu_s if isinstance(totalcpu_s, str) else self._sec_to_hms(totalcpu_s),
            "ReqTRES":  tres_string,
            "End":      end_iso,   # may be None; downstream handles
            "State":    state,
        }
