# services/payments/base.py
"""
Abstract interface + simple event model for payments.
Adapters must implement PaymentProvider.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Protocol


@dataclass
class PaymentIntentResult:
    external_payment_id: str      # provider's id (charge/session)
    # URL to redirect the user, if provider-hosted checkout exists
    checkout_url: Optional[str]
    idempotency_key: Optional[str]


@dataclass
class WebhookEvent:
    provider: str                 # 'stripe' | 'omise' | ...
    external_event_id: Optional[str]
    event_type: str               # e.g. 'payment.succeeded'
    external_payment_id: str
    amount_cents: int
    currency: str
    raw: Dict[str, Any]
    signature_ok: bool


class PaymentProvider(Protocol):
    name: str

    def create_checkout(self, *, payment_id: int, amount_cents: int, currency: str,
                        username: str, receipt_id: int,
                        success_url: str, cancel_url: str) -> PaymentIntentResult:
        """
        Create a payment intent/checkout on the provider.
        Must be idempotent from our side (use payment_id in idempotency key).
        """

    def parse_webhook(self, request) -> WebhookEvent:
        """
        Verify signature and parse the provider webhook into WebhookEvent.
        Must mark signature_ok=True only if verification passes.
        Raise on malformed payloads; otherwise return event.
        """
