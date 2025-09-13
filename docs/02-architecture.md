# Architecture

> How the system is structured: context, containers, components, key flows, data, and the main use cases. All diagrams are text-based (Mermaid) so they render in MkDocs Material.

---

## 1) System context (C4-L1)

```mermaid
graph LR
  U[End User] -- Browser (HTTPS) --> W[Flask Web App]
  A[Admin/Finance] -- Browser (HTTPS) --> W

  subgraph Slurm Cluster
    R[slurmrestd<br/>HTTPS/JWT]
    C[sacct CLI]
  end

  W <--> DB[(PostgreSQL<br/>SQLAlchemy)]
  W -- REST/JSON --> R
  W -- CLI --> C
  W -- /metrics (Prometheus) --> Prom[Prometheus]
  W -- Webhook (HTTPS) --> Pay[Payment Provider]
  Pay -- Webhook callbacks --> W

  A -. optional .-> Adm[Adminer DB UI]
```

**Intent**

- The web app ingests job usage from **Slurm** (prefer `slurmrestd`, fall back to `sacct`, last-resort CSV), computes costs, and produces **receipts**.
- Online payments (optional) are finalized **only via provider webhooks**.
- Observability via Prometheus `/metrics`; health endpoints for ops.
- Adminer is only for **dev troubleshooting**.

---

## 2) Containers (C4-L2)

```mermaid
graph TB
  subgraph User Space
    Browser[User/Admin Browser]
  end

  subgraph App Stack
    App[Flask App\nGunicorn/WSGI]
    DB[(PostgreSQL)]
    Adminer[Adminer]
    Prom[Prometheus]
  end

  subgraph External
    SlurmREST[slurmrestd]
    sacct[sacct CLI node]
    Pay[Payment Provider]
  end

  Browser -->|HTTPS| App
  App <--> DB
  App -->|HTTPS/JSON| SlurmREST
  App -->|POSIX exec| sacct
  App -->|HTTPS webhooks| Pay
  Prom -->|scrape /metrics| App
  Browser -.-> Adminer

```

**Security boundaries**

- Public edge terminates at the Flask reverse proxy/app.
- DB and Adminer live on a private network in **dev**; in **prod** Adminer is removed and DB is managed/hosted.
- `slurmrestd` must be TLS-protected and authenticated (e.g., JWT).
- Payment webhooks require **HMAC/signature** verification and strict amount/currency checks.

---

## 3) Components (C4-L3 inside Flask)

```mermaid
graph LR
  subgraph Controllers["Controllers (Blueprints)"]
    AUTH[auth]
    USER[user]
    ADMIN[admin]
    API[api]
    PAY[payments]
  end

  subgraph Services
    BILL[billing.py<br/>cost engine]
    DS[data_sources.py<br/>slurm_rest.py]
    MET[metrics.py]
    REG[registry.py<br/>payment adapter registry]
  end

  subgraph Models/Stores
    USERS[users_db.py]
    RATES[rates_store.py]
    BILLING[billing_store.py]
    PAYST[payments_store.py]
    AUD[audit_store.py]
    THROT[security_throttle.py]
    SCHEMA[schema.py<br/>SQLAlchemy base]
  end

  AUTH --> USERS
  AUTH --> THROT
  USER --> DS
  USER --> BILL
  USER --> BILLING
  ADMIN --> RATES
  ADMIN --> BILLING
  PAY --> PAYST
  PAY --> REG
  PAY --> AUD
  API --> RATES
  API --> BILL

  BILL --> RATES
  DS --> Slurm[(slurmrestd / sacct)]
  SCHEMA --> DB[(PostgreSQL)]
  AUD --> DB
  THROT --> DB
  RATES --> DB
  BILLING --> DB
  USERS --> DB
  PAYST --> DB
  MET --> App[/metrics export/]
```

**Responsibilities (mapping to files)**

- **Controllers** (route layer): authentication & sessions (`auth.py`), user billing UI (`user.py`), admin console (`admin.py`), public/admin API (`api.py`), payment flows & webhook (`payments.py`).
- **Services**: cost calculation (`billing.py`), Slurm ingestion (`data_sources.py`, `slurm_rest.py`), Prometheus metrics (`metrics.py`), dynamic payment provider binding (`registry.py`).
- **Models/Stores**: SQLAlchemy models and persistence helpers (`schema.py`, `*store.py`), audit hash-chain (`audit_store.py`), login throttling (`security_throttle.py`), users & roles (`users_db.py`), rates CRUD (`rates_store.py`), receipts & items (`billing_store.py`), payments & events (`payments_store.py`).
- **App wiring**: app factory, blueprints, CSRF, i18n, logging, health/ready (`app.py`).

---

## 4) Deployment view

