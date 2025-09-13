# Performance

> How to measure, test, and tune the HPC Billing Platform. This covers metrics, Prometheus/Grafana, SLOs & alerts, load testing, and the knobs you can turn (Gunicorn, DB, Slurm ingestion).

---

## 1) What “good” looks like (targets)

- **Availability**: `/readyz` ≥ 99.5% monthly
- **Latency** (UI & JSON):

  - p95 ≤ **500 ms**
  - p99 ≤ **1.5 s**

- **Error rate** (5xx): ≤ **2%** over 10 min
- **Data freshness**: new Slurm jobs visible ≤ **15 min**
- **Payments**: webhook → receipt paid median ≤ **60 s**

Use the dashboards & alerts below to enforce these.

---

## 2) Metrics you already export

From `services/metrics.py` (dedicated registry; `/metrics` route auto-registered when `METRICS_ENABLED` is true — default **on**):

| Metric                              | Type      | Labels                     | Meaning                                                         |                  |
| ----------------------------------- | --------- | -------------------------- | --------------------------------------------------------------- | ---------------- |
| `http_requests_total`               | Counter   | `method, endpoint, status` | Request rate & errors                                           |                  |
| `http_request_duration_seconds`     | Histogram | `endpoint, method`         | Latency distribution                                            |                  |
| `auth_login_success_total`          | Counter   | —                          | Successful logins                                               |                  |
| `auth_login_failure_total`          | Counter   | `reason`                   | Failed logins (e.g., `bad_credentials`)                         |                  |
| `auth_lockout_active_total`         | Counter   | —                          | Lockout pages shown                                             |                  |
| `auth_lockout_start_total`          | Counter   | —                          | Lockouts started                                                |                  |
| `auth_lockout_end_total`            | Counter   | —                          | Lockouts ended                                                  |                  |
| `auth_forbidden_redirect_total`     | Counter   | —                          | Non-admin tried admin page                                      |                  |
| `billing_receipt_created_total`     | Counter   | \`scope=user               | admin\`                                                         | Receipts created |
| `billing_receipt_marked_paid_total` | Counter   | `actor_type`               | Receipts marked paid (manual)                                   |                  |
| `billing_receipt_voided_total`      | Counter   | —                          | (present for future use; no UI flow)                            |                  |
| `csv_download_total`                | Counter   | `kind`                     | CSV downloads (`admin_paid`, `my_usage`, `user_usage`, `audit`) |                  |
| `payments_webhook_events_total`     | Counter   | `provider, event, outcome` | Webhook events & outcomes                                       |                  |

**Heads-up:** dashboards should use label `actor_type` (not `actor`) for `billing_receipt_marked_paid_total` — the code already warms `actor_type=admin`.

**Buckets:** the histogram uses **default** Prometheus buckets unless you uncomment a custom list. If you primarily serve HTML and small JSON, consider:

```python
Histogram(..., buckets=(0.005,0.01,0.025,0.05,0.1,0.25,0.5,1,2.5,5), ...)
```

---

## 3) Prometheus setup (dev)

If Prometheus runs on your host and scrapes the app at `http://localhost:8000/metrics`:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "hpc_flask"
    scrape_interval: 5s
    static_configs:
      - targets: ["host.docker.internal:8000"]
        labels: { app: "hpc_flask" }
