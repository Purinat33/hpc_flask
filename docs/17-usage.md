# User & Admin Handbook

> A friendly guide to using the HPC Billing Platform day-to-day. This is written for **end users** (researchers) and **admins/finance**. It complements the technical books with step-by-step instructions, tips, and guardrails.

---

## 1) Quick tour

- **Login:** `/login`
- **My usage:** `/me` (views: `detail`, `aggregate`, `billed`)
- **Export CSV:** `/me.csv?start=YYYY-MM-DD&end=YYYY-MM-DD`
- **Create receipt:** `POST /me/receipt` (from the “My usage” page)
- **My receipts:** `/me/receipts` → view any receipt
- **Payments:** `/payments/receipt/<rid>/start` → (provider checkout) → `/payments/thanks`
- **Language:** `POST /i18n/set` (`lang=en` or `th`)
- **Admin console:** `/admin` (sections: **rates**, **usage**, **billing**, **myusage**, **audit**, **dashboard**)
- **API Explorer (dev):** Swagger UI at `http://localhost:8081`

---

## 2) For end users

### 2.1 Sign in & language

1. Open `/login`.
2. Enter your username/password.
3. (Optional) Switch language via the footer or `POST /i18n/set`.

> If you mistype passwords too many times, a **temporary lock** will apply. Wait for the countdown or contact your admin.

### 2.2 See your usage

- Go to `/me`.
- Choose a **date window** (defaults to a recent window).
- Pick a view:

  - **Detail** – one row per Slurm job.
  - **Aggregate** – grouped totals (CPU/GPU/MEM hours, cost).
  - **Billed** – shows which jobs are already on receipts.

### 2.3 Download your usage (CSV)

- Click “Export CSV” or use:

```
/me.csv?start=2025-09-01&end=2025-09-13
```

Open the CSV in Excel/Sheets for your analysis.

> Tip: If the range is short and you see no rows, try extending the window (jobs can finish near midnight).

### 2.4 Create a receipt (turn usage into a bill)

1. On `/me`, choose **a cut-off date** (e.g., _before_ today).
2. Click **Create receipt**.
3. The system:

   - Re-fetches your usage for the window.
   - **Excludes** any job previously billed (safety).
   - Prices each job using the **current tier rates**, then **snapshots** those rates onto the receipt (so totals won’t change later).
   - Saves the receipt and its **line items**.

4. You’ll be redirected to **My receipts**.

> Guardrail: If _any_ job in your selection was already billed, creation **fails atomically** (nothing saved). Adjust the date and retry.

### 2.5 Pay online (if enabled)

- From a receipt, click **Pay** → you’ll be redirected to the provider’s hosted checkout.
- After payment, the **provider posts a signed webhook** to us. When verified:

  - The **payment** becomes `succeeded` (or `failed`/`canceled`).
  - The **receipt** becomes `paid` (only if the payment `succeeded`).

You can revisit `/payments/thanks?rid=<id>` to see the latest status.

> If a payment completes at the provider but your receipt still shows _pending_, ask the admin to **re-deliver** the webhook from the provider dashboard or reconcile manually.

---

## 3) For admins/finance

### 3.1 Admin console

Open `/admin`. Sections:

- **Dashboard** – KPIs & charts (last 90 days): daily total cost, cost by tier, top users.
- **Rates** – edit CPU/GPU/MEM hourly rates per tier (`mu`, `gov`, `private`).
- **Usage** – inspect raw Slurm data vs. computed costs (helps QA).
- **Billing** – list of receipts (pending/paid); mark paid manually when needed.
- **My usage** – your own usage (handy for testing).
- **Audit** – timeline of important actions; export CSV.

> All admin POST actions require **CSRF** (the UI includes this automatically).

### 3.2 Update rates

- Use **Rates** (form) or the JSON API (`POST /formula`).
- Changes affect **future** pricing only; existing receipts are **immutable** due to the rate snapshot on each receipt.

Checklist:

