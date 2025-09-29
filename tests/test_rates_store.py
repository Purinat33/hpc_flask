import pytest
from models import rates_store


@pytest.mark.db
def test_rates_load_save_roundtrip():
    r0 = rates_store.load_rates()
    # schema: tiers like "mu","gov","private" and fields "cpu","gpu","mem"
    assert "mu" in r0
    assert set(["cpu", "gpu", "mem"]).issubset(r0["mu"].keys())

    old_cpu = float(r0["mu"]["cpu"])
    new_cpu = old_cpu + 0.01

    r0["mu"]["cpu"] = new_cpu
    rates_store.save_rates(r0)

    r1 = rates_store.load_rates()
    assert float(r1["mu"]["cpu"]) == pytest.approx(new_cpu, rel=0, abs=1e-9)
    # sanity: unchanged fields remain
    assert r1.keys() == r0.keys()
    assert set(r1["mu"].keys()) == set(r0["mu"].keys())
