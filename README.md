# NVIDIA Bright Computing HPC Accounting Application

> A simple web app for pulling Slurm usage, pricing it, and issuing receipts with payment integration.

## Quick Start:

```bash
docker compose up -d --build
```

Then access:

- Application at http://localhost:8000
- Adminer (Database tool) at http://localhost:8080
- Documentation at http://localhost:9999
- API Doc at http://localhost:8081

### Running Tests

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm app_test
```

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
