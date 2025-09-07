# NVIDIA Bright Computing HPC Accounting Application

## Slurm Integration (Guide):

Here’s a concise, hand-off–ready manual for wiring **real Slurm** into your app for both **auth** (recommended: PAM/SSO against the cluster’s user directory) and **data fetching** (recommended: `slurmrestd`, with `sacct` as fallback). I’ve split it into requirements, setup on the cluster, app-side file changes (exact files/lines), and a short runbook. Citations to Slurm docs are included.

---

### 1) What you’ll implement

- **Authentication (login)**

  - Prefer **PAM/SSO** against the cluster’s existing identity (LDAP/AD/Unix) so users log in with the same account they use for Slurm commands. (This is separate from Slurm’s internal daemon auth via Munge.)
  - Optionally: accept **Slurm JWT** (issued by `scontrol token`) to call `slurmrestd`. Tokens go in `X-SLURM-USER-TOKEN` (or cookie) to identify a user to the REST daemon. ([Slurm][1])

- **Usage data**

  - Primary: **`slurmrestd`** `/slurm/vX.Y.Z/jobs` (+ optional `/slurmdb` endpoints if you have SlurmDBD) with time filters → convert JSON → `pandas.DataFrame`. ([Slurm][1])
  - Fallback: **`sacct`** (`--parsable2`, `--format=User,JobID,Elapsed,TotalCPU,ReqTRES,End,State`, `-S/-E`, `--allusers` for admin view). ([Slurm][1])

---

### 2) Cluster-side requirements & setup

#### A. Slurm accounting & REST

1. **Accounting enabled**

   - `slurmdbd` running, cluster registered, accounting on. (You’ll use `sacct` and optionally `/slurmdb` REST.) ([Slurm][1])

2. **Start `slurmrestd`**

   - Use the packaged unit (`slurmrestd.service`) or run it under a reverse proxy with TLS.
   - Confirm versioned OpenAPI endpoints are enabled (default). The man page shows example curl with `X-SLURM-USER-TOKEN`. ([Slurm][1])

3. **JWT auth for REST (recommended)**

   - In `slurm.conf`, load JWT auth plugin (`AuthAltTypes=auth/jwt`) and configure JWT secrets; tokens are created with `scontrol token`. ([Debian Manpages][2])
   - Clients send the token via header or cookie (`X-SLURM-USER-TOKEN`). ([Slurm][1])

4. **Authorization scope for admin usage**

   - If your “admin” user must see **all users’ jobs** via `sacct`, set their **AdminLevel** in accounting (prefer **Operator** for read-only). In `slurmdbd.conf`/`sacctmgr`, `AdminLevel=Operator` provides limited admin privileges. (SlurmDBD `PrivateData` also affects visibility.) ([Debian Manpages][2])

> Notes: The `slurmrestd` docs show available OpenAPI plugins (including `slurmdb` endpoints) and the header/cookie names for JWT.

---

### 3) App-side changes (exact files)

#### A. Add a **standalone REST client** (new file): `services/slurmrest_client.py`

Create this file; it hides all REST specifics and outputs a **normalized DataFrame** your billing pipeline already understands.

