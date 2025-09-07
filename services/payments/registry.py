# services/payments/registry.py
import os
from flask import current_app, has_app_context
# replace/add real adapters here
from services.payments.dummy_provider import DummyProvider


def _cfg(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    if v is not None:
        return v
    if has_app_context():
        return current_app.config.get(key, default)
    return default


def get_provider():
    name = (_cfg("PAYMENT_PROVIDER") or "dummy").lower()
    if name == "dummy":
        return DummyProvider()
    # elif name == "stripe":
    #     from services.payments.stripe_provider import StripeProvider
    #     return StripeProvider()
    # elif name == "omise":
    #     from services.payments.omise_provider import OmiseProvider
    #     return OmiseProvider()
    raise RuntimeError(f"Unknown PAYMENT_PROVIDER: {name}")