```

If Prometheus runs **in the same Docker network** as the app, replace the target with the **service name** (e.g., `hpc:8000`).

**Verify:**

```bash
curl -i http://localhost:8000/metrics
# Expect Content-Type: text/plain; version=0.0.4; charset=utf-8
```

Disable metrics by setting `METRICS_ENABLED=0` (the route won’t register).

---

## 4) Grafana: key panels (PromQL)

> Create a dashboard with these panels. Adjust the job/instance labels to your scrape config.

**Traffic & errors**

```promql
sum by (method) (rate(http_requests_total[5m]))
sum by (status) (rate(http_requests_total[5m]))
sum(rate(http_requests_total{status=~"5.."}[5m]))
/ on() group_left() sum(rate(http_requests_total[5m]))
```

**Latency (p50/p95/p99)**

```promql
histogram_quantile(0.5, sum by (le)(rate(http_request_duration_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le)(rate(http_request_duration_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le)(rate(http_request_duration_seconds_bucket[5m])))
```

**Top slow endpoints**

```promql
topk(5, histogram_quantile(0.95,
  sum by (endpoint, le)(rate(http_request_duration_seconds_bucket[5m]))))
```

**Auth security**

```promql
rate(auth_login_failure_total[5m])
rate(auth_lockout_start_total[15m])
```

**Business KPIs**

```promql
rate(billing_receipt_created_total[15m])        # receipt creation velocity
rate(billing_receipt_marked_paid_total[15m])    # admin reconciliations
rate(csv_download_total[15m])                   # exports activity
rate(payments_webhook_events_total[5m])         # provider activity
```

---

## 5) Alerts (examples)

Add these to Prometheus alerting rules:

```promql
# High error rate
ALERT HighErrorRate
IF (sum(rate(http_requests_total{status=~"5.."}[5m])) /
    sum(rate(http_requests_total[5m]))) > 0.02
FOR 10m
LABELS { severity="page" }
ANNOTATIONS { summary="5xx > 2% for 10m" }

# Latency SLO breach
ALERT HighLatencyP95
IF histogram_quantile(0.95, sum by (le)(rate(http_request_duration_seconds_bucket[5m]))) > 0.5
FOR 10m
LABELS { severity="page" }
ANNOTATIONS { summary="p95 > 500ms for 10m" }

# No webhooks when expected
ALERT NoWebhooks
IF rate(payments_webhook_events_total[1h]) == 0
FOR 2h
LABELS { severity="warn" }
ANNOTATIONS { summary="No payment webhooks in last 2h" }

# Readiness failing
ALERT AppNotReady
IF probe_success{job="hpc_readyz"} == 0 OR on() hpc_ready == 0
FOR 3m
LABELS { severity="page" }
```

(Adjust to your exporter names; some teams scrape `/readyz` via blackbox exporter.)

---

## 6) Load testing (k6)

Install k6 and run this quick script to simulate logins, browsing, receipt creation:

```js
// save as k6-login-receipt.js
import http from "k6/http";
import { check, sleep } from "k6";

export let options = {
  vus: 20,
  duration: "3m",
  thresholds: {
    http_req_failed: ["rate<0.02"],
    http_req_duration: ["p(95)<500", "p(99)<1500"],
  },
};

export default function () {
  // get login page to fetch cookie + csrf
  let loginPage = http.get("http://localhost:8000/login");
  let csrf = /name="csrf_token" value="([^"]+)"/.exec(loginPage.body)[1];
  let cookies = loginPage.cookies;

  // login
  let res = http.post(
    "http://localhost:8000/login",
    {
      username: "alice",
      password: "alice",
      csrf_token: csrf,
    },
    { cookies }
  );

  check(res, {
    "login redirected": (r) => r.status === 200 || r.status === 302,
  });

  // browse usage
  http.get("http://localhost:8000/me", { cookies });
  sleep(Math.random() * 1);

  // export csv (small load on DB)
  http.get("http://localhost:8000/me.csv?start=2025-09-01&end=2025-09-13", {
    cookies,
  });

  // (optional) attempt receipt creation (comment out in prod)
  // http.post('http://localhost:8000/me/receipt', { before: '2025-09-13', csrf_token: csrf }, { cookies });

  sleep(Math.random() * 1);
}
```

Run:

```bash
k6 run k6-login-receipt.js
```

Watch Grafana while it runs; confirm goals are met.

---

## 7) Server tuning (Gunicorn)

The app is IO-bound (DB, Slurm REST). Start with:

```bash
gunicorn -b 0.0.0.0:8000 wsgi:app \
  --workers 2 --threads 4 --worker-class gthread \
  --timeout 60 --keep-alive 30 --access-logfile - --error-logfile -