```python
# services/slurmrest_client.py
import os
import requests
import pandas as pd
from datetime import datetime

def _sec_to_hms(sec):
    try:
        sec = int(sec or 0)
    except Exception:
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def query_jobs(start_date: str, end_date: str, username: str | None = None) -> pd.DataFrame:
    """
    Calls slurmrestd /slurm/v*/jobs and returns a DataFrame with columns:
      User, JobID, Elapsed, TotalCPU, ReqTRES, End, State
    Env:
      SLURMRESTD_URL          e.g. https://slurm-ctl.example:6820
      SLURMRESTD_TOKEN        (optional) JWT for X-SLURM-USER-TOKEN
      SLURMRESTD_VERSION      (optional) e.g. v0.0.39 (default)
      SLURMRESTD_VERIFY_TLS   "0" to skip TLS verify (dev only)
    """
    base = os.environ.get("SLURMRESTD_URL")
    if not base:
        raise RuntimeError("SLURMRESTD_URL not set")
    ver = os.environ.get("SLURMRESTD_VERSION", "v0.0.39")
    verify = os.environ.get("SLURMRESTD_VERIFY_TLS", "1") != "0"

    headers = {}
    tok = os.environ.get("SLURMRESTD_TOKEN")
    if tok:
        # Header is accepted per slurmrestd manpage (also supports cookie form)
        headers["X-SLURM-USER-TOKEN"] = tok

    url = f"{base.rstrip('/')}/slurm/{ver}/jobs"
    params = {
        "start_time": f"{start_date}T00:00:00",
        "end_time":   f"{end_date}T23:59:59",
    }
    # If your slurmrestd supports server-side user filter, add here (else filter client-side)
    if username:
        params["user_name"] = username

    r = requests.get(url, headers=headers, params=params, timeout=20, verify=verify)
    r.raise_for_status()
    js = r.json()

    rows = []
    for j in js.get("jobs", []):
        user = j.get("user_name") or j.get("user")
        jobid = j.get("job_id") or j.get("jobid")
        elapsed_s = j.get("elapsed") or (j.get("time") or {}).get("elapsed")
        totalcpu_s = (j.get("stats") or {}).get("total_cpu")
        tres = j.get("tres_req_str") or j.get("tres_fmt") or j.get("tres_req") or ""
        state = j.get("job_state") or j.get("state")
        end_ts = j.get("end_time") or (j.get("time") or {}).get("end")
        # normalize
        rows.append({
            "User": user or "",
            "JobID": jobid,
            "Elapsed": elapsed_s if isinstance(elapsed_s, str) else _sec_to_hms(elapsed_s),
            "TotalCPU": totalcpu_s if isinstance(totalcpu_s, str) else _sec_to_hms(totalcpu_s),
            "ReqTRES": tres,
            "End": datetime.utcfromtimestamp(end_ts).isoformat() if isinstance(end_ts, (int, float)) else (end_ts or ""),
            "State": state or "",
        })

    if not rows:
        # keep behavior consistent with your fallbacks
        raise RuntimeError("slurmrestd returned no jobs in the range")
    df = pd.DataFrame(rows)
    return df
```

> Why header? `slurmrestd` accepts JWT via cookie or header; the man page shows header usage (`X-SLURM-USER-TOKEN`). ([Slurm][1])

---

#### B. One-line swap inside `services/data_sources.py`

Replace the placeholder `fetch_from_slurmrestd` with a thin wrapper around the new client:

```python
# services/data_sources.py (replace the stubbed function)
from services.slurmrest_client import query_jobs  # NEW

def fetch_from_slurmrestd(start_date: str, end_date: str, username: str | None = None):
    df = query_jobs(start_date, end_date, username=username)
    # defensive End cutoff (same policy you already use)
    if "End" in df.columns:
        import pandas as pd
        df["End"] = pd.to_datetime(df["End"], errors="coerce")
        cutoff = pd.to_datetime(end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        df = df[df["End"].notna() & (df["End"] <= cutoff)]
    return df
```

Everything else (your cost pipeline, receipts, etc.) remains unchanged.

---

#### C. Authentication: swap dummy DB verification for **PAM**

> Rationale: Slurm itself doesn’t verify user passwords for web apps; it expects system accounts / directory. Keep your web login aligned with cluster login by using PAM (or your SSO that also backs the cluster). `slurmrestd` auth controls access to the REST daemon; PAM controls your web session.

Minimal change in `controllers/auth.py`:

```python
# controllers/auth.py
# 1) add dependency: pip install python-pam
import pam

def _pam_auth(username: str, password: str) -> bool:
    p = pam.pam()
    # The service name can be 'login' or a custom /etc/pam.d/<service>
    return bool(p.authenticate(username, password, service=os.environ.get("PAM_SERVICE", "login")))

# in login_post(), replace:
#   if not verify_password(u, p):
# with:
if not _pam_auth(u, p):
    # ... keep your audit + throttle as-is
    ...
```

**Admin role determination options** (pick one):

- **Static list** (env/DB): keep your current role field and just migrate user creation to PAM-backed identities.
- **Query Slurm AdminLevel**: on login, run `sacctmgr show user where user=<u> format=User,AdminLevel` (or use slurmrestd `/slurmdb/*` if exposed). Treat `AdminLevel=Operator` or `Admin` as app-admin (read-only vs full). (SlurmDBD AdminLevel & PrivateData are documented in `slurmdbd.conf`.) ([Debian Manpages][2])

---

### 4) Security & operations

