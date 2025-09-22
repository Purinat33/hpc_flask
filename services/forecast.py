# services/forecast.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Tuple
import math
import numpy as np
import pandas as pd

# --- Optional: if statsmodels is available, we use Holt-Winters (weekly) ---
_USE_HW = False
try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing  # type: ignore
    _USE_HW = True
except Exception:
    _USE_HW = False


@dataclass
class ForecastResult:
    history_labels: List[str]
    history_values: List[float]
    # {30|60|90: {"labels","values","lower","upper"}}
    horizons: Dict[int, Dict[str, List[float]]]


def _ensure_daily_index(s: pd.Series, end_date: str, train_days: int = 180) -> pd.Series:
    """Ensure a continuous daily DateIndex, fill missing with 0, keep last N days."""
    s = s.copy()
    s.index = pd.to_datetime(s.index)  # to DatetimeIndex (day precision ok)
    end = pd.to_datetime(end_date)
    start = end - pd.Timedelta(days=train_days-1)
    full_ix = pd.date_range(start=start.normalize(),
                            end=end.normalize(), freq="D")
    s = s.reindex(full_ix).fillna(0.0)
    return s


def _hw_forecast(daily: pd.Series, horizon: int) -> Tuple[List[str], List[float], List[float], List[float]]:
    """Holt-Winters weekly additive, fallback to seasonal-naive if it fails."""
    try:
        model = ExponentialSmoothing(
            daily.astype(float),
            trend="add",
            seasonal="add",
            seasonal_periods=7,
            initialization_method="estimated",
        ).fit(optimized=True)
        fcast = model.forecast(horizon)
        # rough PI via residual std (not exact, but serviceable)
        resid = daily - model.fittedvalues
        sigma = float(resid.std(ddof=1) or 0.0)
        z = 1.96  # ~95%
        lower = fcast - z * sigma
        upper = fcast + z * sigma
        idx = pd.date_range(
            start=daily.index[-1] + pd.Timedelta(days=1), periods=horizon, freq="D")
        return (
            [d.date().isoformat() for d in idx],
            [float(max(0.0, v)) for v in fcast.values],
            [float(max(0.0, v)) for v in lower.values],
            [float(max(0.0, v)) for v in upper.values],
        )
    except Exception:
        return _seasonal_naive_forecast(daily, horizon)


def _seasonal_naive_forecast(daily: pd.Series, horizon: int) -> Tuple[List[str], List[float], List[float], List[float]]:
    """
    Weekly seasonal-naive: y_hat[t+h] = y[t - 7 + ((h-1) mod 7)]
    PI from residuals of y[t] - y[t-7] over last 8 weeks.
    """
    if len(daily) < 14:
        # too little history → flat mean
        avg = float(daily.tail(min(len(daily), 7)).mean() or 0.0)
        idx = pd.date_range(
            start=daily.index[-1] + pd.Timedelta(days=1), periods=horizon, freq="D")
        vals = [avg] * horizon
        return ([d.date().isoformat() for d in idx], vals, vals, vals)

    # pattern = last 7 actuals
    last7 = daily.tail(7).values.tolist()
    vals = [float(last7[(i % 7)]) for i in range(horizon)]

    # residuals from seasonal naive fit: r_t = y_t - y_{t-7}
    lag = 7
    if len(daily) > lag:
        r = daily[lag:] - daily[:-lag].values
        sigma = float(r.std(ddof=1) or 0.0)
    else:
        sigma = 0.0
    z = 1.96  # ~95%
    lower = [max(0.0, v - z * sigma) for v in vals]
    upper = [max(0.0, v + z * sigma) for v in vals]

    idx = pd.date_range(
        start=daily.index[-1] + pd.Timedelta(days=1), periods=horizon, freq="D")
    return (
        [d.date().isoformat() for d in idx],
        [float(v) for v in vals],
        [float(v) for v in lower],
        [float(v) for v in upper],
    )


def multi_horizon_forecast(daily: pd.Series, horizons=(30, 60, 90)) -> ForecastResult:
    """
    Input: daily series indexed by day (continuous, zeros filled).
    Output: history + per-horizon forecasts with simple 95% intervals.
    """
    hist_labels = [d.date().isoformat() for d in daily.index]
    hist_values = [float(v) for v in daily.values]

    out: Dict[int, Dict[str, List[float]]] = {}
    for h in horizons:
        if _USE_HW:
            f_labels, f_vals, lower, upper = _hw_forecast(daily, h)
        else:
            f_labels, f_vals, lower, upper = _seasonal_naive_forecast(daily, h)
        out[int(h)] = {
            "labels": f_labels,
            "values": f_vals,
            "lower": lower,
            "upper": upper,
        }

    return ForecastResult(history_labels=hist_labels, history_values=hist_values, horizons=out)


def build_daily_series(df: pd.DataFrame, metric: str, end_date: str, train_days: int = 180) -> pd.Series:
    """
    Map a metric key -> daily series from the costed DF.
      metric in {"cost","jobs","cpu","gpu","mem"}
    """
    if df is None or df.empty or "End" not in df.columns:
        return pd.Series(dtype=float)

    df = df.copy()
    end = pd.to_datetime(df["End"], errors="coerce", utc=True)
    df = df[end.notna()].copy()
    df["__day"] = end.dt.tz_convert("UTC").dt.date

    metric = (metric or "cost").lower()
    if metric == "cost":
        col = "Cost (฿)"
        g = df.groupby("__day")[col].sum()
    elif metric == "jobs":
        g = df.groupby("__day")["JobID"].nunique()
    elif metric == "cpu":
        g = df.groupby("__day")["CPU_Core_Hours"].sum()
    elif metric == "gpu":
        g = df.groupby("__day")["GPU_Hours"].sum()
    elif metric == "mem":
        # prefer used; fallback to alloc if needed
        if "Mem_GB_Hours_Used" in df.columns:
            g = df.groupby("__day")["Mem_GB_Hours_Used"].sum()
        else:
            g = df.groupby("__day")["Mem_GB_Hours_Alloc"].sum()
    else:
        # default to cost
        col = "Cost (฿)"
        g = df.groupby("__day")[col].sum()

    g = g.astype(float)
    g.index = pd.to_datetime(g.index)
    return _ensure_daily_index(g, end_date=end_date, train_days=train_days)
