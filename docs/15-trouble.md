# Troubleshooting & FAQ

> Fast fixes for common problems across Docker, DB, Slurm, payments, Swagger, CSRF, and metrics. Use this as your first stop before filing an issue.

---

## 0) First 60 seconds (triage)

```bash
# What’s running?
docker compose ps

# Tail the app & DB logs
docker compose logs -f hpc
docker compose logs -f pg

# Health checks
curl -i http://localhost:8000/healthz
curl -i http://localhost:8000/readyz
```

**If `/readyz` is 500** → DB or `DATABASE_URL` problem.
**If `/healthz` is 200 but UI errors** → app logic/config.
**If container is “unhealthy”** → see the healthcheck command in `docker-compose.yml`.

---

## 1) Symptom → likely cause → fix

| Symptom                                    | Likely cause                             | Quick fix                                                                                                                                                                          |
| ------------------------------------------ | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **App page doesn’t load; 502 from proxy**  | `hpc` unhealthy / not listening          | `docker compose logs -f hpc`; ensure `gunicorn` bound to `0.0.0.0:8000` (it is by default).                                                                                        |
| **`/readyz` returns 500**                  | DB unreachable                           | In Compose **inside containers** use `pg:5432`; from host use `127.0.0.1:5433`. Restart DB then app: `docker compose restart pg hpc`.                                              |
| **Can’t connect with psql from host**      | Using container address from host        | Use `psql postgresql://hpc_user:muict@127.0.0.1:5433/hpc_app`.                                                                                                                     |
| **Swagger shows Petstore**                 | UI can’t find your spec                  | Visit `http://localhost:8081/?url=/spec/openapi.yaml`. Ensure Compose has `SWAGGER_JSON: /spec/openapi.yaml` and volume `./docs/api:/spec:ro`.                                     |
| **CSV export empty**                       | Date window has no jobs; Slurm down      | Try a broader window; verify Slurm path; for demo ensure `./instance:/app/instance:ro` and `FALLBACK_CSV=/app/instance/test.csv`.                                                  |
| **Receipt creation fails “duplicate job”** | Already-billed job(s) selected           | The UNIQUE `job_key` blocked double billing. Choose an earlier `before` date or exclude those jobs.                                                                                |
| **Webhook doesn’t finalize payment**       | Wrong secret; wrong URL; amount mismatch | Confirm `PAYMENT_WEBHOOK_SECRET`, endpoint reachable, and local `payments.amount_cents/currency` matches the event. Re-deliver from provider dashboard. Dev: `/payments/simulate`. |
| **Login always fails / locks**             | Throttle active; wrong creds             | Wait until `locked_until`; or admin clears throttle entry (policy-gated). Check `/admin/audit` for lock events.                                                                    |
| **CSRF errors on POST**                    | Missing token or header                  | For forms: include hidden `csrf_token`. For JSON: header `X-CSRFToken: <token>` (read it from the page first). Webhook is **CSRF-exempt**.                                         |
| **Metrics 404**                            | Disabled by env                          | Set `METRICS_ENABLED=1`; then `curl http://localhost:8000/metrics`.                                                                                                                |
| **Port already in use**                    | Host port conflict                       | Change host mapping (e.g., `8001:8000`) or stop the process on the host.                                                                                                           |
| **Windows path confusion**                 | Host vs container paths                  | Host: `C:\...\instance\test.csv`. In container: `/app/instance/test.csv`. Bind mount maps between them.                                                                            |

---

## 2) Database URL cheat sheet

* **Inside containers (Compose env):**
  `postgresql+psycopg2://hpc_user:muict@pg:5432/hpc_app`
* **On the host (tools, local runs):**
  `postgresql+psycopg2://hpc_user:muict@127.0.0.1:5433/hpc_app`

> `pg` is the **service name** (Docker DNS). `127.0.0.1:5433` is the **published** port on your host.

---

## 3) Payments debugging (step-by-step)