- **JWT handling**: Obtain JWT via `scontrol token` (user context or privileged issuer) and present it to `slurmrestd`. Configure `AuthAltTypes=auth/jwt` on the cluster and secure secrets; the manpage and `slurm.conf` docs explain JWT usage. ([Slurm][1], [Debian Manpages][2])
- **Visibility and least privilege**:

  - For the admin who fetches “all users” via `sacct`, prefer **AdminLevel=Operator** (read-only). ([Debian Manpages][2])
  - Be aware `PrivateData` can limit visibility of other users’ jobs; coordinate with cluster admins if outputs look sparse. (Documented under SlurmDBD/Slurm config.) ([Debian Manpages][2])

- **TLS**: Put `slurmrestd` behind HTTPS. If you must test with self-signed certs, set `SLURMRESTD_VERIFY_TLS=0` only in dev.
- **Fallback**: Keep your `sacct` fallback; it aligns with Slurm’s own tools. Supported flags are in the man page (`-S/-E/--format/--parsable2`). ([Slurm][1])

---

### 5) Environment variables (app)

- `SLURMRESTD_URL` — e.g. `https://slurm-ctl.example:6820`
- `SLURMRESTD_TOKEN` — (optional) JWT string for the calling user
- `SLURMRESTD_VERSION` — e.g. `v0.0.39` (match your cluster) ([Slurm][1])
- `SLURMRESTD_VERIFY_TLS` — `1` (default) / `0` (dev)
- `PAM_SERVICE` — PAM stack name to use (default `login`)

---

### 6) Quick test runbook

1. **REST up?**

   - From a node that can reach the daemon:

     ```bash
     export SLURM_JWT=$(scontrol token)   # as the test user
     curl -s --fail \
       -H "X-SLURM-USER-TOKEN: $SLURM_JWT" \
       "$SLURMRESTD_URL/slurm/v0.0.39/jobs?start_time=2025-01-01T00:00:00&end_time=2025-12-31T23:59:59" | jq .
     ```

     (Header/cookie names are in the slurmrestd man page.) ([Slurm][1])

2. **App fetch**

   - Set `SLURMRESTD_URL` (and `SLURMRESTD_TOKEN` for your user).
   - Hit your app’s Usage page; confirm `data_source=slurmrestd`.

3. **Admin visibility**

   - If admin needs all users via `sacct`, ensure AdminLevel is set (Operator/Admin) and that relevant `PrivateData` settings don’t hide data; re-run. ([Debian Manpages][2])

---

### 7) Optional: using `/slurmdb` endpoints

If you prefer pulling **historical accounting** directly through REST (instead of CLI `sacct`), enable the **slurmdb OpenAPI plugin** in `slurmrestd`. The docs show `openapi` plugins (including `slurmdb`), and you can query paths under `/slurmdb/v…`. Use the same JWT token mechanism.

---

### References

- **slurmrestd man page** — auth (JWT), header/cookie names, examples. ([Slurm][1])
- **sacct man page** — flags/formatting for accounting data. ([Slurm][1])
- **slurmrestd OpenAPI plugins** — includes `slurmdb` for accounting.
- **slurm.conf (Debian manpage)** — JWT configuration (`AuthAltTypes=auth/jwt`, token notes). ([Debian Manpages][2])
- **slurmdbd.conf (SchedMD)** — `AdminLevel`, `PrivateData`, accounting visibility/roles. ([Debian Manpages][2])

---

### TL;DR hand-off

- **You only edit one place** inside the app’s data flow: `fetch_from_slurmrestd()` in `services/data_sources.py` → call the new `services/slurmrest_client.query_jobs()`.
- **Login**: replace `verify_password()` with PAM (`python-pam`).
- **Cluster**: enable `slurmrestd` + JWT, ensure accounting and AdminLevel for operators.

That’s it — the rest of the pipeline (costs, receipts, admin tables) continues to work unchanged.

[1]: https://slurm.schedmd.com/slurmrestd.html "Slurm Workload Manager - slurmrestd"
[2]: https://manpages.debian.org/experimental/slurm-client/slurm.conf.5 "slurm.conf(5) — slurm-client — Debian experimental — Debian Manpages"

## Features:

### UI & UX

- **Global shell**

  - Animated blue/purple **wavy background** with reduced-motion fallback.
  - Centered **site panel** (cards/tables/forms styling, sidebar nav, tabs, chips).
  - **Header/nav** (left/right groups, logo/brand).
  - **Favicon/logo** wired so a tiny icon shows on every tab.
  - **i18n** (Flask-Babel) with `/i18n/set` to switch language; cookie respected.

