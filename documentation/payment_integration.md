## Payment Integration:

> Goal: plug **any** external payment provider (Stripe, Omise, PayPal, etc.) into our app **without** touching billing math or core flows, while keeping **integrity**, **security**, and **auditing** strong.

You’ll mostly write a small **adapter** that implements our `PaymentProvider` interface, wire it up in the **registry**, and set a few **env vars**. Everything else (DB, routes, auditing, success path, idempotency) is already in place.

---

### 0) TL;DR — What you must do

1. **Create one file**: `services/payments/<provider>_provider.py`
   Implement `PaymentProvider` (see full skeleton below).

2. **Register it** in `services/payments/registry.py`.

3. **Set environment variables** in `.env`:

   - `PAYMENT_PROVIDER=<provider>` (e.g. `stripe`, `omise`)
   - `PAYMENT_CURRENCY=THB` (or your currency)
   - `SITE_BASE_URL` (public base URL)
   - Provider-specific secrets (e.g. `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, …)
   - `PAYMENT_SUCCESS_PATH=/payments/thanks`
   - `PAYMENT_CANCEL_PATH=/me`
   - `PAYMENT_WEBHOOK_SECRET` (if your adapter uses HMAC, otherwise the provider’s own webhook secret)

4. **Point the provider’s dashboard**:

   - **Webhook URL** → `https://YOURDOMAIN/payments/webhook`
   - **Success URL** → we pass it per checkout; provider should redirect the user there

5. **(Optional, dev)**: Use our **Dummy provider** end-to-end with `/payments/simulate`.

Everything else is already wired:

- Controller routes: `controllers/payments.py`
- Storage & finalization (atomic): `models/payments_store.py`
- UI: “Pay now” buttons + thanks page
- CSRF exemption for webhooks
- Audit log entries

---

### 1) How the flow works (architecture)

```
User clicks “Pay now”
        │
        ▼
controllers/payments.start_receipt_payment(rid)
  ├── create local payment intent (payments_store.create_payment_for_receipt)
  ├── provider.create_checkout(...)  ← your adapter
  ├── attach_provider_checkout(...)  ← saves provider ids/URL
  └── redirect to provider-hosted checkout
        │
        ▼
 Provider processes payment & calls our webhook:
 POST /payments/webhook  ← your adapter verifies + parses
        │
        ▼
controllers/payments.webhook()
  ├── record_webhook_event(...)  (idempotent)
  └── finalize_success_if_amount_matches(...)
         ├── verifies currency + exact amount
         ├── marks local payment as succeeded
         └── marks receipt as PAID (atomic DB tx)
```

**Data integrity** is enforced by:

- **Minor units** (cents) only, no floats
- **Currency match** + **amount match** before flipping to `paid`
- **Webhook idempotency** (`payment_events` uniq index)
- **Audit trail** (intent, webhook, finalize)

---

### 2) Files you already have (don’t change core logic)

- **Controller**: `controllers/payments.py`
  Routes to start checkout, handle thanks page, and receive webhooks.

- **Store**: `models/payments_store.py`
  Creates payment intents, records webhook events, and **finalizes** success atomically.

- **Provider interface**: `services/payments/base.py`
  `PaymentProvider` + event/result models you must return.

- **Provider registry**: `services/payments/registry.py`
  Looks up `PAYMENT_PROVIDER` and returns an adapter instance.

- **Dummy provider**: `services/payments/dummy_provider.py`
  For local dev; simulates a successful payment end-to-end.

- **UI**:

  - “Pay now” buttons are already present in admin/user templates.
  - Thanks page: `templates/payments/thanks.html`.

- **App wiring**: `app.py`

  - Blueprint registered
  - Webhook route **CSRF-exempt**
  - DB schema init on startup

---

### 3) Implementing a real provider adapter

Create a **new file** like:

```
services/payments/stripe_provider.py
```

> Replace code below with the provider’s SDK calls. The comments show **exactly** where to put real calls/field extractions.

```python
# services/payments/stripe_provider.py
"""
Stripe adapter for our PaymentProvider interface.

WHAT YOU MUST FILL:
- In create_checkout(): call Stripe API to create a Checkout Session (or PaymentIntent)
- In parse_webhook(): verify Stripe signature and parse payload -> WebhookEvent

ENV VARS USED:
- STRIPE_SECRET_KEY         : server-side API key
- STRIPE_WEBHOOK_SECRET     : stripe webhook signing secret (for /payments/webhook)
- PAYMENT_CURRENCY, SITE_BASE_URL, PAYMENT_SUCCESS_PATH, PAYMENT_CANCEL_PATH
"""

from __future__ import annotations
import os
from typing import Optional, Dict, Any

# 1) If using the official SDK:
#    pip install stripe
#    import stripe
#    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

from flask import request as flask_request

from services.payments.base import (
    PaymentProvider, PaymentIntentResult, WebhookEvent
)


class StripeProvider(PaymentProvider):
    name = "stripe"

    # ------- helpers -------
    def _secret_key(self) -> str:
        key = os.environ.get("STRIPE_SECRET_KEY")
        if not key:
            raise RuntimeError("STRIPE_SECRET_KEY is not set")
        return key

    def _webhook_secret(self) -> str:
        key = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not key:
            raise RuntimeError("STRIPE_WEBHOOK_SECRET is not set")
        return key

    # ------- required API -------
    def create_checkout(self, *, payment_id: int, amount_cents: int, currency: str,
                        username: str, receipt_id: int,
                        success_url: str, cancel_url: str) -> PaymentIntentResult:
        """
        Create a provider checkout (e.g. Stripe Checkout Session).
        MUST return:
          - external_payment_id : provider's payment/session id
          - checkout_url        : URL we redirect the user to
          - idempotency_key     : our own idempotency key for this payment
        """
        # TODO: enable Stripe SDK and create a Checkout Session (example-ish):
        # stripe.api_key = self._secret_key()
        # idem_key = f"payment_{payment_id}"
        # session = stripe.checkout.Session.create(
        #     mode="payment",
        #     success_url=success_url,
        #     cancel_url=cancel_url,
        #     client_reference_id=str(payment_id),  # our correlation
        #     metadata={
        #         "payment_id": str(payment_id),
        #         "username": username,
        #         "receipt_id": str(receipt_id),
        #     },
        #     line_items=[{
        #         "price_data": {
        #             "currency": currency.lower(),
        #             "product_data": {"name": f"HPC usage receipt #{receipt_id}"},
        #             "unit_amount": amount_cents,
        #         },
        #         "quantity": 1,
        #     }],
        #     idempotency_key=idem_key,
        # )
        # external_id = session.id
        # checkout_url = session.url

        # --- REMOVE below once real code is in place ---
        idem_key = f"payment_{payment_id}"
        external_id = f"stripe_session_{payment_id}"
        checkout_url = success_url  # temp: send to thanks immediately for smoke test
        # ------------------------------------------------

        return PaymentIntentResult(
            external_payment_id=external_id,
            checkout_url=checkout_url,
            idempotency_key=idem_key,
        )

    def parse_webhook(self, request) -> WebhookEvent:
        """
        Verify Stripe webhook signature and normalize into WebhookEvent.
        """
        # TODO: with Stripe SDK:
        # stripe.api_key = self._secret_key()
        # payload = request.get_data(as_text=True)
        # sig_header = request.headers.get("Stripe-Signature", "")
        # event = stripe.Webhook.construct_event(payload, sig_header, self._webhook_secret())
        #
        # # Extract fields from the event for success case
        # # For Checkout Session:
        # #   event['type'] == 'checkout.session.completed'
        # #   session = event['data']['object']
        # #   external_payment_id = session['id']
        # #   amount_cents = session['amount_total']  (or compute from line items)
        # #   currency = session['currency']
        #
        # external_event_id = event.get("id")
        # event_type = event.get("type", "")
        # raw = event  # full JSON object
        # signature_ok = True
        #
        # # Normalize a few known success event types
        # if event_type == "checkout.session.completed":
        #     session = event["data"]["object"]
        #     external_payment_id = session["id"]
        #     amount_cents = int(session.get("amount_total") or 0)
        #     currency = (session.get("currency") or "").upper()
        # else:
        #     # If different flow, map accordingly
        #     external_payment_id = ""
        #     amount_cents = 0
        #     currency = "THB"

        # --- DEV fallback when SDK isn’t wired yet: trust JSON body for a smoke test ---
        payload = request.get_json(silent=True) or {}
        external_event_id = payload.get("id")
        event_type = payload.get("type") or "checkout.session.completed"
        external_payment_id = payload.get("external_payment_id", "")
        amount_cents = int(payload.get("amount_cents") or 0)
        currency = (payload.get("currency") or "THB").upper()
        raw = payload
        signature_ok = True  # ONLY for local testing
        # --------------------------------------------------------------------------------

        return WebhookEvent(
            provider=self.name,
            external_event_id=external_event_id,
            event_type=event_type,
            external_payment_id=external_payment_id,
            amount_cents=amount_cents,
            currency=currency,
            raw=raw,
            signature_ok=signature_ok,
        )
```

#### Then register it

```python
# services/payments/registry.py
import os
from flask import current_app, has_app_context
from services.payments.dummy_provider import DummyProvider
# ADD:
# from services.payments.stripe_provider import StripeProvider

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
    elif name == "stripe":           # ADD THIS BRANCH
        return StripeProvider()
    # elif name == "omise": ...
    # elif name == "paypal": ...
    raise RuntimeError(f"Unknown PAYMENT_PROVIDER: {name}")
```

Set `.env`:

```
PAYMENT_PROVIDER=stripe
PAYMENT_CURRENCY=THB
SITE_BASE_URL=https://your.domain     # must be public
PAYMENT_SUCCESS_PATH=/payments/thanks
PAYMENT_CANCEL_PATH=/me
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

> **Important:** Your provider must **redirect** users back to `success_url` after a successful charge. We already pass that URL to `create_checkout(...)`.

---

### 4) What’s already handled for you

- **Atomic “mark paid”**: `finalize_success_if_amount_matches(...)` compares currency/amount and flips both **payment** and **receipt** in one DB transaction.
- **Idempotency**: `payment_events` has a `UNIQUE(provider, external_event_id)`. Duplicate webhooks are ignored safely.
- **Auditing**:

  - `payment.intent` (start checkout)
  - `payment.webhook` (received, with signature ✅/❌)
  - `payment.finalize` (success path, or 409 on mismatch)

- **UI**: “Pay now” buttons and a friendly **thanks** page (`/payments/thanks`) that links back to the receipt.

---

### 5) Security checklist

- ✅ **Webhook signature verification** in your adapter’s `parse_webhook`
  (e.g., Stripe’s `Stripe-Signature`, Omise’s signature, PayPal’s validation).
  Return `signature_ok=False` if verification fails.

- ✅ **Never trust amounts from the client/UI**. We verify the amount from the **provider** webhook against our locally stored `amount_cents`.

- ✅ **No card data** in our DB. We only store:

  - `external_payment_id`
  - `status`
  - `amount_cents`, `currency`
  - raw webhook (for reconciliation)

- ✅ **CSRF-exempt** only for `/payments/webhook` (already configured).

- ✅ **Public URL**: Ensure `SITE_BASE_URL` is set correctly so providers can reach the webhook and build proper success URLs.

---

### 6) Local / Dev testing

- **With dummy provider** (no external gateway):

  1. `.env`: `PAYMENT_PROVIDER=dummy`
  2. Create a receipt (My Usage → Create Receipt).
  3. Click **Pay now** → you’ll be redirected through `/payments/simulate`, which posts a fake signed webhook and then sends you to **Thanks**.
  4. Open the receipt detail — it should be **paid**.

- **With Stripe/Omise/etc.**:

  1. Set provider variables in `.env`.
  2. Expose your app (e.g., via ngrok) so the provider can call `/payments/webhook`.
  3. Create a receipt; click **Pay now**.
  4. Complete the hosted checkout; ensure webhook reaches the app; receipt becomes **paid**.

---

### 7) Troubleshooting

- **Thanks page shows “processing…” forever**
  → The webhook didn’t arrive or `signature_ok` was False or amount/currency mismatch.
  Check logs (`log/app.log`) and the **Audit Log** page.

- **Error: Unknown PAYMENT_PROVIDER**
  → You forgot to add your adapter branch in `registry.py` or you mistyped the env value.

- **Receipt didn’t flip to paid**
  → Confirm the provider webhook contains:

  - Correct event type (your adapter must normalize to one of:
    `payment.succeeded`, `charge.succeeded`, or `checkout.session.completed`)
  - Correct `external_payment_id`, `amount_cents`, `currency`

- **Amount mismatch**
  → Our `payments_store` compares **exact cents**. Confirm you passed the exact total (minor units) when creating checkout, and you read the right sum in the webhook.

---

### 8) Optional enhancements (if you need them later)

- **Refunds**: add `payments.refund(provider, external_payment_id, amount_cents)` and store refund events.
- **Partial payments / multi-currency**: extend `payments_store` schema with allocations.
- **Reconciliation job**: scheduled poll against provider API to verify paid statuses and fill any missed webhooks.

---

### 9) Minimal unit tests (suggested)

Create tests under `tests/` for your provider:

```python
# tests/test_payments_provider.py
from services.payments.registry import get_provider

def test_provider_create_checkout_smoke(app):
    with app.app_context():
        prov = get_provider()
        # Pretend payment id = 123, THB 100.00
        res = prov.create_checkout(payment_id=123, amount_cents=10000, currency="THB",
                                   username="alice", receipt_id=1,
                                   success_url="https://x/success", cancel_url="https://x/cancel")
        assert res.external_payment_id
        assert res.checkout_url

def test_webhook_parsing_signature_and_mapping(client, app):
    with app.app_context():
        prov = get_provider()
        # Build a fake request using Flask test_client or directly call parse_webhook with a crafted request
        # Ensure WebhookEvent has correct event_type, amount_cents, currency, signature_ok=True
```

---

### 10) Reference: What each field means

- **PaymentIntentResult**

  - `external_payment_id`: provider’s unique id (session/charge/etc.)
  - `checkout_url`: where we redirect the browser
  - `idempotency_key`: a deterministic key for the provider call (we suggest `payment_{payment_id}`)

- **WebhookEvent**

  - `event_type`: normalize your provider’s success event to one of
    `payment.succeeded`, `charge.succeeded`, or `checkout.session.completed`
  - `signature_ok`: must be **True** only if you verified the provider’s signature
  - `external_payment_id`, `amount_cents`, `currency`: used for finalization checks

---

### 11) One more full example: Omise (skeleton)

```python
# services/payments/omise_provider.py
"""
Omise adapter skeleton.

ENV:
  OMISE_SECRET_KEY       : server-side key
  OMISE_WEBHOOK_SECRET   : if using HMAC-based verification
"""

from __future__ import annotations
import os
from services.payments.base import PaymentProvider, PaymentIntentResult, WebhookEvent

class OmiseProvider(PaymentProvider):
    name = "omise"

    def _secret(self) -> str:
        key = os.environ.get("OMISE_SECRET_KEY")
        if not key:
            raise RuntimeError("OMISE_SECRET_KEY not set")
        return key

    def create_checkout(self, *, payment_id: int, amount_cents: int, currency: str,
                        username: str, receipt_id: int,
                        success_url: str, cancel_url: str) -> PaymentIntentResult:
        # TODO: call Omise API to create a charge/source and hosted checkout if applicable.
        # Use payment_id to build an idempotency key.
        external_id = f"omise_{payment_id}"   # TODO: replace with real id
        checkout_url = success_url            # TODO: replace with hosted checkout URL
        idem = f"payment_{payment_id}"
        return PaymentIntentResult(external_id, checkout_url, idem)

    def parse_webhook(self, request) -> WebhookEvent:
        # TODO: verify Omise signature, parse JSON, extract:
        # - event_type       (normalize to “payment.succeeded” on success)
        # - external_payment_id
        # - amount_cents
        # - currency
        payload = request.get_json(silent=True) or {}
        return WebhookEvent(
            provider=self.name,
            external_event_id=payload.get("id"),
            event_type=payload.get("type") or "payment.succeeded",
            external_payment_id=payload.get("charge") or payload.get("id", ""),
            amount_cents=int(payload.get("amount") or 0),
            currency=(payload.get("currency") or "THB").upper(),
            raw=payload,
            signature_ok=True,  # TODO: set after real verification
        )
```

Remember to add it in `registry.py`:

```python
elif name == "omise":
    from services.payments.omise_provider import OmiseProvider
    return OmiseProvider()
```

---

### 12) Done? Quick verification list

- [ ] `services/payments/<provider>_provider.py` created and returns correct fields
- [ ] Provider added to `registry.py`
- [ ] `.env` set: `PAYMENT_PROVIDER`, secrets, `SITE_BASE_URL`
- [ ] Provider dashboard webhook → `/payments/webhook`
- [ ] Click **Pay now**, complete checkout, receipt becomes **paid** (after webhook)
- [ ] Audit log shows `payment.intent`, `payment.webhook`, `payment.finalize` entries

If you follow the steps above, you can drop in **any** payment gateway with a small, well-contained adapter and keep the rest of the system untouched.
