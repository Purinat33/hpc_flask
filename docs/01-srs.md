# Requirements (SRS)

> Grounded in the current codebase. This is the minimal‑but‑complete spec for what the app does **today** and what it **must** keep doing.

---

## 1. Purpose & scope

The **HPC Billing Platform** prices Slurm job usage, lets users and admins inspect usage, and produces invoices (PDF) and accounting exports (CSV/ZIP). It supports previewing a derived journal and posting to a lightweight GL with period close/reopen.

**Out of scope:** quota enforcement, scheduler control, self‑service registration, payment card handling, PII beyond usernames.

---

## 2. Stakeholders & roles

- **End user** (`role=user`): views own usage and receipts; downloads CSV and PDFs.
- **Admin/Finance** (`role=admin`): views all usage, adjusts rates, generates invoices, marks paid/revert (subject to eligibility), runs accounting exports, manages tier overrides, manages accounting periods.
- **Ops**: deploys/monitors app, DB, and optional integrations (Slurm REST, LLM Copilot).

---

## 3. Functional requirements

### 3.1 Authentication & session (FR‑A)

- **FR‑A1**: Sign in with username/password; cookie session.
- **FR‑A2**: Log out invalidates the session.
- **FR‑A3**: Throttling limits repeated failed logins per `(username, IP)` and temporarily **locks** after threshold; lock/unlock events are audited and metered.
- **FR‑A4**: CSRF protection for all session‑backed POSTs (forms & JSON); explicitly exempt only JSON tools that are designed for unauth (e.g., Copilot ask). _(Payments webhook endpoints are not currently enabled.)_

### 3.2 Usage viewing & export (FR‑U)

- **FR‑U1**: A signed‑in user can view their usage in `/me` with `view=detail|aggregate|billed|trend` and a `before=YYYY‑MM‑DD` cut‑off.
- **FR‑U2**: The user can export usage as CSV at `/me.csv` with `before` (default: today). Export covers `1970‑01‑01..before` and includes computed cost columns.
- **FR‑U3**: The system normalizes job rows from **slurmrestd** (primary), **sacct** (fallback), or **CSV** (last resort) into a common schema; cost columns are added deterministically.

### 3.3 Receipts (FR‑R)

- **FR‑R1**: **Admins** can create a personal receipt ("My usage") up to a cut‑off date; bulk create for a month groups by user and creates one receipt per user (skips blanks and already billed jobs).
- **FR‑R2**: System computes resource hours and cost per job and stores a header + item rows; header includes locked snapshot of tier rates and tax fields.
- **FR‑R3**: Duplicate billing is prevented by a **globally unique `job_key`** across all `receipt_items`.
- **FR‑R4**: Users can view a list of their receipts and each receipt (read‑only); PDFs available in English and Thai.
- **FR‑R5**: Admin can **mark paid**; admin can **revert** to pending **only if** no external payment is linked and no downstream lock flags (GL export / e‑tax / customer sent) are present.

### 3.4 Rates management (FR‑P)

- **FR‑P1**: Tiers `mu|gov|private` with per‑hour rates for CPU/GPU/MEM; editable via Admin UI.
- **FR‑P2**: JSON endpoints `GET /formula` (with **ETag**) and `POST /formula` (admin + CSRF) read/update a single tier.

### 3.5 Tier overrides (FR‑T)

- **FR‑T1**: Admin can set/clear **per‑user tier overrides**.
- **FR‑T2**: Pricing uses the override tier when present; otherwise a natural classifier (MU/Gov/Private) is used.
- **FR‑T3**: All changes are audited with before/after info.

### 3.6 Admin console (FR‑ADM)

- **FR‑ADM1**: Sections: `dashboard`, `usage`, `billing`, `myusage`, `rates`, `tiers`, `audit`.
- **FR‑ADM2**: CSV exports: **paid receipts**, **admin’s own usage**, **audit log**.
- **FR‑ADM3**: Forecast and pricing simulation JSON endpoints exist for UI charts.