- **Auth screens**

  - Login form with **full-width button** and inline **error/status message** (no flash).
  - CSRF tokens on forms; custom CSRF error page.

- **Playground**

  - **HPC Cost Playground** that fetches `/formula?type=…`, shows current per-hour rates, live recalculation & breakdown.

- **User pages**

  - **/me** “My Usage”: filter by “completed before”, **detail/aggregate/billed** tabs.
  - **Create Receipt** (server filters out already-billed), **CSV download**, receipt list & receipt detail pages.

- **Admin pages**

  - Sidebar sections: **Change Rate**, **Usage Tables (all users)**, **My Usage (admin’s own)**, **Billing**, **Audit**.
  - **Change Rate** for `mu|gov|private` tiers.
  - **Usage Tables** (detail + aggregate across users) with totals.
  - **My Usage** mirrors user flow (detail/aggregate/billed), **Create Receipt** for self, **my.csv** export.
  - **Billing**: list **pending** receipts, **Mark as paid**, **paid.csv** export.
  - **Audit**: recent audit table and **audit.csv** export.

### API & Services

- **Rates API**

  - `GET /formula?type=tier` → current rates (THB, per-hour).
  - `POST /formula` (admin-only) → update tier rates.

- **Data ingestion**

  - `data_sources.fetch_jobs_with_fallbacks(start,end, username?)` with cascade:

    1. **slurmrestd** client hook (standalone helper; configurable)
    2. **sacct** CLI (`--parsable2`, end-time cutoff, optional user filter)
    3. **test.csv** fallback (path configurable)

  - Uniform DataFrame with `User, JobID, Elapsed, TotalCPU, ReqTRES, End, State`.
  - **Cost computation** via `services.billing.compute_costs` (adds CPU/GPU/MEM hours, tier, Cost (฿)).

- **Billing store**

  - SQLite models with **receipts** and **receipt_items** (UNIQUE job_key to prevent double-billing).
  - Helpers: `create_receipt_from_rows`, `list_receipts`, `get_receipt_with_items`,
    `billed_job_ids`, `canonical_job_id`, `list_billed_items_for_user`,
    admin helpers (`admin_list_receipts`, `mark_receipt_paid`, `paid_receipts_csv`).

### Auth & Security

- **Login/Logout** with Flask-Login; roles (`admin|user`) and `admin_required` decorator.
- **CSRF** protection (Flask-WTF) + global `csrf_token()` in Jinja.
- **Temporary lockout / throttling**

  - SQLite **auth_throttle** table.
  - Configurable: `AUTH_THROTTLE_MAX_FAILS`, `AUTH_THROTTLE_WINDOW_SEC`, `AUTH_THROTTLE_LOCK_SEC`.
  - **Inline messages** for “locked” and “invalid credentials”.

- **Route hygiene**

  - Admin blocked from `/me` (redirect), admins land on **/playground** not `/me`.
  - Login always redirects to **/playground** (ignore `next`).

### Auditing & Observability

- **Tamper-evident audit log** (hash-chained entries) with indexes.
- Context captured: timestamp (UTC), actor, IP, UA, method, path, action, target, status, extra, prev_hash, hash.
- **Audited actions** (examples already wired):

  - `auth.login.success|failure|lockout.start|lockout.active|lockout.end|logout`
  - `rates.update.form`
  - `receipt.paid`
  - (If you added earlier as discussed) `receipt.create` for user/admin

- **Export & viewing**: `/admin/audit` (last N), `/admin/audit.csv`.
- **App logging**: rotating file logs, per-request timing via before/after request.

### Config & Ops

- **Instance path** setup, SQLite initializers for billing/users/audit/throttle.
- **Admin seeding** (ENV `ADMIN_PASSWORD`); optional demo users via `SEED_DEMO_USERS`.
- **Env-driven** file paths (DB, fallback CSV), slurmrestd URL/token (via helper).
- **Internationalization** defaults (`en`, `th`).

### Little QoL touches

- Totals chips (CPU/GPU/MEM/Elapsed), grand total badges.
- CSV exports for user/admin usage and paid receipts.
- Responsive header/grid, accessible animations, aria-live login status.

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
