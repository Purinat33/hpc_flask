from models.rates_store import load_rates, save_rates


def test_rates_store_roundtrip(app):
    r = load_rates()
    r["gov"] = {"cpu": 9.9, "gpu": 8.8, "mem": 7.7}
    save_rates(r)
    r2 = load_rates()
    assert r2["gov"] == {"cpu": 9.9, "gpu": 8.8, "mem": 7.7}
