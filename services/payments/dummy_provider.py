# services/payments/dummy_provider.py
"""
A development-only provider that *simulates* a successful payment.
Useful to test end-to-end flows without touching real gateways.

How it works:
- create_checkout(...) returns a local "external_payment_id" and a
  "checkout_url" that points to an internal route which immediately
  triggers a fake webhook (success).
- parse_webhook(...) accepts our own POST and returns a WebhookEvent.
"""

from __future__ import annotations
import hashlib
import hmac
import os
from urllib.parse import urlencode

from flask import url_for, request as flask_request
from services.payments.base import PaymentProvider, PaymentIntentResult, WebhookEvent


class DummyProvider(PaymentProvider):
    name = "dummy"

    def _secret(self) -> bytes:
        return (os.environ.get("PAYMENT_WEBHOOK_SECRET") or "dev").encode("utf-8")

    def create_checkout(self, *, payment_id: int, amount_cents: int, currency: str,
                        username: str, receipt_id: int,
                        success_url: str, cancel_url: str) -> PaymentIntentResult:
        # Make a deterministic external id for local testing
        external_payment_id = f"dummy_{payment_id}"
        # Build a local “checkout” URL that just simulates success by calling our webhook.
        qs = urlencode({
            "external_payment_id": external_payment_id,
            "amount_cents": amount_cents,
            "currency": currency,
        })
        # The route below will post a fake webhook into the app and then redirect.
        checkout_url = url_for(
            "payments.simulate_checkout", _external=True) + "?" + qs
        idem = f"idem_{payment_id}"
        return PaymentIntentResult(external_payment_id, checkout_url, idem)

    def parse_webhook(self, request) -> WebhookEvent:
        # Dummy: accept JSON {"external_payment_id": "...", "amount_cents": 123, "currency": "THB"}
        payload = request.get_json(silent=True) or {}
        body = request.get_data() or b""
        sig = request.headers.get("X-Dummy-Signature", "")
        mac = hmac.new(self._secret(), body, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(mac, sig)

        return WebhookEvent(
            provider=self.name,
            external_event_id=payload.get("event_id"),   # None in dummy
            event_type=payload.get("event_type") or "payment.succeeded",
            external_payment_id=payload.get("external_payment_id", ""),
            amount_cents=int(payload.get("amount_cents") or 0),
            currency=(payload.get("currency") or "THB"),
            raw=payload,
            signature_ok=ok or True,   # Always ok for dev
        )