### 3.7 Accounting (FR‑GL)

- **FR‑GL1**: Derive a **journal** (preview) and maintain a **posted GL** that respects period locks.
- **FR‑GL2**: Admin can **close** and **reopen** accounting periods; closing can run service accruals and posts a closing batch.
- **FR‑GL3**: Exports: posted GL to CSV, Xero‑compatible CSVs (sales/bank), and a formal GL ZIP with manifest/HMAC.
- **FR‑GL4**: Provide ledger views (HTML), CSV download, and PDF (Thai/English layout).

### 3.8 Health, readiness & metrics (FR‑O)

- **FR‑O1**: `GET /healthz` returns 200 when process is alive.
- **FR‑O2**: `GET /readyz` returns 200 only when DB is reachable; otherwise 500.
- **FR‑O3**: `GET /metrics` (when enabled) exposes a dedicated Prometheus registry; common series are pre‑warmed (0‑counts).

### 3.9 Internationalization (FR‑I18N)

- **FR‑I18N1**: Language switch via `POST /i18n/set` (`en`/`th`); stored in a cookie; PDFs have EN/TH templates.

### 3.10 Copilot (optional) (FR‑C)

- **FR‑C1**: If enabled, endpoints provide a JS widget, a `POST /copilot/ask` handler (CSRF‑exempt), and an admin reindex.
- **FR‑C2**: Uses local embeddings (Ollama) over Markdown under a configured docs directory.

> **Payments**: Online payment webhook/checkout endpoints are **not registered** in the current build. Manual mark‑paid is supported and audited.

---

## 4. External interfaces (routes)

### User‑facing

- `GET /me` – views `detail|aggregate|billed|trend` with `before`.
- `GET /me.csv` – CSV export with `before`.
- `GET /me/receipts` – list own receipts.
- `GET /me/receipts/<rid>` – receipt detail.
- `GET /me/receipts/<rid>.pdf` – invoice PDF (EN).
- `GET /me/receipts/<rid>.th.pdf` – invoice PDF (TH).

### Admin

- `GET /admin` – sections via `?section=rates|usage|billing|myusage|dashboard|tiers` and view switches.
- `POST /admin` – update rates (form submit in the rates section).
- `POST /admin/my/receipt` – create **admin’s own** receipt up to cut‑off.
- `POST /admin/invoices/create_month` – bulk create monthly receipts (by user).
- `POST /admin/invoices/revert_month` – bulk revert eligible paid receipts in window.
- `POST /admin/receipts/<rid>/paid` – mark paid (manual).
- `POST /admin/receipts/<rid>/revert` – revert to pending (eligibility enforced).
- `GET /admin/paid.csv`, `GET /admin/my.csv` – CSV exports.
- `GET /admin/audit` (HTML), `GET /admin/audit.csv`, `GET /admin/audit.verify.json`.
- `GET /admin/ledger` (HTML), `GET /admin/ledger.csv` – derived journal; `GET /admin/export/ledger.csv` – posted GL export.
- `GET /admin/receipts/<rid>.etax.json` and `.etax.zip` – unsigned e‑tax payload and ZIP bundle.
- `GET /admin/receipts/<rid>.pdf` and `.th.pdf` – admin‑side PDFs.
- `GET /admin/export/xero_sales.csv`, `GET /admin/export/xero_bank.csv` – convenience exports.
- `POST /admin/tiers` – upsert/clear user tier overrides.
- `POST /admin/periods/<YYYY>/<MM>/close` and `/reopen` – period control; also `POST /admin/periods/bootstrap`.
- `POST /admin/export/gl/formal.zip` – generate formal posted‑GL ZIP (manifest + HMAC).
- JSON helpers: `GET /admin/simulate_rates.json`, `GET /admin/forecast.json`.

### JSON & ops

- `GET /formula` (ETag), `POST /formula` (admin).
- `GET /healthz`, `GET /readyz`, `GET /metrics`.

### Auth & i18n

- `GET /login`, `POST /login`, `POST /logout`, `POST /i18n/set`.