```mermaid
flowchart LR
  subgraph Docker Compose
    app[app: Flask + Gunicorn]
    db[(postgres)]
    adminer[adminer]
    prom[prometheus]
    gra[grafana]
  end

  app <--> db
  adminer <--> db
  app --> prom
  prom --> gra
  gra --> app
```

- Dev: `docker compose up -d --build` brings up **app + postgres (+ adminer)**.
- Prod: use a managed Postgres, remove Adminer, front the app with a hardened reverse proxy, and configure secrets via environment (no `.env` in image).

---

## 5) Data model (bird’s-eye)

```mermaid
erDiagram
  USERS ||--o{ RECEIPTS : "has"
  USERS ||--o{ PAYMENTS : "initiates"
  RECEIPTS ||--o{ RECEIPT_ITEMS : "contains"
  PAYMENTS ||--o{ PAYMENT_EVENTS : "emits"
  RATES ||--o{ RECEIPT_ITEMS : "used to price"
  AUDIT_LOG }o--|| USERS : "actor (username)"
  AUTH_THROTTLE }o--|| USERS : "per-user entries"

  USERS {
    string username PK
    string password_hash
    string role  "admin|user"
    datetime created_at
  }

  RATES {
    string tier PK "mu|gov|private"
    numeric cpu_rate
    numeric gpu_rate
    numeric mem_rate
    datetime updated_at
  }

  RECEIPTS {
    int id PK
    string username FK
    date start_date
    date end_date
    numeric total_amount
    string status "draft|paid|void"
    datetime paid_at
    string method
    string tx_ref
  }

  RECEIPT_ITEMS {
    int receipt_id FK
    string job_key  "canonical unique"
    string job_id_display
    numeric cpu_core_hours
    numeric gpu_hours
    numeric mem_gb_hours
    numeric cost
  }

  PAYMENTS {
    int id PK
    int receipt_id FK
    string username FK
    string provider
    string status "pending|succeeded|failed"
    string currency
    int amount_cents
    string external_payment_id
    datetime created_at
  }

  PAYMENT_EVENTS {
    int id PK
    int payment_id FK
    string provider
    string external_event_id "unique per provider"
    string event_type
    boolean signature_ok
    text raw_payload
    datetime received_at
  }

  AUTH_THROTTLE {
    string username
    string ip
    datetime window_start
    int fail_count
    datetime locked_until
  }

  AUDIT_LOG {
    int id PK
    datetime at
    string actor
    string action
    text extra
    string prev_hash
    string hash
  }
```

**Notable constraints**

- `RECEIPT_ITEMS.job_key` is **unique** across all receipts → prevents double billing.
- `PAYMENT_EVENTS (provider, external_event_id)` is **unique** → idempotent webhook handling.
- Role and status fields use CHECK-like guards in store layer.

---

## 6) Key flows (sequence)

### 6.1 Login with throttling & audit

```mermaid
sequenceDiagram
  participant U as User
  participant Auth as /auth
  participant TH as Auth Throttle
  participant AUD as Audit

  U->>Auth: POST /auth/login (username, password)
  Auth->>TH: check_locked(username, ip)
  TH-->>Auth: locked? no/yes
  alt Locked
    Auth-->>U: 429/403 generic error (no leak)
  else Not locked
    Auth->>Auth: verify_password()
    alt Success
      TH->>TH: reset fail_count
      AUD->>AUD: log(action="login_success")
      Auth-->>U: 302 -> /
    else Fail
      TH->>TH: increment fail_count, maybe set locked_until
      AUD->>AUD: log(action="login_fail")
      Auth-->>U: 200 with generic message
    end
  end
```

### 6.2 Usage → Receipt

```mermaid
sequenceDiagram
  participant U as User
  participant UI as /user
  participant DS as data_sources
  participant BILL as billing
  participant BDB as billing_store

  U->>UI: Select date window, "Fetch"
  UI->>DS: fetch_jobs_with_fallbacks(start,end,user?)
  DS-->>UI: rows (User, JobID, Elapsed, TotalCPU, ReqTRES, End, State)
  UI->>BILL: compute_costs(rows)
  BILL-->>UI: add CPU_Core_Hours, GPU_Hours, Mem_GB_Hours, tier, Cost(฿)
  UI->>BDB: create_receipt_from_rows(rows+costs)
  BDB->>BDB: enforce UNIQUE(job_key)
  BDB-->>UI: Receipt(id,total)
  UI-->>U: Show receipt & items
```

### 6.3 Payment (optional) with webhook finalization

```mermaid
sequenceDiagram
  participant U as User
  participant PAY as payments
  participant Prov as Provider
  participant PDB as payments_store
  participant AUD as audit

  U->>PAY: Start payment for receipt X
  PAY->>PDB: create Payment(row)
  PAY->>Prov: create_checkout(amount,currency,return_url,webhook_url)
  Prov-->>U: Hosted checkout page

  Prov-->>PAY: POST /payments/webhook (signed)
  PAY->>PDB: record_event(provider,event_id,signature_ok,raw)
  PAY->>PDB: if signature_ok and amount/currency match -> mark payment succeeded, mark receipt paid
  AUD->>AUD: log("payment_finalized", receipt_id, payment_id)
  PAY-->>U: /payments/thanks shows status
```