- [ ] Confirm tier values (CPU/GPU/MEM per hour).
- [ ] Save → verify via `GET /formula` (ETag changes).
- [ ] Create a small test receipt to confirm totals.

### 3.3 Mark a receipt “paid” (manual)

If payment happened offline (bank transfer) or the gateway can’t re-send the webhook:

1. Open the receipt in **Billing**.
2. Click **Mark as paid**.
3. Enter reference notes (e.g., transfer ID) if prompted.
4. Submit → writes an **audit** entry and sets `paid_at`.

### 3.4 Exports for finance

- **Paid receipts**: `/admin/paid.csv`
- **Audit log**: `/admin/audit.csv`
- **Your admin usage**: `/admin/my.csv`

Store exports per your finance retention policy.

---

## 4) Payments—what “good” looks like

- Webhook arrives **once**, but replays are harmless (**idempotent**).
- We only accept events where **signature** is valid **and** `amount/currency` matches the expected local payment row.
- A successful event flips:

  - `payments.status → succeeded`
  - `receipts.status → paid` (+ `paid_at`, `method/tx_ref`)

**If stuck pending:** check provider secret/URL; request a **re-delivery** from the provider’s dashboard. As a last resort, use **Mark paid** (manual).

---

## 5) Common tasks (copy/paste)

### 5.1 Scripted CSV pull

```bash
# After logging in via browser (or using the curl login flow)
curl -b cookies.txt \
  "http://localhost:8000/me.csv?start=2025-09-01&end=2025-09-13" \
  -o my_usage.csv
```

### 5.2 Admin: change rates via JSON

```bash
# Get CSRF token from /login or /admin first
curl -b cookies.txt -X POST http://localhost:8000/formula \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: <token>" \
  -d '{"tiers":[{"tier":"mu","cpu":0.02,"gpu":1.5,"mem":0.001}]}'
```

### 5.3 Check health

```bash
curl -s http://localhost:8000/healthz
curl -si http://localhost:8000/readyz
```

### 5.4 (Dev) Simulate a successful payment

```bash
curl -b cookies.txt \
  "http://localhost:8000/payments/simulate?rid=123&external_payment_id=dev_123&amount_cents=1000&currency=THB" \
  -L
```

---

## 6) Tips & guardrails

- **No double billing**: duplicates are blocked via a **unique job key** per line item.
- **Receipts are immutable totals**: each receipt stores a **rate snapshot** at creation.
- **Webhooks are not CSRF-protected**: by design; they rely on **signatures + idempotency**.
- **Minimal PII**: we store only usernames + job metadata—no card data.

---

## 7) Troubleshooting quickies

- **No jobs appear** → Extend the date window; ensure Slurm is reachable. In dev, set a valid `FALLBACK_CSV`.
- **Receipt failed to create** → Some jobs were already billed; pick an earlier cut-off.
- **Payment succeeded but UI shows pending** → webhook not received/verified; admin should re-deliver or mark paid with an audit note.
- **CSRF error on POST** → session expired or missing token; reload the page and retry.
- **Admin Dashboard empty charts** → very new deployment or metrics disabled; usage window might be too short.

---

## 8) Glossary (mini)

- **Usage**: Slurm job metrics (CPU/GPU/MEM hours) used for pricing.
- **Receipt**: Your priced usage for a window, with immutable line items & **rate snapshot**.
- **Paid**: A receipt that’s settled (by online payment or manual reconciliation).
- **Webhook**: A signed callback from the payment provider confirming the outcome.
- **Audit**: An append-only, hash-chained log of important actions.
- **Tier**: Pricing category (`mu`, `gov`, `private`) applied per user.

---

## 9) Appendix: minimal “how pricing works”

For each job:

```
cost = cpu_core_hours * rate.cpu
     + gpu_hours      * rate.gpu
     + mem_gb_hours   * rate.mem
```

At receipt creation, the **current tier & per-unit rates are snapshotted** onto the receipt, and the total is the **sum of item costs**. Changing rates later **does not** change past receipts.

---
