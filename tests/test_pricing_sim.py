import pytest
import pandas as pd

from services.pricing_sim import (
    _normalize_rates,
    build_pricing_components,
    simulate_revenue,
    simulate_vs_current,
)


def test_normalize_rates_lowercases_and_defaults():
    r = _normalize_rates({"MU": {"cpu": 1, "gpu": 5, "mem": 0.5}})
    assert "mu" in r
    rs = r["mu"]
    assert rs.cpu == 1.0 and rs.gpu == 5.0 and rs.mem == 0.5

    # missing keys should default to 0.0
    r2 = _normalize_rates({"gov": {}})
    assert r2["gov"].cpu == 0.0 and r2["gov"].gpu == 0.0 and r2["gov"].mem == 0.0


def test_build_pricing_components_groups_and_normalizes():
    df = pd.DataFrame([
        {
            "End": "2025-01-15T00:00:00Z",
            "tier": "MU",           # should become "mu"
            "User": "alice",
            "CPU_Core_Hours": 2.0,
            "GPU_Hours": 0.0,
            "Mem_GB_Hours_Used": 4.0,
        },
        {
            "End": "2025-01-15T12:00:00+00:00",
            "tier": "gov",
            "User": "bob",
            "CPU_Core_Hours": 1.0,
            "GPU_Hours": 1.0,
            "Mem_GB_Hours_Used": 0.0,
        },
    ])
    comps = build_pricing_components(df)
    # should have two rows (per user), with normalized tiers and date-only
    assert set(comps["tier"]) == {"mu", "gov"}
    assert set(comps["User"]) == {"alice", "bob"}
    assert str(comps.iloc[0]["date"]) == "2025-01-15"


def test_simulate_vs_current_math_and_shapes():
    # two jobs on same day, one MU and one GOV
    df = pd.DataFrame([
        {"End": "2025-01-15T00:00:00Z",      "tier": "MU",  "User": "alice",
         "CPU_Core_Hours": 2.0, "GPU_Hours": 0.0, "Mem_GB_Hours_Used": 4.0},
        {"End": "2025-01-15T12:00:00+00:00", "tier": "gov", "User": "bob",
         "CPU_Core_Hours": 1.0, "GPU_Hours": 1.0, "Mem_GB_Hours_Used": 0.0},
    ])
    comps = build_pricing_components(df)

    current = {
        "mu":  {"cpu": 1.0, "gpu": 5.0,  "mem": 0.5},
        "gov": {"cpu": 3.0, "gpu": 10.0, "mem": 1.0},
    }
    candidate = {
        "mu":  {"cpu": 2.0, "gpu": 5.0,  "mem": 0.5},  # double MU CPU
        "gov": {"cpu": 3.0, "gpu": 10.0, "mem": 1.0},  # unchanged
    }

    out = simulate_vs_current(comps, current, candidate)
    # current_total: MU=2*1 + 4*0.5 = 4; GOV=1*3 + 1*10 = 13 -> 17
    # candidate_total: MU=2*2 + 4*0.5 = 6; GOV unchanged 13 -> 19; delta=2
    assert out["current_total"] == pytest.approx(17.0, rel=0, abs=1e-9)
    assert out["candidate_total"] == pytest.approx(19.0, rel=0, abs=1e-9)
    assert out["delta"] == pytest.approx(2.0, rel=0, abs=1e-9)

    # shape checks from simulate_revenue() output
    assert isinstance(out["candidate_by_tier"], list)
    assert any(item["tier"] in ("MU", "GOV")
               for item in out["candidate_by_tier"])
    assert isinstance(out["candidate_by_user"], list)
    assert any(item["user"] in ("alice", "bob")
               for item in out["candidate_by_user"])
    assert isinstance(out["candidate_daily"], list)
    assert any(row["date"] == "2025-01-15" for row in out["candidate_daily"])


def test_simulate_revenue_empty_components_returns_zeros():
    empty = build_pricing_components(pd.DataFrame())
    res = simulate_revenue(empty, {"mu": {"cpu": 1, "gpu": 5, "mem": 0.5}})
    assert res["sim_total"] == 0.0
    assert res["by_tier"] == []
    assert res["by_user"] == []
    assert res["daily"] == []


def test_unknown_tier_uses_private_fallback():
    # components with an unknown tier should use "private" rates if provided
    comps = pd.DataFrame([{
        "date": pd.to_datetime("2025-01-10").date(),
        "tier": "something-else",
        "User": "u",
        "CPU_Core_Hours": 1.0,
        "GPU_Hours": 0.0,
        "Mem_GB_Hours_Used": 0.0,
    }])

    # private cpu rate = 7.0 → sim_total should be 7.0
    res = simulate_revenue(
        comps, {"private": {"cpu": 7.0, "gpu": 0.0, "mem": 0.0}})
    assert res["sim_total"] == pytest.approx(7.0, rel=0, abs=1e-9)