### Copilot (when enabled)

- `GET /copilot/widget.js`, `POST /copilot/ask`, `POST /copilot/reindex`.

---

## 5. Data requirements

### Entities (minimum)

- **users**: `username (PK)`, `password_hash`, `role`, `created_at`.
- **rates**: `tier (PK)`, `cpu (DECIMAL)`, `gpu (DECIMAL)`, `mem (DECIMAL)`, `updated_at`.
- **receipts**: `id (PK)`, `username`, `start`, `end`, `status('pending|paid|void')`,
  money: `currency`, `subtotal`, `tax_label?`, `tax_rate%`, `tax_amount`, `tax_inclusive`, `total`,
  lifecycle: `paid_at?`, `method?`, `tx_ref?`, approvals: `invoice_no? (UNIQUE)`, `approved_by?`, `approved_at?`, `created_at`, `pricing_tier`, `rate_cpu`, `rate_gpu`, `rate_mem`, `rates_locked_at`.
- **receipt_items**: `receipt_id + job_key (PK)`; `job_key (UNIQUE)`, `job_id_display`, `cpu_core_hours`, `gpu_hours`, `mem_gb_hours`, `cost`.
- **payments** _(present, not wired to UI)_: `id`, `receipt_id (FK)`, `username`, `provider`, `status`, `currency`, `amount_cents`, `external_payment_id (UNIQUE)`, timestamps.
- **payment_events**: `(provider, external_event_id) UNIQUE`, `payment_id?`, `event_type`, `signature_ok`, `raw`, `received_at`.
- **audit_log**: `id`, `ts`, `actor`, `action`, `status`, `target_type`, `target_id`, `prev_hash`, `hash`, `ip_fingerprint`, `ua_fingerprint`, `request_id`, `extra(JSON, limited keys)`.
- **auth_throttle**: `username`, `ip` (UNIQUE together), `window_start`, `fail_count`, `locked_until`.
- **user_tier_overrides**: `username (PK)`, `tier`, `updated_at`.
- **GL**: `gl_batches`, `gl_entries`, `gl_export_runs`, `gl_export_run_batches`, `accounting_periods` (fields include period year/month, status, source refs, export metadata, HMACs, etc.).

### Derived fields

- **job_key**: canonicalized job identifier (`12345` from `12345.batch` etc.) preventing double billing.
- **resource hours**: CPU core‑hours, GPU hours, Mem GB‑hours derived from Slurm fields.
- **effective tier**: override tier if present else natural classifier.

---

## 6. Non‑functional requirements

### Security (NFR‑S)

- **NFR‑S1**: CSRF on all session POSTs except explicitly exempt tooling endpoints.
- **NFR‑S2**: Audit log is **hash‑chained**, append‑only, with HMAC over selected, normalized fields; verification endpoint proves chain integrity.
- **NFR‑S3**: Passwords stored as salted hashes; session cookies use `Secure`, `HttpOnly`, `SameSite` in production.
- **NFR‑S4**: Idempotency via DB constraints where applicable (`job_key`, `(provider, external_event_id)`).

### Performance & availability (NFR‑P)

- **NFR‑P1**: `/readyz` and `/healthz` respond within 200 ms nominally.
- **NFR‑P2**: p95 page latency ≤ 500 ms for typical views under demo load.
- **NFR‑P3**: Metrics endpoint uses a dedicated registry and pre‑warms series to avoid empty dashboards.

### Reliability & ops (NFR‑R)

- **NFR‑R1**: App starts without Slurm; UI still works using CSV fallback when configured.
- **NFR‑R2**: Structured request logs include client, method, path, status, latency; 4xx/5xx are logged at warning level.
- **NFR‑R3**: Metrics can be disabled with env without breaking startup.

### Privacy (NFR‑PR)

- **NFR‑PR1**: Store minimal PII (username only) for core flows.
- **NFR‑PR2**: Payment PAN/CVV never touch the app (no card forms).

### i18n/UX (NFR‑U)

