# Requirements (SRS)

> Grounded in the current codebase. This is the minimal-but-complete spec for what the app does today and what it **must** keep doing.

---

## 1. Purpose & scope

The **HPC Billing Platform** prices Slurm job usage, lets users generate receipts, and (optionally) reconciles online payments via provider webhooks. It exposes a small JSON API (rates, health) and HTML/CSV UIs for users and admins.

Out of scope: quota enforcement, scheduler control, sophisticated accounting policy, user PII beyond usernames.

---

## 2. Stakeholders & roles

- **End user** (`role=user`): views usage, exports CSV, creates receipts.
- **Admin/Finance** (`role=admin`): edits rates, views billing/usage across users, audits activity, can mark receipts paid (manual reconciliation).
- **Ops**: deploys/monitors app, DB, and integrations (Slurm, payment provider).

---

## 3. Functional requirements

### 3.1 Authentication & session (FR-A)

- **FR-A1**: Users can sign in via username/password; sessions are cookie-based.
- **FR-A2**: Log out invalidates the session.
- **FR-A3**: Throttling must limit repeated failed logins per `(username, IP)` and temporarily lock after threshold.
- **FR-A4**: CSRF protection is required for all session-backed POSTs (forms & JSON), except the payments webhook.

### 3.2 Usage viewing & export (FR-U)

- **FR-U1**: A signed-in user can view their Slurm usage within a selectable date window; views: `detail`, `aggregate`, `billed`.
- **FR-U2**: The user can export usage as CSV with `start` / `end` query params (defaults last 7 days).
- **FR-U3**: The system must normalize job rows from sources (`slurmrestd`, fallback `sacct`, fallback CSV) to a common schema.

### 3.3 Receipt creation & viewing (FR-R)

- **FR-R1**: A user can create a **receipt** of **unbilled** jobs up to a chosen cut-off date.
- **FR-R2**: The system computes per-job resource hours and cost, and stores a header (total) + item rows.
- **FR-R3**: Duplicate billing is prevented via a **globally unique job key**; if any job is already billed, creation fails atomically.
- **FR-R4**: Users can view a list of their receipts and view each receipt (read-only).

### 3.4 Rates management (FR-P)

- **FR-P1**: Admins can view and update tiered rates (`mu`, `gov`, `private`) for CPU/GPU/MEM per hour.
- **FR-P2**: JSON endpoint `GET /formula` returns current rates and supports **ETag**; `POST /formula` updates one tier (admin + CSRF).

### 3.5 Payments (optional) (FR-M)

- **FR-M1**: A user can start an online payment for a receipt; the app creates a local Payment row and redirects to a provider-hosted checkout.
- **FR-M2**: The app accepts **signed** provider webhooks at `POST /payments/webhook` and finalizes success **only** when signature verifies **and** amount/currency match the local Payment row.
- **FR-M3**: Webhook processing is **idempotent** using a unique `(provider, external_event_id)`.
- **FR-M4**: Admin can **mark paid** manually for reconciliation; action is audited.
- **FR-M5**: A dev-only **simulate** flow triggers a success webhook locally for testing.

### 3.6 Admin console (FR-ADM)

- **FR-ADM1**: Admin can switch sections (rates, usage, billing, my usage, audit).
- **FR-ADM2**: Admin can export **paid receipts**, **own usage**, and **audit** as CSV.

### 3.7 Health & metrics (FR-O)

- **FR-O1**: `GET /healthz` returns 200 when process is alive.
- **FR-O2**: `GET /readyz` returns 200 only when DB is reachable; otherwise 500.
- **FR-O3**: `GET /metrics` (when enabled) exposes Prometheus metrics using a **dedicated** registry; series pre-warmed to avoid empty dashboards.

### 3.8 Internationalization (FR-I18N)

- **FR-I18N1**: Language can be switched via `POST /i18n/set` (`en`/`th`) and persisted in a cookie.

---

## 4. External interfaces (routes)

### User-facing

- `GET /me` (HTML) with `before`, `view=detail|aggregate|billed`
- `GET /me.csv` (CSV) with `start`, `end`
- `POST /me/receipt` (form) create receipt
- `GET /me/receipts` (HTML), `GET /me/receipts/<rid>` (HTML)

### Admin

- `GET /admin?section=...`
- `POST /admin` (update rates form)
- `POST /admin/receipts/<rid>/paid`
- `GET /admin/paid.csv`, `/admin/my.csv`, `/admin/audit`, `/admin/audit.csv`
- `POST /admin/tiers` (save user tier overrides)

### Payments

- `GET /payments/receipt/<rid>/start` (redirect)
- `GET /payments/thanks` (status page)
- `GET /payments/simulate` (dev)
- `POST /payments/webhook` (CSRF-exempt)

### JSON & ops

- `GET /formula`, `POST /formula`
- `GET /healthz`, `GET /readyz`, `GET /metrics`

### Auth

- `GET /login`, `POST /login`, `POST /logout`
- `POST /i18n/set`

###

- `POST /copilot/ask` (JSON)
- `POST /copilot/reindex (admin/ops)`
- `GET /copilot/widget.js`

---

## 5. Data requirements

### Entities (minimum)

