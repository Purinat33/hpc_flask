# User & Admin Handbook

> A friendly guide to using the **HPC Billing Platform** day-to-day. Written for **end users** (researchers) and **admins/finance**. It complements the technical books with step-by-step instructions, tips, and guardrails.

---

## 1) Quick tour

- **Login:** `/login`
- **My usage:** `/me` (views: `detail`, `aggregate`, `billed`)
- **Export CSV:** `/me.csv?start=YYYY-MM-DD&end=YYYY-MM-DD`
- **Create receipt:** `POST /me/receipt` (from **My usage**)
- **My receipts:** `/me/receipts` → view any receipt
- **Pay a receipt:** `/payments/receipt/<rid>/start` → (provider checkout) → `/payments/thanks`
- **Language:** `POST /i18n/set` (`lang=en` or `th`)
- **Admin console:** `/admin` (sections: **rates**, **usage**, **billing**, **myusage**, **dashboard**, **audit**, **tiers**)
- **Docs Copilot (in-app help):** click the **❓ Help** button (bottom-right)
- **API Explorer (dev):** Swagger UI at `http://localhost:8081`

---

## 2) For end users

### 2.1 Sign in & language

1. Open `/login`.
2. Enter your username/password.
3. (Optional) Switch language via the footer or `POST /i18n/set`.

> Too many bad attempts trigger a **temporary lock** (per username+IP). Wait for the lock to expire or contact an admin.

---

### 2.2 See your usage

- Go to `/me`.
- Choose a **date window** (defaults sensibly).
- Pick a view:

  - **Detail** – one row per Slurm job.
  - **Aggregate** – grouped totals (CPU/GPU/MEM hours, cost).
  - **Billed** – jobs already included on receipts (historic).

**What’s shown**

- CPU **core-hours**, GPU hours, **MEM GB-hours**, job state, and your **pricing tier**.
- Costs are recomputed for display, but billing totals are **snapshotted** on receipts.

---

### 2.3 Download your usage (CSV)

Use the button on the page or call:

```
/me.csv?start=2025-09-01&end=2025-09-13
```

Open in Excel/Sheets for your own analysis.

> Tip: If nothing appears, extend the window—jobs that finish near midnight might land outside a very tight range.

---

### 2.4 Create a receipt (turn usage into a bill)

1. On `/me`, pick a **cut-off date** (e.g., _before_ today).
2. Click **Create receipt**.
3. The system:

   - Re-fetches your usage up to the cut-off.
   - **Excludes** anything already billed (safety).
   - Prices each job and **snapshots the tier & per-unit rates** onto the receipt.
   - Saves the header + line items atomically.

You’ll be redirected to **My receipts**.

> Guardrail: if **any** job in the selection was already billed, the entire creation **fails atomically** (no partial receipts). Adjust the cut-off and retry.

---

### 2.5 Pay online (if enabled)

- From a receipt, click **Pay** to open the provider’s **hosted checkout**.
- The provider sends a **signed webhook** back to us. When verified:

  - Your **payment** becomes `succeeded` (or `failed`/`canceled`).
  - Your **receipt** becomes `paid` (only when payment `succeeded`).

Revisit `/payments/thanks?rid=<id>` to see the latest status.

> If the provider shows “paid” but the receipt is still `pending`, ask an admin to trigger **webhook re-delivery** from the provider dashboard (or reconcile manually).

---

### 2.6 Docs Copilot (in-app help)

- Click the **❓ Help** button (bottom-right).
- Ask questions about this app (“How are CPU hours computed?”, “What is a receipt?”).
- Copilot answers **from the built-in docs** and shows **sources** it used.
- If global CSRF is enabled, the widget sends the token automatically for its requests.

> Rate limited per IP. If you see “Rate limit exceeded”, just try later.

---

## 3) For admins/finance

### 3.1 Admin console

Open `/admin`. Sections:

- **Dashboard** — KPIs & charts (last 90 days): daily cost trend, cost by tier, top users, plus node usage and energy/throughput snapshots where available.
- **Rates** — edit CPU/GPU/MEM hourly rates per tier (`mu`, `gov`, `private`).
- **Usage** — raw Slurm vs computed costs (QA/triage).
- **Billing** — receipts list (pending/paid); **mark paid** for offline payments.
- **My usage** — your own usage (handy for testing flows).
- **Audit** — recent sensitive actions; CSV export.
- **Tiers (User Tier Overrides)** — **new** per-user tier selector; searchable list.

All POST actions require **CSRF** (the UI includes this automatically).

---

### 3.2 Update rates

