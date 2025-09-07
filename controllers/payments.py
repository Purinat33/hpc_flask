# controllers/payments.py
from __future__ import annotations
import hmac
import hashlib
import os
import json
from flask import Blueprint, render_template, request, redirect, url_for, abort, current_app
from flask_login import login_required, current_user

from services.payments.registry import get_provider
from models.payments_store import (
    init_payments_schema,
    create_payment_for_receipt,
    attach_provider_checkout,
    record_webhook_event,
    finalize_success_if_amount_matches,
    get_payment,
)
from models.audit_store import audit
from models.payments_store import load_receipt
from models.payments_store import get_latest_payment_for_receipt
payments_bp = Blueprint("payments", __name__)


def _env(key: str, default: str | None = None) -> str | None:
    v = os.environ.get(key)
    return v if v is not None else current_app.config.get(key, default)


# ----- user starts a checkout for a receipt -----

@payments_bp.get("/payments/receipt/<int:rid>/start")
@login_required
def start_receipt_payment(rid: int):
    rec = load_receipt(rid)
    if not rec:
        abort(404)
    if not (current_user.is_admin or rec["username"] == current_user.username):
        abort(403)

    currency = (_env("PAYMENT_CURRENCY") or "THB").upper()
    provider = get_provider()

    # Reuse if we already have a pending/succeeded intent
    existing = get_latest_payment_for_receipt(rid)
    if existing:
        if existing["status"] == "succeeded":
            return redirect(url_for("payments.payment_thanks", rid=rid))
        if existing["status"] == "pending" and existing.get("checkout_url"):
            return redirect(existing["checkout_url"])

    # Otherwise create a new intent
    pid, amount_cents = create_payment_for_receipt(
        provider.name, rid, current_user.username, currency)

    site = _env("SITE_BASE_URL") or request.host_url.rstrip("/")
    success_base = _env("PAYMENT_SUCCESS_PATH") or "/payments/thanks"
    success_url = f"{site}{success_base}?rid={rid}"
    cancel_url = site + (_env("PAYMENT_CANCEL_PATH") or "/me")

    intent = provider.create_checkout(
        payment_id=pid,
        amount_cents=amount_cents,
        currency=currency,
        username=current_user.username,
        receipt_id=rid,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    attach_provider_checkout(
        pid, intent.external_payment_id, intent.checkout_url, intent.idempotency_key)

    audit("payment.intent", target=f"receipt={rid}", status=200,
          extra={"payment_id": pid, "provider": provider.name, "amount_cents": amount_cents})

    return redirect(intent.checkout_url or url_for("user.view_receipt", rid=rid))


@payments_bp.get("/payments/thanks")
@login_required
def payment_thanks():
    """
    Friendly confirmation page. If ?rid= is provided and belongs to the user,
    we show current receipt status to avoid confusion.
    """
    from models.payments_store import load_receipt

    rid = request.args.get("rid", type=int)
    status = None
    if rid:
        rec = load_receipt(rid)
        if rec and rec.get("username") == current_user.username:
            status = rec.get("status")

    return render_template("payments/thanks.html", rid=rid, status=status)


# ----- provider webhook (no auth, signature-verified) -----

@payments_bp.post("/payments/webhook")
def webhook():
    """
    Generic webhook endpoint. The adapter does signature verification and returns a WebhookEvent.
    This route must be CSRF-exempt in app.py.
    """
    provider = get_provider()
    evt = provider.parse_webhook(request)

    # First, persist the event (idempotent on external_event_id)
    eid = record_webhook_event(
        provider.name, evt.external_event_id, evt.event_type, evt.raw, evt.signature_ok)
    audit("payment.webhook", target=f"provider={provider.name}", status=200 if evt.signature_ok else 400,
          extra={"event_id": eid, "etype": evt.event_type})

    if not evt.signature_ok:
        abort(400)

    # We only act on "success" events (adapter should normalize names)
    if evt.event_type in ("payment.succeeded", "charge.succeeded", "checkout.session.completed"):
        ok = finalize_success_if_amount_matches(
            external_payment_id=evt.external_payment_id,
            amount_cents=evt.amount_cents,
            currency=evt.currency,
            provider=provider.name,
        )
        audit("payment.finalize", target=f"external={evt.external_payment_id}", status=200 if ok else 409,
              extra={"currency": evt.currency, "amount_cents": evt.amount_cents})

    return "", 200


# ----- DEV ONLY: simulate a payment (used by DummyProvider) -----

@payments_bp.get("/payments/simulate")
@login_required
def simulate_checkout():
    """
    Dev helper used by DummyProvider:
    - constructs a signed (or plain) JSON and POSTs to /payments/webhook to simulate success
    - then redirects the user back to /payments/thanks
    """
    rid = request.args.get("rid", type=int)
    external_payment_id = request.args.get("external_payment_id") or ""
    amount_cents = int(request.args.get("amount_cents") or "0")
    currency = request.args.get("currency") or "THB"

    payload = {
        "event_type": "payment.succeeded",
        "external_payment_id": external_payment_id,
        "amount_cents": amount_cents,
        "currency": currency,
    }
    body = json.dumps(payload).encode("utf-8")

    # Post internally
    from werkzeug.test import EnvironBuilder
    from werkzeug.wrappers import Request as WSGIRequest
    from werkzeug.test import run_wsgi_app

    builder = EnvironBuilder(method="POST", path=url_for(
        "payments.webhook"), data=body, content_type="application/json")
    env = builder.get_environ()
    secret = (_env("PAYMENT_WEBHOOK_SECRET") or "dev").encode("utf-8")
    mac = hmac.new(secret, body, hashlib.sha256).hexdigest()
    env["HTTP_X_DUMMY_SIGNATURE"] = mac

    status, headers, app_iter = run_wsgi_app(current_app.wsgi_app, env)
    # drain iterator
    for _ in app_iter:  # noqa
        pass

    return redirect(url_for("payments.payment_thanks", rid=rid))
