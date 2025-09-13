# Testing

> How we verify the HPC Billing Platform works as designed—covering math, auth, permissions, payments/webhooks, UI flows, and tamper-evident auditing. This book is practical: it names the suites we run, the risks they catch, and how to run/extend them locally and in CI.

---

## 1) Test model at a glance

- **Unit/logic**

  - Cost math derives `CPU_Core_Hours`, `GPU_Hours`, `Mem_GB_Hours` from Slurm fields; rates applied; columns asserted.&#x20;
  - Rates read/write round-trip.&#x20;

- **AuthN/AuthZ & routes**

  - Login success/failure audited; key pages require login; role guards enforced in views and API. &#x20;

- **Permissions**

  - Owners can view/pay their receipts; admins can view but cannot initiate checkout for others; non-owners are redirected. &#x20;

- **Payments/webhooks**

  - Wrong amount/currency → ignored; event idempotency → replays are no-ops; happy path marks receipt paid and records payment. &#x20;

- **Receipts & double-billing**

  - Same job can’t be billed twice; duplicates are skipped atomically.&#x20;

- **Audit integrity**

  - Hash-chained audit log verifies; any change breaks the chain and is detected. &#x20;

- **UI smoke & i18n**

  - Root redirects; playground renders; locale cookie set; bad locale rejected. &#x20;

- **Admin workflows**

  - Rate editing persists via ORM; usage view filters billed jobs and aggregates totals; mark-paid endpoint + CSV export. &#x20;

---

## 2) How to run the tests

```bash
# From repo root
pytest -q
pytest -q -k payments           # a subset
pytest -q -vv --maxfail=1       # verbose, stop on first failure
```

**Database note.** Tests point the app at a dedicated Postgres (`TEST_DATABASE_URL`), create/drop schema once per session, and clean tables after each test to avoid cross-test bleed. The fixture also ensures the partial unique index used for idempotency exists in the test DB. &#x20;

The app fixture sets a throwaway instance path, disables CSRF (tests only), seeds demo users, and wires a CSV fallback for data-source tests. &#x20;

---

## 3) Suites & what they prove

### 3.1 Billing math & rates

- **`test_billing_math.py`** — Parses Slurm fields, computes resource hours, and emits a priced DataFrame (columns asserted).&#x20;
- **`test_rates_store.py`** — Rates persistence round-trips cleanly.&#x20;

### 3.2 Receipts, uniqueness, and exports

- **Double-billing guard** — Creating a receipt with already-billed jobs inserts zero items and adds no cost.&#x20;
- **Admin CSV / lists** — Admin listing, per-user billed/pending views, idempotent mark-paid, and paid-history CSV are exercised. &#x20;

### 3.3 Authentication, authorization, and permissions

- **Audit on auth** — Bad + good logins generate at least two audit rows.&#x20;
- **Role-protected API** — Updating rates without admin is blocked.&#x20;
- **Owner/admin rules** — Owners can view and start checkout; admins can view others’ receipts but **cannot** initiate their payments; non-owners are redirected. &#x20;

### 3.4 Payments & webhook security

- **Finalization checks** — Wrong `amount_cents` or `currency` keeps the receipt **pending** even with a valid signature. &#x20;
- **Idempotency** — Re-posting the same `event_id` is safe; only one `PaymentEvent` row is stored; the receipt ends in `paid`. &#x20;
- **Happy path** — End-to-end dummy flow: start → simulate → webhook → thanks; ORM shows `Receipt.status='paid'` and matching `Payment`. &#x20;
- **Safety on state** — Finalization refuses non-`pending` receipts.&#x20;

### 3.5 Audit chain integrity

- **Tamper-evident log** — Rehashing the chain passes before mutation and fails after changing an early row. &#x20;

### 3.6 UI (user/admin) & smoke

- **User pages** — Detail view hides billed jobs; aggregate view builds a single row; CSV download includes expected IDs; creating a receipt with no jobs audits a noop. &#x20;
- **Admin pages** — Rates section renders and persists; usage view filters out billed jobs and computes totals. &#x20;
- **Smoke/i18n** — Root redirect, playground render, locale cookie set, invalid locale rejected. &#x20;

---

## 4) Fixtures & environment

- **DB bootstrap** — Creates a dedicated test DB, (re)creates schema at session start, and ensures the unique partial index used for webhook idempotency exists (mirrors migration behavior). &#x20;
- **Per-test cleanup** — After each test, only tables touched in tests are truncated to keep runs fast and isolated.&#x20;
- **App fixture** — Seeds users `alice`/`bob`, disables CSRF (tests only), and wires a read-only CSV for fallback ingestion tests.&#x20;

---

## 5) What risks these tests mitigate

| Risk/bug we’ve seen in the wild                     | How the suite catches it                              |
| --------------------------------------------------- | ----------------------------------------------------- |
| Mis-computed costs from `Elapsed`/`ReqTRES` parsing | Column presence & hour math assertions in cost tests. |
| Accidental double-billing on re-ingest              | Duplicate jobs produce zero inserts/zero total.       |
| Broken RBAC or IDOR on receipts                     | Owner/admin/non-owner paths enforced.                 |
| Webhook spoofing/replay                             | Amount/currency checks + event idempotency.           |
| Audit tampering                                     | Chain rehash fails after mutation.                    |

---

## 6) Adding new tests (patterns to follow)

- **Controller context without parsing HTML**: capture Jinja context via `template_rendered` and assert keys/values. (See `captured_templates` helpers.)&#x20;
- **Monkeypatch at the right import site**: patch the symbol **inside** the controller that imported it, not the original module.&#x20;
- **ORM verification for critical writes**: after an endpoint action, open a short session and assert persisted rows/fields.&#x20;
- **Idempotency/negative paths**: always test the happy path **and** the “ignored” or “no-op” path (duplicates, wrong signature/amount, voided receipt, etc.). &#x20;

---

## 7) CI hints

- Run `pytest -q` on every PR; fail fast on payments/webhooks/auth suites (they guard money and access).
- Add dependency/container scanners in CI (see **Security** book), but keep test DB setup identical to local fixtures to avoid “works on my machine.”
- Cache wheels/venv between CI runs for speed; do **not** cache the database.

---

## 8) Troubleshooting

- **Stale data between tests** → ensure per-test cleanup fixture ran (tables wiped after each test).&#x20;
- **Tests can’t connect to Postgres** → confirm `TEST_DATABASE_URL` and that the bootstrap created the DB (the fixture will create it if your role allows). &#x20;
- **Admin/rates tests failing** → check that the app fixture seeded `admin` with the expected password and that CSRF is disabled in tests.&#x20;

---

### TL;DR

Our tests simulate real user/admin flows (including paying and downloading CSVs), enforce RBAC, prove idempotency on money-moving webhooks, prevent double-billing, and make audit tampering obvious. Run them often; extend them when you add routes, change data models, or touch payments.
