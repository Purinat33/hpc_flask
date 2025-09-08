# services/metrics.py
from __future__ import annotations
import os

os.environ.setdefault("PROMETHEUS_DISABLE_CREATED_SERIES", "1")

try:
    from prometheus_client import (
        Counter, Histogram, CollectorRegistry,
        generate_latest, CONTENT_TYPE_LATEST,
        # import these only if you want to add some runtime collectors back:
        # ProcessCollector, PlatformCollector, GCCollector
    )
    _PROM = True
except Exception:  # pragma: no cover
    _PROM = False

    class _Noop:
        def labels(self, **kw): return self
        def inc(self, *a, **k): pass
        def observe(self, *a, **k): pass

    def Counter(*a, **k): return _Noop()
    def Histogram(*a, **k): return _Noop()
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    def generate_latest(*a, **k): return b""
    def CollectorRegistry(*a, **k): return None

# Use a DEDICATED registry so only our app metrics show up
APP_REGISTRY = CollectorRegistry(auto_describe=True)

# --- Generic HTTP metrics (bind to our registry) ---
REQUEST_COUNT = Counter(
    "http_requests_total", "HTTP requests total",
    ["method", "endpoint", "status"], registry=APP_REGISTRY
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency (seconds)",
    ["endpoint", "method"], registry=APP_REGISTRY,
    # buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
)

# --- Auth flow metrics ---
LOGIN_SUCCESSES = Counter("auth_login_success_total",
                          "Login successes", registry=APP_REGISTRY)
LOGIN_FAILURES = Counter("auth_login_failure_total", "Login failures", [
                         "reason"], registry=APP_REGISTRY)
LOCKOUT_ACTIVE = Counter("auth_lockout_active_total",
                         "Active lockouts shown", registry=APP_REGISTRY)
LOCKOUT_START = Counter("auth_lockout_start_total",
                        "Lockouts started", registry=APP_REGISTRY)
LOCKOUT_END = Counter("auth_lockout_end_total",
                      "Lockouts ended", registry=APP_REGISTRY)
FORBIDDEN_REDIRECTS = Counter(
    "auth_forbidden_redirect_total", "Non-admin attempted admin; redirected", registry=APP_REGISTRY
)

# --- Billing & CSV metrics ---
RECEIPT_CREATED = Counter("billing_receipt_created_total", "Receipts created", [
                          "scope"], registry=APP_REGISTRY)
RECEIPT_MARKED_PAID = Counter("billing_receipt_marked_paid_total", "Receipts marked paid", [
                              "actor_type"], registry=APP_REGISTRY)
RECEIPT_VOIDED = Counter("billing_receipt_voided_total",
                         "Receipts voided", registry=APP_REGISTRY)
CSV_DOWNLOADS = Counter("csv_download_total", "CSV download events", [
                        "kind"], registry=APP_REGISTRY)

# --- Payments / Webhook ---
WEBHOOK_EVENTS = Counter(
    "payments_webhook_events_total", "Webhook events", ["provider", "event", "outcome"], registry=APP_REGISTRY
)


def init_app(app):
    @app.get("/metrics")
    def metrics():
        data = generate_latest(APP_REGISTRY)
        return app.response_class(data, mimetype=CONTENT_TYPE_LATEST)

    # Optionally re-add some runtime collectors (commented out by default to keep it clean):
    # ProcessCollector(registry=APP_REGISTRY)
    # PlatformCollector(registry=APP_REGISTRY)
    # GCCollector(registry=APP_REGISTRY)

    # --- pre-warm labeled series so dashboards don't say "No data" ---
    try:
        LOGIN_FAILURES.labels(reason="bad_credentials").inc(0)
        LOCKOUT_ACTIVE.inc(0)
        LOCKOUT_START.inc(0)
        LOCKOUT_END.inc(0)
        FORBIDDEN_REDIRECTS.inc(0)

        RECEIPT_CREATED.labels(scope="user").inc(0)
        RECEIPT_CREATED.labels(scope="admin").inc(0)
        # FIX: label name must be actor_type (not "actor")
        RECEIPT_MARKED_PAID.labels(actor_type="admin").inc(0)
        RECEIPT_VOIDED.inc(0)

        for k in ("admin_paid", "my_usage", "user_usage", "audit"):
            CSV_DOWNLOADS.labels(kind=k).inc(0)

        WEBHOOK_EVENTS.labels(
            provider="dummy", event="payment_succeeded", outcome="ok").inc(0)
    except Exception:
        pass
