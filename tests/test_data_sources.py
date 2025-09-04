import pandas as pd
from services.data_sources import fetch_jobs_with_fallbacks


def test_fallback_filters_future_jobs(app):
    # before cutoff set to a known date
    df, src, notes = fetch_jobs_with_fallbacks("1970-01-01", "2025-02-10")
    # our sample CSV had one job on 2025-12-31; it must be filtered out
    ids = set(df["JobID"].astype(str))
    assert "1" in ids
    assert "2" not in ids
    assert src == "test.csv"
