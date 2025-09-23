# Flows

> End-to-end flows for users, admins, payments, ingestion, Copilot, and ops.

---

## Conventions

* **Actors:** User (member), Admin, App (Flask), DB (PostgreSQL), Slurm (`slurmrestd`/`sacct`), Provider (payment gateway), Prometheus, **Ollama** (embeddings/chat), **Docs** (Markdown on disk).
* **CSRF:** All session POSTs include `csrf_token` (form field) or `X-CSRFToken` header (AJAX/JSON).
* **No “void” path** covered here (intentionally excluded for now).

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

* Neutral error messages avoid leaking whether a username exists.
* Lockout windows and counters are per `(username, ip)`.

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
    DS->>Slurm: GET slurmrestd (query window)
    Slurm-->>DS: jobs (incl. steps): User, JobID, Elapsed, TotalCPU, CPUTimeRAW, AllocTRES, ReqTRES, AveRSS, End, State
  else fallback to sacct
    DS->>Slurm: exec sacct --format=... (keeps steps)
    Slurm-->>DS: parsed rows
  else last resort CSV
    DS->>DS: read FALLBACK_CSV
  end
  DS-->>App: normalized rows (parents + steps)
  App->>App: compute_costs(): step-aware CPU/MEM, GPU alloc, classify tier, price parent jobs
  App-->>U: 200 HTML (tables) or CSV if /me.csv
```

**Notes**

* Rows are normalized to a common schema regardless of source.
* Pricing is deterministic and re-computed on demand for display.

**Costing precedence (step-aware):**

* **CPU**: Σ `TotalCPU` (steps) → `CPUTimeRAW/3600` → `AllocCPUS × Elapsed`
* **MEM**: Σ `AveRSS(GB) × Elapsed` (steps) → `mem_from_TRES × Elapsed`
* **GPU**: `AllocGPU × Elapsed` (fallback `ReqGPU × Elapsed`)
* **Tier**: derived by classifier, with **per-user override** if present (see §5).

---

## 3) Create a receipt (unbilled jobs)

```mermaid
sequenceDiagram
  participant U as User
  participant App as App (/user)
  participant B as BillingStore
  participant R as RatesStore
  participant TI as TierOverrides
  participant DB as DB

  U->>App: POST /me/receipt (before, csrf_token)
  App->>App: re-fetch jobs up to 'before', compute_costs(rows)
  App->>TI: effective_tier = override(username) or classifier(username)
  App->>R: load current rates for effective_tier
  App->>B: create_receipt(username, start,end, items, snapshot={tier, rate_cpu,gpu,mem, rates_locked_at})
  B->>DB: INSERT receipt (status=pending, snapshot fields set)
  B->>DB: INSERT receipt_items (UNIQUE job_key prevents duplicates)
  DB-->>B: ok (or constraint error if any job was already billed)
  B-->>App: receipt_id, total
  App-->>U: 302 -> /me/receipts/{rid}
