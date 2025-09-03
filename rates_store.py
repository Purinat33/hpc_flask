# rates_store.py
import os
import json

DEFAULT_RATES = {
    "mu":      {"cpu": 1.0,  "gpu": 5.0,   "mem": 0.5},
    "gov":     {"cpu": 3.0,  "gpu": 10.0,  "mem": 1.0},
    "private": {"cpu": 5.0,  "gpu": 100.0, "mem": 2.0},
}


def rates_file() -> str:
    return os.environ.get("RATES_FILE", "rates.json")


def get_admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "change-me")


def load_rates() -> dict:
    path = rates_file()
    if not os.path.exists(path):
        save_rates(DEFAULT_RATES)
        return DEFAULT_RATES.copy()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    merged = DEFAULT_RATES.copy()
    merged.update({k.lower(): v for k, v in data.items()})
    return merged


def save_rates(rates: dict) -> None:
    with open(rates_file(), "w", encoding="utf-8") as f:
        json.dump(rates, f, ensure_ascii=False, indent=2)