- **NFR‑U1**: English and Thai templates/labels where implemented; default to English when missing.

---

## 7. Integrations

### Slurm

- Primary: **slurmrestd** over HTTPS with auth.
- Fallbacks: `sacct` CLI; dev/demo CSV via `FALLBACK_CSV`.

### Observability

- Prometheus scrapes `/metrics`.
- `/healthz` and `/readyz` for probes/LB.

### Copilot / Ollama (optional)

- Embeddings and chat via **Ollama** HTTP API.
- Indexes Markdown under `COPILOT_DOCS_DIR`; vectors persisted under `COPILOT_INDEX_DIR`.

---

## 8. Configuration (env)

Core (app.py): `APP_ENV`, `FLASK_SECRET_KEY`, `FALLBACK_CSV`, `AUTH_THROTTLE_MAX_FAILS`, `AUTH_THROTTLE_WINDOW_SEC`, `AUTH_THROTTLE_LOCK_SEC`, `ADMIN_PASSWORD`, `AUTO_CREATE_SCHEMA`, `SEED_DEMO_USERS`, `DEMO_USERS`, `LOG_TO_STDOUT`.

Metrics: `METRICS_ENABLED`.

Audit: `AUDIT_HMAC_SECRET`, `AUDIT_HMAC_KEYRING`, `AUDIT_HMAC_KEY_ID`, `AUDIT_ANONYMIZE_IP`, `AUDIT_STORE_RAW_UA`.

Tax (billing): `BILLING_TAX_ENABLED`, `BILLING_TAX_LABEL`, `BILLING_TAX_RATE`, `BILLING_TAX_INCLUSIVE`.

Database: `DATABASE_URL` (and engine‑specific extras in `models.base`).

Copilot (optional): `COPILOT_ENABLED`, `COPILOT_DOCS_DIR`, `COPILOT_INDEX_DIR`, `OLLAMA_BASE_URL`, embedding/LLM model names & limits.

> Keep dev‑only tools (Adminer, Swagger, MkDocs, Ollama) non‑exposed in production or protect behind auth/TLS.

---

## 9. Constraints & assumptions

- Stateless web app; persistence in DB.
- No email/notification subsystem.
- Payments module is present in codebase but **disabled** in the current app wiring.
- Voiding receipts is restricted and audited; full ERP is out of scope.

---

## 10. Acceptance criteria (system tests)

1. **Login throttle**: N bad passwords → lock; success after window; lock/unlock audited; metrics counters increment.
2. **Usage view**: `/me?view=detail` shows jobs; `/me.csv` rows match the table after the same `before` cut‑off.
3. **Receipt**: creating admin self‑receipt includes only **unbilled** jobs up to cut‑off; creating duplicate fails on `job_key` unique.
4. **Rates API**: `GET /formula` returns JSON + `ETag`; `If‑None‑Match` → 304; `POST /formula` updates a tier (admin only) and audits the change.
5. **Tier overrides**: setting/clearing overrides changes effective pricing; changes are audited; summary audit includes counts.
6. **Ledger**: `/admin/ledger.csv` returns derived journal CSV for the window; `/admin/export/ledger.csv` returns posted GL; `/admin/ledger` renders totals without error.
7. **Period control**: closing a period succeeds only when accruals and consistency checks pass; reopen returns UI to editable state.
8. **Exports**: generating formal GL ZIP returns a manifest and HMAC signature; Xero CSV endpoints return non‑empty files when GL has data.
9. **PDFs**: receipt PDFs (EN/TH) render valid bytes; ledger PDF (TH) renders when there are rows.
10. **Readiness**: when DB is unreachable, `/readyz` is 500; when DB recovers, `/readyz` returns 200 again.
11. **Metrics**: `/metrics` responds with text format and includes pre‑warmed series (0 counts present).

---

## 11. Nice‑to‑have (not required for current release)

- Admin “view as user” (impersonation) with audit.
- Public read‑only status/pricing pages.
- Optional online payments (checkout + webhook) restored and gated behind env.
