# services/datetimex.py
from __future__ import annotations
from datetime import datetime, date, time, timezone
from zoneinfo import ZoneInfo
import pandas as pd

APP_TZ = ZoneInfo("Asia/Bangkok")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def to_iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def parse_iso_to_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    # pandas handles many formats & offsets; force UTC
    ts = pd.to_datetime(s, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()

def local_day_end_utc(d: date) -> datetime:
    # 23:59:59 local, then convert to UTC
    local_end = datetime.combine(d, time(23, 59, 59), tzinfo=APP_TZ)
    return local_end.astimezone(timezone.utc)

def ensure_utc_series(s: pd.Series, assume_local: ZoneInfo | None = None) -> pd.Series:
    """
    Convert a pandas series of datetimes/strings to tz-aware UTC.
    If naive, assume `assume_local` then convert to UTC.
    """
    ts = pd.to_datetime(s, errors="coerce", utc=False)
    if getattr(ts.dtype, "tz", None) is None:
        if assume_local:
            ts = ts.dt.tz_localize(assume_local)
        else:
            # last resort: assume UTC
            ts = ts.dt.tz_localize("UTC")
    return ts.dt.tz_convert("UTC")
