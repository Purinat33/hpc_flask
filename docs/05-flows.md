# Flows

> End-to-end flows for users, admins, payments, ingestion, and ops.

---

## Conventions

- **Actors:** User (member), Admin, App (Flask), DB (PostgreSQL), Slurm (slurmrestd/`sacct`), Provider (payment gateway), Prometheus.
- **CSRF:** All session POSTs include `csrf_token` (form field) or `X-CSRFToken` header.
- **No “void” path** covered here (intentionally excluded for now).

---

## 1) Sign-in with throttling

```mermaid
sequenceDiagram
  participant U as User
  participant App as App (/auth)
  participant TH as AuthThrottle
  participant AUD as AuditLog

  U->>App: GET /login
  App-->>U: 200 HTML (form + CSRF)

  U->>App: POST /login (username, password, csrf_token)
  App->>TH: check_locked(username, ip)
  TH-->>App: locked? (yes/no)

  alt locked
    App-->>U: 200 HTML (generic error)
  else not locked
    App->>App: verify_password()
    alt success
      TH->>TH: reset fail_count
      AUD->>AUD: log(login_success)
      App-->>U: 302 -> /
    else fail
      TH->>TH: increment fail_count / maybe lock
      AUD->>AUD: log(login_fail)
      App-->>U: 200 HTML (generic error)
    end
  end
```

**Notes**

- Neutral error messages avoid leaking whether a username exists.
- Lockout windows and counters are per `(username, ip)`.

---

## 2) Usage ingestion (Slurm → App)

```mermaid
sequenceDiagram
  participant U as User
  participant App as App (/user)
  participant DS as Data Sources
  participant Slurm as slurmrestd or sacct
  participant DB as DB

  U->>App: GET /me?before=YYYY-MM-DD&view=detail|aggregate|billed
  App->>DS: fetch_jobs(start,end,user?)
  alt slurmrestd available
    DS->>Slurm: GET /slurmrestd (query window)
    Slurm-->>DS: jobs (incl. steps): User, JobID, Elapsed, TotalCPU, CPUTimeRAW, AllocTRES, ReqTRES, AveRSS, End, State
  else fallback to sacct
    DS->>Slurm: exec sacct --format=... ("keeps steps")
    Slurm-->>DS: parsed rows
  else last resort CSV
    DS->>DS: read FALLBACK_CSV
  end
  DS-->>App: normalized rows (parents + steps)
  App->>App: compute_costs(): step-aware CPU/MEM, GPU alloc, price per parent job
  App-->>U: 200 HTML (tables) or CSV if /me.csv
```

**Notes**

- Rows are normalized to a common schema regardless of source.
- Pricing is deterministic and re-computed on demand for display.

**Costing precedence (step-aware):**

- **CPU**: Σ `TotalCPU` (steps) → `CPUTimeRAW/3600` → `AllocCPUS × Elapsed`
- **MEM**: Σ `AveRSS(GB) × Elapsed` (steps) → `mem_from_TRES × Elapsed`
- **GPU**: `AllocGPU × Elapsed` (fallback `ReqGPU × Elapsed`)

---

## 3) Create a receipt (unbilled jobs)

```mermaid
sequenceDiagram
  participant U as User
  participant App as App (/user)
  participant B as BillingStore
  participant DB as DB

  U->>App: POST /me/receipt (before, csrf_token)
  App->>App: re-fetch jobs up to 'before'
  App->>App: compute_costs(rows) → cpu_core_hours, gpu_hours, mem_gb_hours, cost
  App->>B: create_receipt(username, start,end, items)
  B->>DB: INSERT receipt (status=pending)
  B->>DB: INSERT receipt_items (UNIQUE job_key prevents duplicates)
  DB-->>B: ok (or constraint error if any job was already billed)
  B-->>App: receipt_id, total
  App-->>U: 302 -> /me/receipts/{rid}
```

**Notes**

- **De-duplication:** a canonical `job_key` is **globally unique**; attempts to re-bill fail atomically.
- A receipt is immutable once created (except payment status).

---

## 4) Payments

### 4.1 Manual reconciliation (admin marks paid)

```mermaid
sequenceDiagram
  participant A as Admin
  participant App as App (/admin)
  participant B as BillingStore
  participant AUD as AuditLog

  A->>App: POST /admin/receipts/{rid}/paid (csrf)
  App->>B: mark_receipt_paid(rid, method="manual", tx_ref=...)
  B-->>App: ok
  App->>AUD: log(payment_mark_paid, rid)
  App-->>A: 302 -> /admin?section=billing
```

### 4.2 Hosted checkout + webhook (online)

