# Operations (Ops)

> Day-to-day operations for the **HPC Billing Platform**: what to watch, how to back up & restore, housekeeping jobs, incident runbooks, and checklists. Designed for teams running via Docker Compose or a reverse-proxy’d single host; adapt paths for Kubernetes as needed.

---

## 1) Daily / Weekly / Monthly checklist

### Daily

- [ ] **Health**: `GET /healthz` (200), `GET /readyz` (200).
- [ ] **Dashboards**: skim Prometheus/Grafana panels (req rate, 4xx/5xx, latency p95).
- [ ] **Payments**: check for **pending** payments > 1h (reconcile or investigate).
- [ ] **Auth locks**: scan throttle table for users locked > 30m and assist if needed.
- [ ] **Slurm ingestion**: spot-check “My usage” for a random user (yesterday’s jobs appear).

### Weekly

- [ ] **Backups**: verify latest **Postgres dump** exists and restore test works.
- [ ] **Audit**: export `/admin/audit.csv`, scan for anomalous actions.
- [ ] **Rates**: confirm rates match policy (no accidental dev values in prod).
- [ ] **Webhooks**: review failed webhook events, if any.

### Monthly

- [ ] **Secret rotation**: rotate `PAYMENT_WEBHOOK_SECRET` (with provider), DB password if policy requires.
- [ ] **Dependency scan**: run `pip-audit` / `safety`; rebuild image.
- [ ] **Capacity**: DB size, table growth, indices bloat; prune old dev data if policy allows.
- [ ] **DR drill**: run a timed restore to a scratch instance; record RTO/RPO.

---

## 2) Quick links & commands

### Endpoints

- Liveness: `GET https://<host>/healthz`
- Readiness: `GET https://<host>/readyz`
- Metrics: `GET https://<host>/metrics` (Prometheus)
- Admin console: `GET https://<host>/admin`
- Audit export: `GET https://<host>/admin/audit.csv`

### Compose service names

- App: `hpc` (container)
- Postgres: `pg` (or `db` in some environments)
- Swagger UI (dev): `swagger`

> Adjust commands below if your DB service is named `db` instead of `pg`.

---

## 3) Backups

> Use **logical dumps** (`pg_dump`) for portability. Store off-host and encrypt at rest.

### Ad-hoc backup (Compose)

```bash
# Choose the DB container name you use: pg or db
DBSVC=pg

# Create a compressed custom-format dump inside the container
docker compose exec $DBSVC sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F c -f /tmp/hpc_app.dump'

# Copy to the host with a date tag
mkdir -p backups
docker cp ${DBSVC}:/tmp/hpc_app.dump ./backups/hpc_app-$(date +%F).dump

# (Optional) Encrypt with age/gpg, then remove the plaintext file
```

### Scheduled backup (cron on host)

```
# m h  dom mon dow   command
15 2 * * * docker compose exec pg sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F c -f /tmp/hpc_app.dump' && \
           docker cp pg:/tmp/hpc_app.dump /var/backups/hpc/hpc_app-$(date +\%F).dump && \
           find /var/backups/hpc -type f -mtime +30 -delete
```

**Retention**: keep ≥30 days daily; ≥12 months monthly (policy-dependent).
**Test restores** at least monthly (see §4).

---

## 4) Restore (runbook)

> Restore **into a new DB** (or maintenance window) to avoid clobbering prod unexpectedly.

```bash
# Stop the app so it doesn’t write during restore (optional if restoring elsewhere)
docker compose stop hpc

# Put dump file into the container
DBSVC=pg
docker cp ./backups/hpc_app-YYYY-MM-DD.dump ${DBSVC}:/tmp/restore.dump

# Drop & recreate or restore over existing DB (CAUTION: data loss if dropped)
docker compose exec $DBSVC sh -lc 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c /tmp/restore.dump'

# Start the app
docker compose start hpc

# Verify:
#  - /readyz 200
#  - admin pages load
#  - user receipts and rates visible
```

**Smoke test after restore**

- Login with a known user (non-admin).
- Load `/me` for last 7 days; confirm jobs & totals.
- Open a recent paid receipt; amounts intact.

---

## 5) Payments operations

### Reconciliation (manual)

- Typical causes of **stuck pending**:

  - Provider sent webhook to wrong URL or blocked by firewall.
  - Signature mismatch (wrong secret).
  - Amount/currency mismatch (client or rate change mid-flow).

**Steps**