1. **Start flow:** `/payments/receipt/<rid>/start` creates a local `payments` row (`status=pending`).
2. **Webhook arrives:** `POST /payments/webhook` (signed). We store a `payment_event`.
3. **Verify:** signature **and** amount/currency must match the local payment.
4. **Finalize:** in one transaction set `payments.status=succeeded` and `receipts.status=paid`.

**Checks**

```sql
-- Payment row:
SELECT * FROM payments WHERE receipt_id = <rid>;

-- Events (idempotency):
SELECT provider, external_event_id, event_type, signature_ok, created_at
FROM payment_events WHERE payment_id = <payment_id>;
```

If the same external event appears twice, that’s OK—unique `(provider, external_event_id)` makes the second a no-op.

---

## 4) Slurm ingestion issues

* Prefer **slurmrestd**; verify `SLURMRESTD_URL`.
* If using `sacct` fallback, ensure CLI is present in the container/host and time windows are bounded server-side.
* For demos, mount CSV **read-only** and set `FALLBACK_CSV`.

**Smoke check inside the app container**

```bash
docker compose exec hpc sh -lc 'python - <<PY
import os; print("FALLBACK_CSV=", os.environ.get("FALLBACK_CSV"))
print("SLURMRESTD_URL=", os.environ.get("SLURMRESTD_URL"))
PY'
```

---

## 5) When all else fails (clean restart)

```bash
docker compose down
docker compose up -d --build
# If you must reset the DB too (CAUTION: deletes data):
# docker compose down -v && docker compose up -d --build
```

---

## 6) Decision tree (quick visual)

```mermaid
flowchart TD
  A[Problem] --> B{App responds?}
  B -- No --> C[/docker compose logs -f hpc/]
  C --> D{Readyz 200?}
  D -- No --> E[Check DATABASE_URL & DB health] --> F[Restart pg,hpc]
  D -- Yes --> G[Check route logs & stacktrace]

  B -- Yes --> H{Which feature?}
  H -- CSV --> I[Dates/Slurm/CSV fallback]
  H -- Payment --> J[Secret/URL/amount & events table]
  H -- Swagger --> K[SWAGGER_JSON & /spec mount]
  H -- Metrics --> L[METRICS_ENABLED=1 & curl /metrics]
  H -- Auth --> M[Throttle/CSRF; audit log]
```

---

## 7) FAQ

**Q: Can we change past receipts after rates change?**
A: No. Receipts capture totals at creation. Update rates for *future* receipts.

**Q: Why do I see “duplicate job” errors?**
A: `receipt_items.job_key` is globally UNIQUE to prevent double billing. You’ve selected already-billed work.

**Q: Is the webhook protected by CSRF?**
A: No—CSRF applies to browsers. Webhooks are authenticated by signature and idempotent event IDs.

**Q: Where are raw payment events?**
A: In `payment_events.raw_payload` (store redacted). Use `/admin/audit.csv` for high-level actions.

**Q: Can I run the app without Slurm?**
A: Yes. Use `FALLBACK_CSV` and the UI will render from the CSV; pricing/rates still work.

---

## 8) Useful one-liners

```bash
# Check app can talk to DB from inside container
docker compose exec hpc sh -lc 'python - <<PY
import os, psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"].replace("postgresql+psycopg2","postgresql"))
print("DB OK"); conn.close()
PY'

# Pull ETag and test GET /formula caching
ET=$(curl -sI http://localhost:8000/formula | sed -n "s/^ETag: //p")
curl -i -H "If-None-Match: $ET" http://localhost:8000/formula

# Verify metrics endpoint
curl -s http://localhost:8000/metrics | head -n 15
```

---

## 9) When to open an issue

* You’ve followed the decision tree and still can’t resolve it.
* A webhook is **accepted** (200) but neither `payments` nor `receipts` change.
* `/readyz` flips between 200 and 500 under low load (possible DB flakiness).
* You found a reproducible bug in pricing math or CSV exports.

Please include: Compose file (redacted), logs (last 100 lines), exact steps/commands, and environment (`APP_ENV`, OS).

---