```

**Notes**

* **De-duplication:** a canonical `job_key` is **globally unique**; attempts to re-bill fail atomically.
* **Rates snapshot:** receipt stores `pricing_tier`, `rate_cpu/gpu/mem`, `rates_locked_at` to preserve historical totals.

---

## 4) Copilot (Docs assistant)

### 4.1 Ask

```mermaid
sequenceDiagram
  participant U as User (widget/UI)
  participant App as App (/copilot/ask)
  participant IDX as Copilot Index (vectors + meta)
  participant Ol as Ollama
  participant Docs as /docs/*.md

  U->>App: POST /copilot/ask {q} (cookie + X-CSRFToken)
  App->>App: rate_limit(ip)  # per-minute leaky bucket
  App->>IDX: ensure_index_loaded()  # build if missing/stale
  App->>Ol: embed(q)
  IDX-->>App: top-K matches (cosine >= MIN_SIM?)
  alt low similarity or no hits
    App-->>U: {"answer_html":"I don't know.","sources":[]}
  else sufficient context
    App->>Ol: chat([system, user(ctx+q)])
    Ol-->>App: short answer text
    App-->>U: {"answer_html": "...", "sources":[...], "from":"copilot"}
  end
```

### 4.2 Reindex (admin/ops)

```mermaid
sequenceDiagram
  participant A as Admin
  participant App as App (/copilot/reindex)
  participant Docs as /docs/*.md
  participant Ol as Ollama
  participant IDX as Copilot Index

  A->>App: POST /copilot/reindex (csrf)
  App->>Docs: read *.md, strip code/mermaid/comments, chunk by H2/H3
  App->>Ol: embed(each chunk)
  Ol-->>App: vectors (normalized)
  App->>IDX: save vectors.npy + meta.json + signature.txt
  App-->>A: {"ok":true}
```

**Notes**

* If `COPILOT_ENABLED=false`, `/copilot/ask` returns **503** with `"Copilot disabled."`.
* Sources include file and anchor; answers are intentionally brief.

---

## 5) User Tier Overrides (admin)

```mermaid
sequenceDiagram
  participant A as Admin
  participant App as App (/admin?section=tiers)
  participant TI as TierOverrides (store)
  participant DS as Data Sources
  participant AUD as AuditLog

  A->>App: GET /admin?section=tiers
  App->>TI: load_overrides()
  App->>DS: fetch user names seen (DB/Slurm lookback)
  App-->>A: HTML table (username, radio mu/gov/private, Overridden? YES/NO)

  A->>App: POST /admin/tiers (csrf, fields tier_<username>=...)
  loop for each submitted user
    App->>App: natural = classify_user_type(username)
    alt desired == natural
      App->>TI: clear_override(username)  # removes override
      AUD->>AUD: log(tier.override.clear)
    else different
      App->>TI: upsert_override(username, desired)
      AUD->>AUD: log(tier.override.set)
    end
  end
  App-->>A: 302 -> /admin?section=tiers
```

**Effect**

* Future pricing & receipts for that user use **effective tier = override or natural** (whichever applies). Snapshot is written on receipt creation (§3).

---

## 6) Payments

### 6.1 Manual reconciliation (admin marks paid)

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

### 6.2 Hosted checkout + webhook (online)

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
  alt ok + payment still pending
    App->>P: set payment=succeeded, set receipt=paid
    App->>AUD: log(payment_finalized, rid)
  else reject or mismatch
    App->>P: set payment=failed (or leave pending), record reason
  end
  App-->>Prov: 200 (idempotent)

  U->>App: GET /payments/thanks?rid=...
  App-->>U: HTML (current payment/receipt status)
```

### 6.3 Cancel/Fail path (provider-driven)

```mermaid
stateDiagram-v2
  [*] --> pending
  pending --> succeeded: webhook(signature_ok && amount/currency match)
  pending --> failed: webhook(failed)/timeout
  pending --> canceled: provider_cancel
  succeeded --> [*]
  failed --> [*]
  canceled --> [*]
```

**Notes**

* **Idempotency:** `(provider, external_event_id)` unique prevents double-apply.
* **Security:** signature + amount/currency must match the local payment row.

---

## 7) Rates management (admin + API)

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

* `GET /formula` returns JSON and an **ETag**.
* `POST /formula` (admin) updates a tier. Clients can cache with `If-None-Match`.

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

## 8) CSV exports

```mermaid
sequenceDiagram
  participant U as User/Admin
  participant App as App
  U->>App: GET /me.csv (or /admin/paid.csv, /admin/my.csv, /admin/audit.csv)
  App-->>U: 200 text/csv (Content-Disposition: attachment)
```

**Notes**

* Filters via query params (e.g., `start/end` for `/me.csv`).
* CSVs are generated on demand from live data.

---

## 9) Internationalization

```mermaid
sequenceDiagram
  participant Any as Any user
  participant App as App
  Any->>App: POST /i18n/set (lang=en|th, csrf)
  App-->>Any: 302 back (sets language cookie)
```

---

## 10) Observability & ops

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

* `/healthz` = process up; `/readyz` = DB connectivity OK.
* Metrics include request counts/latency and domain counters (auth/billing/payments).

---

## 11) Auditing (hash chain)

```mermaid
flowchart LR
  A1[login_success] --> A2[rates.update] --> A3[receipt.create] --> A4[payment.finalized] --> A5[tier.override.set/clear]
  subgraph Audit table
    A1 --- A2 --- A3 --- A4 --- A5
  end
```

* Each record stores `prev_hash` and `hash = H(prev_hash || record)`.
* Export via `/admin/audit.csv` for reviews.

---

## 12) Failure modes & guarantees

* **Double billing:** blocked by `UNIQUE(job_key)`; the whole receipt creation fails atomically if any duplicate appears.
* **Webhook replay:** ignored by unique `(provider, external_event_id)`; events are stored and re-applying is safe.
* **CSRF missing/invalid:** POST is rejected with 400/403; UI pages always embed a token.
* **Auth lockout:** repeated login failures trigger a temporary lock per `(username, ip)`.
* **Slurm unavailable:** automatic fallback order: `slurmrestd → sacct → CSV` (for demos). If all fail, the page shows a friendly message; CSV exports can still work if prior data exists.
* **DB unhealthy:** `/readyz` returns 500; load balancer can pull the instance from rotation.
* **Copilot rate limit:** exceeds per-IP bucket → `"Rate limit exceeded..."` response; similarity below threshold → `"I don't know."`.
* **Tier overrides:** choosing the same tier as the natural classifier **removes** the override; effective tier is resolved at pricing time and snapshotted on the receipt.

---

## 13) Demo mode (dev)

* **Users:** seeded demo users in development for quick login.
* **Data:** `FALLBACK_CSV` allows “offline” demos when Slurm is not reachable.
* **Payments:** `/payments/simulate` triggers a signed, local webhook event to exercise the success path without a real gateway.
* **Copilot:** enable Ollama locally; docs are read from the repo’s `/docs` folder.

---

## 14) Flow checklist (QA)

* [ ] Login form includes CSRF and shows neutral errors; lockout works.
* [ ] Usage page renders with detail/aggregate/billed views; costing is step-aware.
* [ ] Receipt creation filters already-billed jobs; **pricing snapshot** is written.
* [ ] **Tier overrides** page lists users, saves/clears overrides, and logs audit.
* [ ] Admin can update rates; `GET /formula` reflects changes and ETag updates.
* [ ] Hosted checkout redirects; webhook finalizes only on signature + amount/currency match; canceled/failed states display correctly.
* [ ] Copilot answers with sources; rate limiting and “I don’t know.” behavior verified; reindex works.
* [ ] CSV exports download with expected columns.
* [ ] `/healthz`, `/readyz`, `/metrics` behave as documented.
* [ ] Audit CSV contains key actions in order with a consistent hash chain.
