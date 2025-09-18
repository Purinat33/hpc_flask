# services/jinja_tz.py
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

try:
    from services.datetimex import APP_TZ
except Exception:
    APP_TZ = "UTC"


def _tz_from_app() -> ZoneInfo:
    # APP_TZ may be "Asia/Bangkok" or a tzinfo
    tzname = getattr(APP_TZ, "key", None) or getattr(APP_TZ, "zone", None) or (
        APP_TZ if isinstance(APP_TZ, str) else "UTC"
    )
    return ZoneInfo(str(tzname))


def _as_aware(dt):
    if dt is None:
        return None
    if isinstance(dt, pd.Timestamp):
        # pandas Timestamps can be tz-naive or tz-aware
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        return dt.to_pydatetime()
    if isinstance(dt, str):
        # be generous: parse strings via pandas, assume UTC if no tz
        try:
            ts = pd.to_datetime(dt, utc=True)
            return ts.to_pydatetime()
        except Exception:
            return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None  # unknown type


def dt_local(value, tz_name: str | None = None, fmt: str = "%Y-%m-%d %H:%M:%S %Z"):
    dt = _as_aware(value)
    if not dt:
        return ""
    tz = ZoneInfo(tz_name) if tz_name else _tz_from_app()
    return dt.astimezone(tz).strftime(fmt)


def register_jinja_tz_filters(app):
    app.jinja_env.filters["dt_local"] = dt_local
    app.jinja_env.globals["DISPLAY_TZ"] = getattr(_tz_from_app(), "key", "UTC")