```mermaid
sequenceDiagram
  participant U as User
  participant App as App (/payments)
  participant Prov as Provider
  participant P as PaymentsStore
  participant AUD as AuditLog

  U->>App: GET /payments/receipt/{rid}/start
  App->>P: create_payment(rid, amount,currency,status=pending)
  App->>Prov: create_checkout(return_url, webhook_url)
  Prov-->>U: Redirect to hosted checkout

  Prov-->>App: POST /payments/webhook (signed JSON)
  App->>P: record_event(provider, external_event_id, signature_ok, payload)
  App->>App: verify signature AND amount/currency match
  alt ok + pending
    App->>P: set payment=succeeded, set receipt=paid
    App->>AUD: log(payment_finalized, rid)
  else reject
    App->>P: keep as pending/failed, log reason
  end
  App-->>Prov: 200 (idempotent)

  U->>App: GET /payments/thanks?rid=...
  App-->>U: HTML (current payment/receipt status)
```

**Notes**

- **Idempotency:** `(provider, external_event_id)` unique prevents double-apply.
- **Security:** signature + amount/currency must match the local payment row.

---

## 5) Rates management (admin + API)

```mermaid
sequenceDiagram
  participant A as Admin
  participant App as App
  participant R as RatesStore

  A->>App: GET /admin?section=rates
  App-->>A: HTML (current tiers & form)

  A->>App: POST /admin (type,cpu,gpu,mem,csrf)
  App->>R: update_tier(type, cpu,gpu,mem)
  R-->>App: ok
  App-->>A: 302 -> /admin?section=rates
```

**Machine API**

- `GET /formula` returns JSON and an **ETag**.
- `POST /formula` (admin) updates a tier.
  Clients can cache with `If-None-Match`.

```mermaid
sequenceDiagram
  participant C as Client
  participant App as API (/formula)

  C->>App: GET /formula (If-None-Match: "v1")
  alt unchanged
    App-->>C: 304 Not Modified
  else changed
    App-->>C: 200 JSON (ETag: "v2")
  end
```

---

## 6) CSV exports

```mermaid
sequenceDiagram
  participant U as User/Admin
  participant App as App
  U->>App: GET /me.csv (or /admin/paid.csv, /admin/my.csv, /admin/audit.csv)
  App-->>U: 200 text/csv (Content-Disposition: attachment)
```

**Notes**

- Filters via query params (e.g., `start/end` for `/me.csv`).
- CSVs are generated on demand from the DB.

---

## 7) Internationalization

```mermaid
sequenceDiagram
  participant Any as Any user
  participant App as App
  Any->>App: POST /i18n/set (lang=en|th, csrf)
  App-->>Any: 302 back (sets language cookie)
```

---

## 8) Observability & ops

```mermaid
sequenceDiagram
  participant LB as Probe
  participant Mon as Prometheus
  participant App as App
  participant DB as DB

  LB->>App: GET /healthz
  App-->>LB: 200

  LB->>App: GET /readyz
  App->>DB: ping
  DB-->>App: ok/fail
  App-->>LB: 200 if ok else 500

  Mon->>App: GET /metrics
  App-->>Mon: 200 text/plain (Prometheus metrics)
```

**Notes**

- `/healthz` = process up; `/readyz` = DB connectivity OK.
- Metrics include request counts/latency and domain counters (auth/billing/payments).

---

## 9) Auditing (hash chain)

```mermaid
flowchart LR
  A1[login_success] --> A2[rates_update] --> A3[receipt_create] --> A4[payment_finalized]
  subgraph Audit table
    A1 --- A2 --- A3 --- A4
  end
```

- Each record stores `prev_hash` and `hash = H(prev_hash || record)`.
- Export via `/admin/audit.csv` for reviews.

---

## 10) Failure modes & guarantees

- **Double billing:** blocked by `UNIQUE(job_key)`; the whole receipt creation fails atomically if any duplicate appears.
- **Webhook replay:** ignored by unique `(provider, external_event_id)`; events are stored and re-applying is safe.
- **CSRF missing/invalid:** POST is rejected with 400/403; UI pages always embed a token.
- **Auth lockout:** repeated login failures trigger a temporary lock per `(username, ip)`.
- **Slurm unavailable:** automatic fallback order: `slurmrestd → sacct → CSV` (for demos). If all fail, the page shows a friendly message; CSV exports can still work if DB has prior data.
- **DB unhealthy:** `/readyz` turns 500; load balancer can pull the instance from rotation.

---

## 11) Demo mode (dev)

- **Users:** seeded demo users in development for quick login.
- **Data:** `FALLBACK_CSV` allows “offline” demos when Slurm is not reachable.
- **Payments:** `/payments/simulate` triggers a signed, local webhook event to exercise the success path without a real gateway.

---

## 12) Flow checklist (QA)

- [ ] Login form includes CSRF and shows neutral errors.
- [ ] Usage page renders with detail/aggregate/billed views.
- [ ] Receipt creation filters already-billed jobs and is transactional.
- [ ] Admin can update rates; GET `/formula` reflects changes and ETag updates.
- [ ] Hosted checkout redirects; webhook finalizes only on signature + amount/currency match.
- [ ] CSV exports download with expected columns.
- [ ] `/healthz`, `/readyz`, `/metrics` behave as documented.
- [ ] Audit CSV contains key actions in order with consistent hash chain.