### 6.4 Observability & ops

```mermaid
sequenceDiagram
  participant Mon as Prometheus
  participant App as Flask
  participant DB as Postgres

  Mon->>App: GET /metrics
  App-->>Mon: Prometheus text metrics

  Mon->>App: GET /healthz
  App-->>Mon: 200 if process alive

  Mon->>App: GET /readyz
  App->>DB: ping
  DB-->>App: ok/fail
  App-->>Mon: 200 if DB ok else 500
```

---

## 7) Costing logic (summary)

- Parse Slurm fields into **resource hours**:

  - CPU core-hours from `TotalCPU`/`AllocCPUs` + elapsed.
  - GPU hours from `ReqTRES` parse (`gres/gpu` or `gpu:` patterns).
  - Memory GB-hours from `ReqTRES` memory spec.

- Select **tier** (e.g., `mu | gov | private`) per user rule.
- Apply **tiered rates** → per-job `Cost (฿)`; sum per receipt.

---

## 8) Use cases

| ID    | Actor    | Goal                 | Preconditions       | Main success                                           | Notes                                        |
| ----- | -------- | -------------------- | ------------------- | ------------------------------------------------------ | -------------------------------------------- |
| UC-01 | User     | View my usage        | Logged in           | Sees jobs in date window; can export CSV               | Skips already-billed jobs                    |
| UC-02 | User     | Create a receipt     | UC-01               | Receipt with total is created; items frozen            | Prevents duplicates via `job_key`            |
| UC-03 | User     | Pay a receipt        | UC-02               | Redirected to provider; after webhook → receipt = paid | Signature + amount/currency check            |
| UC-04 | Admin    | Update rates         | Admin role          | New rates persist; new pricing uses them               | Version by timestamp; no retroactive changes |
| UC-05 | Admin    | Inspect billing      | Admin role          | Filter by user/date/status                             | Export audit CSV                             |
| UC-06 | Ops      | Health/ready checks  | App deployed        | `/healthz` 200; `/readyz` DB reachable                 | For Kubernetes/LB probes                     |
| UC-07 | Ops      | Metrics scrape       | Metrics enabled     | Prometheus scrapes `/metrics`                          | Request & auth/billing counters              |
| UC-08 | Security | Throttle brute force | Repeated bad logins | Account/IP temporarily locked                          | Neutral error messages                       |

---

## 9) Cross-cutting concerns

- **Authentication & sessions**: Flask-Login session cookies; CSRF everywhere except webhooks.
- **RBAC**: `user` and `admin` guards at route level.
- **Throttling**: per-user+IP counters with lockout time windows.
- **Auditing**: append-only, hash-chained log for sensitive actions (login events, rate changes, payment finalization).
- **Idempotency**: webhook events unique per provider; applying events is safe to repeat.
- **Configuration**: environment-driven (`DATABASE_URL`, secrets, `SLURMRESTD_URL`, `PAYMENT_PROVIDER`, etc.).
- **Error handling**: graceful fallbacks on Slurm ingestion (`slurmrestd` → `sacct` → CSV).

---

## 10) Scaling & performance (brief)

- **Read path** (usage fetch + pricing) is CPU-light; cache Slurm calls by window if needed.
- **Write path** (receipt creation/payment finalization) is short, DB-bounded; wrap in transactions.
- Use **indexes** on `receipt_items.job_key`, `payment_events (provider, external_event_id)`, and common filters (username, dates, status).
- Horizontal scale the app (stateless) behind a reverse proxy; keep DB as single source of truth, with regular backups.

---

## 11) Threat model (snapshot)

- **Spoofed webhooks** → verify signatures, enforce amount/currency and receipt binding, log all events.
- **Replay attacks** → unique `(provider, external_event_id)`; ignore duplicates.
- **Credential stuffing** → throttle/lockout + audit; neutral login messages.
- **CSRF** → global CSRF; explicitly exempt only webhook route.
- **Prying on dev DB** → never expose Adminer in production; isolate networks; restrict DB users.
- **Double billing** → `job_key` uniqueness at DB layer + UI filtering.

---

## 12) What to configure (quick checklist)

- `DATABASE_URL`, `FLASK_SECRET_KEY`, `ADMIN_PASSWORD`
- `SLURMRESTD_URL` (+ auth tokens/certs), or path to `sacct`
- `PAYMENT_PROVIDER`, `PAYMENT_CURRENCY`, `SITE_BASE_URL`, provider secrets & webhook signing key
- `METRICS_ENABLED`, log level, probe paths

---