- Use **Rates** (form) or the JSON API (`POST /formula`).
- Changes apply to **future** receipts only; existing receipts are **immutable** because each stores a **pricing snapshot**.

Checklist:

- [ ] Confirm CPU/GPU/MEM rates for each tier.
- [ ] Save → verify via `GET /formula` (ETag changes).
- [ ] Create a tiny test receipt to sanity-check totals.

---

### 3.3 Mark a receipt “paid” (manual)

For bank transfers or missing webhooks:

1. Open **Billing** and locate the receipt.
2. Click **Mark as paid**.
3. Optionally note a reference (`tx_ref`).
4. The system sets `paid_at`, updates status, and logs an **audit** event.

---

### 3.4 User Tier Overrides (new)

Path: **Admin → Tiers**

- Use the **filter** box to find a user quickly.
- Select `MU` / `GOV` / `PRIVATE` for that user.
- **If you pick the same tier as the natural classifier**, the override is **removed** (clean state).
- Saving writes an **audit** entry per change (set/clear) and a summary.

Notes:

- Overrides affect **future** pricing only. Existing receipts remain unchanged (they carry their own rate snapshot).
- If you later remove an override, the user’s next pricing falls back to the **natural** classifier.

---

### 3.5 Exports for finance

- **Paid receipts**: `/admin/paid.csv`
- **Audit log**: `/admin/audit.csv`
- **Your admin usage**: `/admin/my.csv`

Retain per institutional finance policy.

---

## 4) Payments—what “good” looks like

- Webhooks may arrive more than once; we dedupe by **(provider, external_event_id)** → **idempotent**.
- We only mark a payment succeeded if **signature** validates **and** **amount/currency** match the local payment row.
- Upon success we flip:

  - `payments.status → succeeded`
  - `receipts.status → paid` and set `paid_at` (in one transaction)

If a receipt is stuck in `pending`, check the webhook secret/URL; request a **re-delivery** from the provider; as a last resort, **mark paid** manually (with notes).

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
# Get a CSRF token from /login or /admin first
curl -b cookies.txt -X POST http://localhost:8000/formula \
  -H "Content-Type: application/json" \
  -H "X-CSRFToken: <token>" \
  -d '{"type":"mu","cpu":2.5,"gpu":12.0,"mem":0.9}'
```

### 5.3 Health checks

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

- **No double billing**: a **globally unique job key** prevents the same job appearing on multiple receipts.
- **Receipts are immutable totals**: each stores `pricing_tier`, `rate_*`, and `rates_locked_at`.
- **Webhooks aren’t CSRF-protected**: by design; they rely on **signatures + idempotency**.
- **Docs Copilot**: answers only from docs; shows sources; subject to a per-IP rate limit.
- **Minimal PII**: we store usernames + usage metadata—no card data.

---

## 7) Troubleshooting quickies

- **No jobs appear** → Extend the date window; ensure Slurm is reachable. In dev, set a valid `FALLBACK_CSV`.
- **Receipt failed to create** → Some jobs were already billed; pick an earlier cut-off.
- **Payment succeeded but UI shows pending** → webhook not received/verified; re-deliver from provider or mark paid with an audit note.
- **CSRF error on POST** → session expired or missing token; reload the page and retry.
- **Admin Dashboard empty charts** → very new deployment or metrics disabled; widen the window.
- **Copilot says “disabled” or rate limited** → check `COPILOT_ENABLED`, per-IP rate limit, or rebuild the index via `/copilot/reindex` (admin-only).

---

## 8) Glossary (mini)

- **Usage** — Slurm job metrics (CPU/GPU/MEM hours) used for pricing.
- **Receipt** — Priced usage for a window, with immutable line items & **pricing snapshot**.
- **Paid** — A receipt settled by online payment or manual reconciliation.
- **Webhook** — Signed callback from the payment provider confirming outcome.
- **Audit** — Append-only, hash-chained log of sensitive actions.
- **Tier** — Pricing category (`mu`, `gov`, `private`) applied per user.
- **Tier override** — Admin-set per-user tier that supersedes the natural classifier; choosing the same as natural **clears** the override.
- **Docs Copilot** — In-app docs assistant (Ollama-backed) that retrieves from Markdown and cites sources.

---

## 9) Appendix: minimal “how pricing works”

For each job:

```
cost = cpu_core_hours * rate.cpu
     + gpu_hours      * rate.gpu
     + mem_gb_hours   * rate.mem
```

At receipt creation, the **current tier & per-unit rates are snapshotted** onto the receipt (`pricing_tier`, `rate_*`, `rates_locked_at`). The receipt total is the **sum of item costs**. Changing rates later **does not** change past receipts.

---