1. Check `/payments/thanks?rid=<id>` to see current state.
2. Search provider dashboard for `external_payment_id` or reference on receipt.
3. If provider shows **paid** but our app is pending:

   - Re-deliver the event from provider’s dashboard (preferred), **or**
   - Use the **dev simulate** tool only in non-prod to exercise the path.

4. If business policy allows, use **Admin → Mark paid** (manual reconciliation). Log the external reference in the form.

### Rotate webhook secret

- Generate new secret; set `PAYMENT_WEBHOOK_SECRET` in env/secret store.
- Update provider webhook settings to send with new secret.
- Roll app containers (zero downtime if behind proxy).
- Confirm new deliveries succeed.

---

## 6) Slurm ingestion checks

### slurmrestd path (preferred)

- Confirm env: `SLURMRESTD_URL` (and token/certs if used).
- From the **app container**:

  ```bash
  docker compose exec hpc sh -lc 'python - <<PY
  ```

import os,urllib.request; u=os.environ.get("SLURMRESTD_URL",""); print("SLURMRESTD_URL=",u);

# Optionally perform a minimal GET to a public endpoint if available.

PY'

````
- If 401/403/SSL error, fix token/certs or trust chain at the proxy.

### sacct fallback
- Ensure `sacct` is installed/available if you plan to use CLI fallback.
- Verify the command used in your environment (time window, format); the app **should not** pass user input directly.
- If CLI is not desired in prod, **disable** by not installing Slurm client tools in the app host/container.

### CSV fallback (dev/demo)
- Path must be mounted **read-only**; set `FALLBACK_CSV=/app/instance/test.csv`.
- Replace the file as needed to demo scenarios.

---

## 7) User & rate administration

### Create/assist users
- In **dev**, demo users can be seeded via env.
- In **prod**, create users through your admin UI/workflow (if present) or via SQL as a last resort.

Example (psql) to reset a password (use your hash function approach):
```sql
-- WARNING: Perform via application path when possible.
-- This is illustrative; do not store plaintext.
UPDATE users SET password_hash = '<new-hash>' WHERE username = 'alice';
````

### Update rates

- Use **Admin → Rates** UI or `POST /formula` (admin session).
- After change, verify:

  - `GET /formula` reflects new values (ETag changed).
  - New receipts use the updated rates (old receipts remain unchanged).

---

## 8) Audit & compliance

### Export audit

- `GET /admin/audit.csv` → archive weekly/monthly per policy.

### Verify hash chain (spot check)

```bash
docker compose exec hpc python - <<'PY'
# Pseudo-checker; adjust to your ORM/DB setup if needed
import hashlib, json, os, psycopg2
conn = psycopg2.connect(os.environ["DATABASE_URL"].replace("postgresql+psycopg2","postgresql"))
cur = conn.cursor()
cur.execute("SELECT id, ts, actor, action, target, status, extra, prev_hash, hash FROM audit_log ORDER BY id")
prev = "0000"
ok = True
for (id, ts, actor, action, target, status, extra, prev_hash, h) in cur.fetchall():
    if prev_hash != prev: ok=False; print("Chain break at", id, "(prev mismatch)")
    payload = json.dumps({"id": id, "ts": str(ts), "actor": actor, "action": action, "target": target, "status": status, "extra": extra}, sort_keys=True)
    prev = hashlib.sha256((prev + payload).encode()).hexdigest()
    if h != prev: ok=False; print("Hash mismatch at", id)
print("AUDIT OK" if ok else "AUDIT FAIL")
PY
```

> Treat this as an operational sanity check; your authoritative verifier can live in a separate tool or notebook.

---

## 9) Monitoring & alerting

### Metrics to watch

- **http_requests_total** by status and route (or your app’s equivalents).
- **request_latency_seconds** p50/p95 (histogram).
- **auth_throttle_locks_total** (if exported).
- **billing_receipts_created_total**.
- **payments_webhook_events_total** & **payments_finalized_total**.
- **db_ready** gauge (used by `/readyz`).

### Example alerts (Prometheus)

```
# App not ready
probe_readyz_down = (up{job="hpc"} == 0) or (hpc_ready == 0)
ALERT AppNotReady IF probe_readyz_down FOR 3m

# High 5xx rate
ALERT HighErrorRate IF
  sum(rate(http_requests_total{status=~"5.."}[5m])) /
  sum(rate(http_requests_total[5m])) > 0.02 FOR 10m