```

Guidelines:

- **Workers**: \~ `CPU cores` (2–4) for `gthread`; increase **threads** (4–8) for concurrency.
- **Timeout**: keep ≤ 60 s; investigate anything hitting it.
- **Keep-alive**: 30 s is fine behind a reverse proxy.
- **Ulimit/backlog**: if you expect spikes, raise `--backlog 2048` and OS limits.

Scale up by adding more app containers behind the proxy (stateless).

---

## 8) Database performance

Indexes we rely on (from the data model):

- `receipt_items(job_key)` **UNIQUE**
- `payment_events(provider, external_event_id)` **UNIQUE**
- `receipts(username, created_at DESC)`
- `receipts(status, created_at DESC)`
- `audit_log(ts DESC)`

Tips:

- Ensure `work_mem` and `shared_buffers` are sane for your Postgres size.
- **Connection pool**: for SQLAlchemy (if configurable in your `base.py`):

  - `pool_size=5–10`, `max_overflow=10`, `pool_recycle=1800`.

- Keep long CSV exports off peak hours if data is large; paginate UI views.

Backup/restore performance lives in **08-ops.md**.

---

## 9) Slurm ingestion performance

- Prefer `slurmrestd` (HTTP) over `sacct` (CLI).
- Bound windows: fetch jobs for the requested date range only; avoid “open-ended” queries.
- If `sacct` fallback is used, ensure the CLI format is minimal and date-bounded.
- Demo CSV: keep small; parse once per request is fine for dev.

Future optimization (optional): cache fetched Slurm windows in memory or a small table keyed by `(user, start, end)` with a short TTL.

---

## 10) Caching & HTTP efficiency

- **`GET /formula`** already supports **ETag** → use `If-None-Match` in any automation.
- For static assets, have your reverse proxy set far-future cache headers.
- Consider CDN or reverse proxy caching for public assets only (never cache user-scoped pages).

---

## 11) Profiling & diagnostics

Quick tools:

- **py-spy** (no code changes): `py-spy top --pid <gunicorn-worker-pid>`
- **Flamegraphs**: `py-spy record -o profile.svg --pid <pid>` → open `profile.svg`
- **SQL logging** (temporary): enable SQLAlchemy echo in dev to catch slow queries.

What to look for:

- Repeated Slurm calls per request
- N+1 queries when rendering tables
- Large CSV constructed in memory (consider streaming generator if needed)

---

## 12) Known pitfalls & how to avoid them

- **Label cardinality blow-ups**: keep `endpoint` label to **route names** (e.g., `/me`, not `/me/receipts/123`). If your middleware uses raw paths, normalize to a template or Flask endpoint name.
- **Histogram default buckets** too coarse/fine: set custom buckets if your p95 sits near bucket edges.
- **Metrics disabled** unexpectedly: check `METRICS_ENABLED`; in prod you might set it to `0`—remember to flip it back on when you want visibility.
- **Pre-warming**: you already call `.inc(0)` on common label combos — keep that to avoid “no data” panels.

---

## 13) Performance playbook (when things go slow)

1. **Confirm**: latency ↑ and 5xx? Look at Grafana (p95, error rate).
2. **Scope**: which `endpoint` spikes? Use “Top slow endpoints”.
3. **Logs**: check app logs for timeouts/tracebacks.
4. **DB**: inspect slow queries; add/verify indexes.
5. **External**: Slurm REST latency/network? Payment provider delays?
6. **Scale**: add 1–2 more Gunicorn workers or app replicas; re-test.
7. **Optimize**: cache Slurm reads; stream large CSVs; reduce template work.

---

## 14) Benchmarks to capture in PRs (template)

- Scenario (users, endpoints)
- Hardware/env
- p50/p95/p99 and error rate, before/after
- DB CPU, QPS, rows scanned
- Notable regressions (if any) and why they’re acceptable

---