- **users**: `username (PK)`, `password_hash`, `role`, `created_at`
- **rates**: `tier (PK)`, `cpu`, `gpu`, `mem`, `updated_at`
- **receipts**: `id (PK)`, `username`, `start`, `end`, `total`, `status`, `paid_at`, `method`, `tx_ref`, `created_at`
- **receipt_items**: `receipt_id`, `job_key (UNIQUE)`, `job_id_display`, resource-hours, `cost`
- **payments**: `id`, `receipt_id`, `username`, `provider`, `status`, `currency`, `amount_cents`, `external_payment_id`, timestamps
- **payment_events**: `id`, `payment_id`, `provider`, `external_event_id (UNIQUE per provider)`, `event_type`, `signature_ok`, `raw`, `received_at`
- **audit_log**: `id`, `ts`, `actor`, `action`, `status`, `target`, `prev_hash`, `hash`, `extra`
- **auth_throttle**: `(username, ip) UNIQUE`, window, counters, `locked_until`
- **user_tier_overrides**: `username (PK)`, `tier`, `updated_at`

### Derived fields

- **job_key**: canonicalized unique identifier for a job (prevents double billing).
- **resource hours**: CPU core-hours, GPU hours, Mem GB-hours derived from Slurm fields.
- **effective tier**: override_tier if present else natural classifier.

---

## 6. Non-functional requirements

### Security (NFR-S)

- **NFR-S1**: CSRF on all session POSTs except webhook.
- **NFR-S2**: Verify webhook signature and **amount+currency** match before marking success.
- **NFR-S3**: Unique constraints enforce idempotency (`job_key`, `(provider, external_event_id)`).
- **NFR-S4**: Passwords stored as salted hashes; session cookies set with `Secure`, `HttpOnly`, `SameSite` in prod.
- **NFR-S5**: Audit log is **hash-chained** and append-only.

### Performance & availability (NFR-P)

- **NFR-P1**: `/readyz` and `/healthz` respond within 200 ms under nominal load.
- **NFR-P2**: p95 request latency ≤ 500 ms for typical pages.
- **NFR-P3**: Metrics endpoint must not include default Python process collectors unless explicitly enabled (dedicated registry).

### Reliability & ops (NFR-R)

- **NFR-R1**: App starts without Slurm; UI still works using CSV fallback if configured.
- **NFR-R2**: Errors are logged; request logs include method, path, status, latency.
- **NFR-R3**: Metrics can be disabled via env without breaking startup.

### Privacy (NFR-PR)

- **NFR-PR1**: Store minimal PII (username only).
- **NFR-PR2**: Payment PAN/CVV never touch the app; only provider identifiers stored.

### i18n/UX (NFR-U)

- **NFR-U1**: English and Thai labels/messages available where implemented; missing translations default to English.

---

## 7. Integrations

### Slurm

- Primary: **slurmrestd** over HTTPS with auth.
- Fallback: `sacct` CLI.
- Last resort (dev/demo): CSV mounted read-only (`FALLBACK_CSV`).

### Payments

- Pluggable provider registry; “dummy” provider available for dev.
- Webhook secret provided via env; success path updates both Payment and Receipt in one transaction.

### Observability

- Prometheus scrapes `/metrics`.
- Health & readiness endpoints for probes/LB.

### Copilot / Ollama

- Embeddings and chat via **Ollama** HTTP API.
- Indexes Markdown under `COPILOT_DOCS_DIR`; vectors cached under `COPILOT_INDEX_DIR`.

---

## 8. Configuration (env)

Minimum set (names as used by code):

- `APP_ENV` (`development|production`)
- `FLASK_SECRET_KEY`
- `DATABASE_URL`
- `ADMIN_PASSWORD` (first run/seed)
- `SEED_DEMO_USERS` (dev)
- `DEMO_USERS` (dev list)
- `FALLBACK_CSV` (optional)
- `SLURMRESTD_URL` (+ token/certs as applicable)
- `METRICS_ENABLED` (default on)
- `PAYMENT_PROVIDER`, `PAYMENT_CURRENCY`, `PAYMENT_WEBHOOK_SECRET`, `SITE_BASE_URL`
- `COPILOT_ENABLED` (default `true`), `COPILOT_DOCS_DIR`, `COPILOT_INDEX_DIR`, `OLLAMA_BASE_URL`, `COPILOT_EMBED_MODEL`, `COPILOT_LLM`, `COPILOT_TOP_K`, `COPILOT_MIN_SIM`, `COPILOT_RATE_LIMIT_PER_MIN`

---

## 9. Constraints & assumptions

- Stateless web app; all persistence in DB.
- No email or notification subsystem (out of scope).
- Adminer & Swagger UI are **dev tools**; not exposed in production.
- “Void receipt” behavior is **not** exposed via UI (reserved).

---

## 10. Acceptance criteria (system tests)

1. **Login throttle**: N bad passwords → lock; success after window; audit entries present.
2. **Usage view**: `/me` shows jobs for the window; CSV matches the table total rows.
3. **Receipt**: creating a receipt for overlapping jobs fails if any job already billed; otherwise succeeds and total > 0 (when usage exists).
4. **Rates API**: `GET /formula` returns JSON + `ETag`; `If-None-Match` returns 304 when unchanged; `POST /formula` updates a tier (admin only).
5. **Webhook idempotency**: posting the same signed event twice updates payment/receipt once; second call is a no-op; both calls 200.
6. **Manual paid**: admin POST to mark paid flips receipt status and audits the action.
7. **Readiness**: when DB stops, `/readyz` returns 500; when DB returns, `/readyz` returns 200.
8. **Metrics**: `/metrics` responds with Prometheus text; includes pre-warmed series (counters at 0).
9. **Tier overrides**:
   - Setting an override changes the effective tier used in pricing.
   - Choosing the natural tier removes the override row.
   - Actions appear in audit.

---

## 11. Nice-to-have (not required for current release)

- Admin “view as user” (impersonation) with audit.
- Public, read-only pages (pricing/cluster status).
- Optional self-registration (admin approval).

---