# No webhooks for long period (during known active hours)
ALERT NoWebhooks IF rate(payments_webhook_events_total[1h]) == 0
```

> Names depend on your `metrics.py`; align labels accordingly.

---

## 10) Log operations

- **Where**: app logs to stdout → `docker logs hpc`.
- **Ship centrally**: consider Loki/ELK (Docker logging driver, promtail, or filebeat).
- **PII**: avoid logging sensitive payloads; sanitize webhook bodies (no secrets).
- **Sampling**: if traffic grows, sample info-level request logs; keep errors and audits full.

---

## 11) Housekeeping (DB)

> Perform during low-traffic windows; **back up first**.

### Find largest tables

```sql
SELECT relname AS table, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC;
```

### Reindex/Analyze (if needed)

```sql
REINDEX TABLE receipt_items;
VACUUM (ANALYZE) receipt_items;
```

### Prune old dev/test data (policy-gated)

```sql
-- Example ONLY: do not delete production finance data without policy!
-- DELETE FROM payment_events WHERE created_at < now() - interval '365 days';
```

---

## 12) Incident runbooks

### A) App down (`/healthz` 5xx)

1. `docker compose ps` → app status.
2. `docker logs hpc --tail 200` → last errors.
3. Restart app: `docker compose restart hpc`.
4. If crash-looping, roll back image tag to last known good.

**Post-mortem**: capture logs & timeline; open issue.

### B) Ready probe failing (`/readyz` 500)

1. `docker compose logs pg` (or `db`) → Postgres healthy?
2. `psql` from hpc container to DB:
3. Fix DB network or credentials; restart app after DB is healthy.

### C) Flood of failed logins

1. Inspect auth throttle table counts.
2. Consider temporary rate-limit at reverse proxy (per IP).
3. Notify users and review audit log.

### D) Webhook failures spike

1. Check reverse proxy access logs (requests to `/payments/webhook`).
2. Validate signature header name and secret.
3. Re-deliver events from provider dashboard after fix.

---

## 13) Zero-downtime upgrades (single host)

1. **Pre**: take a DB backup; note current image tag.
2. `docker pull your-registry/hpc-billing:<new>`
3. `docker compose up -d` (app restarts quickly; proxy keeps connections).
4. Verify `/readyz` 200; run a quick smoke test.
5. **Rollback**: `docker compose pull your-registry/hpc-billing:<old>` → `up -d`.

---

## 14) Configuration sanity (prod)

- `APP_ENV=production`
- `FLASK_SECRET_KEY` set, long random
- `SESSION_COOKIE_SECURE=1`, `HTTPONLY=1`, `SAMESITE=Lax`
- `PAYMENT_WEBHOOK_SECRET` set and matches provider
- `SLURMRESTD_URL` over **HTTPS** (+ token/certs)
- **Adminer** & **Swagger UI** not exposed publicly

---

## 15) Ops FAQ

**Q: CSV export empty but jobs exist in Slurm?**
A: Confirm date range; the CSV uses server-side fetch logic. If Slurm is temporarily unavailable, only historical DB data appears; wait until Slurm recovers or use a longer window.

**Q: Can we un-lock a user early?**
A: Yes—clear/adjust their auth throttle entry (via admin tool or SQL). Make sure to log the action.

**Q: Do we need to “rotate logs”?**
A: In Compose, logs stream to Docker; use a logging driver with retention limits or ship to a centralized store.

**Q: Can we change rates retroactively?**
A: No; rates are applied at pricing time. To adjust past receipts, void and re-issue per policy (manual process).

---

## 16) Ops templates

### Example systemd timer (if not using cron)

```

# /etc/systemd/system/hpc-backup.service

\[Unit]
Description=Backup HPC Billing DB

\[Service]
Type=oneshot
ExecStart=/usr/bin/docker compose exec pg sh -lc 'pg_dump -U "\$POSTGRES_USER" -d "\$POSTGRES_DB" -F c -f /tmp/hpc_app.dump'
ExecStart=/usr/bin/docker cp pg:/tmp/hpc_app.dump /var/backups/hpc/hpc_app-\$(date +%%F).dump

# /etc/systemd/system/hpc-backup.timer

\[Unit]
Description=Daily HPC Backup

\[Timer]
OnCalendar=_-_-\* 02:15:00
Persistent=true

\[Install]
WantedBy=timers.target

```

Enable:

```bash
systemctl enable --now hpc-backup.timer
```

---

## 17) Appendix: minimal SLOs

- **Availability**: 99.5% monthly for `/readyz`.
- **Latency**: p95 `< 500ms` for main UI endpoints at normal load.
- **Data freshness**: new Slurm jobs visible within `15 min` under normal conditions.
- **Payments reconciliation**: no **pending** older than `24h`.

Track breaches in your on-call notes and review quarterly.

---

_End of Operations._
