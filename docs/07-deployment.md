# Deployment

> How to run the HPC Billing Platform in **dev**, **staging**, and **production** with as few app-code changes as possible. Pick the path that matches your constraints, then follow the checklists.

---

## 1) Deployment options (pick one)

| Option                                  | When to use                      | Pros                                                   | Cons                                  |
| --------------------------------------- | -------------------------------- | ------------------------------------------------------ | ------------------------------------- |
| **Docker Compose (dev)**                | Local development, demos         | 1 command up; includes Postgres + Adminer + Swagger UI | Not HA; not hardened                  |
| **Compose + reverse proxy (prod-lite)** | Single VM/server, campus network | Simple, TLS at proxy, keeps app stateless              | Single host; manual ops               |
| **Kubernetes**                          | Multiple nodes or cloud          | Self-healing, rolling updates                          | More moving parts                     |
| **systemd + venv**                      | Bare-metal box without Docker    | Minimal runtime deps                                   | You own Python/OS deps; less portable |

> The app is **12-factor friendly**: all settings via env vars, no local disk state (except Postgres).

---

## 2) Prerequisites

- A Linux host (or WSL2) with Docker (and optionally docker-compose plugin).
- A domain name and TLS cert (Let’s Encrypt via Caddy/Traefik/Certbot is fine) for production.
- An SMTP account (optional, if you later add email) — not required today.
- Access to your Slurm environment (prefer `slurmrestd`; fallback `sacct`).
- A payment provider account (optional in dev; use the built-in **dummy** simulate flow).

---

## 3) Environment variables (minimum set)

Create `.env.production` (don’t commit):

```bash
# Core
APP_ENV=production
FLASK_SECRET_KEY=change-me-in-prod
DATABASE_URL=postgresql+psycopg2://hpc_user:***@db:5432/hpc_app

# Admin bootstrap (first run only; can be removed after)
ADMIN_PASSWORD=generate-a-strong-one

# Slurm integration (prefer slurmrestd)
SLURMRESTD_URL=https://slurm.example.org:6820
SLURMRESTD_TOKEN=...         # or client cert paths if using mTLS
# Fallback CSV (demo only)
# FALLBACK_CSV=/app/instance/test.csv

# Payments (dev: use dummy + simulate)
PAYMENT_PROVIDER=dummy
PAYMENT_CURRENCY=THB
PAYMENT_WEBHOOK_SECRET=super-secret
SITE_BASE_URL=https://billing.example.org

# Metrics (optional)
METRICS_ENABLED=1
```

> In dev, you can keep these in `.env` and set `APP_ENV=development` + `SEED_DEMO_USERS=1`.

---

## 4) Dev with Docker Compose (reference)

From repo root:

```bash
docker compose up -d --build
# App: http://localhost:8000
# Adminer (dev only): http://localhost:8080
# Swagger UI (if enabled): http://localhost:8081
```

The provided `docker-compose.yml` already:

- Brings up **Postgres**, **Adminer**, **App** (`gunicorn`), and optional **Swagger UI**.
- Seeds demo users in development.
- Exposes `/healthz`, `/readyz`, `/metrics` for probes/Prometheus.

---

## 5) Production with Compose + reverse proxy

### 5.1 Compose override for prod

Create `docker-compose.prod.yml`:

```yaml
services:
  hpc:
    image: your-registry/hpc-billing:latest # build and push your image
    env_file:
      - .env.production
    ports:
      - "127.0.0.1:8000:8000" # bind to loopback; proxy terminates TLS
    command: gunicorn -b 0.0.0.0:8000 --access-logfile - --error-logfile - wsgi:app
    restart: unless-stopped
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "python - <<'PY'\nimport urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=2).getcode()==200 else 1)\nPY",
        ]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s

  db:
    image: postgres:16
    environment:
      POSTGRES_USER: hpc_user
      POSTGRES_PASSWORD: change-me
      POSTGRES_DB: hpc_app
    volumes:
      - pgdata:/var/lib/postgresql/data
    restart: unless-stopped

  # Remove Adminer in prod, or bind to localhost only if you must keep it.
  # swagger (OpenAPI UI) should also be prod-disabled or auth-protected.

volumes:
  pgdata:
```

Start:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### 5.2 Reverse proxy (choose one)

**Nginx**

```nginx
server {
  listen 443 ssl http2;
  server_name billing.example.org;

  # TLS + security headers
  add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
  add_header X-Frame-Options DENY;
  add_header X-Content-Type-Options nosniff;
  add_header Referrer-Policy same-origin;
  add_header Content-Security-Policy "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'";

  location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_read_timeout 60s;
  }
}
```

**Caddy (auto-TLS)**

```
billing.example.org {
  reverse_proxy 127.0.0.1:8000
  header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains"
    X-Frame-Options "DENY"
    X-Content-Type-Options "nosniff"
    Referrer-Policy "same-origin"
    Content-Security-Policy "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
  }
}
```

---

## 6) Kubernetes (minimal sketch)

Use this when you need HA and rolling updates.

- **Deployment** for `hpc` with `readinessProbe: /readyz` and `livenessProbe: /healthz`.
- **Service** ClusterIP for the app.
- **Ingress** (nginx/traefik) for TLS and headers.
- **Stateful** Postgres: use a managed database (RDS/CloudSQL) or a Helm chart.

Example probes:

```yaml
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  periodSeconds: 15
```

Mount secrets via K8s Secrets → env.

---

## 7) Slurm integration

- **Preferred:** `slurmrestd` over HTTPS with auth (JWT/mTLS). Set:

  - `SLURMRESTD_URL`, `SLURMRESTD_TOKEN` (or cert paths).

- **Fallback:** `sacct` available inside the container/host.

  - Don’t pass user-supplied flags directly.
  - Bound date windows server-side.

- **Demo:** `FALLBACK_CSV=/app/instance/test.csv` (mount read-only).

Network rules:

- Allow the app to egress to `slurmrestd`.
- No inbound access from Slurm to the app is required (webhooks are from payment providers, not Slurm).

---

## 8) Payments (webhook) setup

- Expose `https://billing.example.org/payments/webhook` on the public internet.
- Configure the provider to send **signed** events to that URL.
- Set `PAYMENT_WEBHOOK_SECRET` in env (do NOT commit).
- Restrict by IP/rate-limit at the proxy if possible.
- In dev, use `/payments/simulate` (dummy provider) to test end-to-end.

---

## 9) OpenAPI (optional, dev/stage)

Keep the spec in `docs/api/openapi.yaml`. To host Swagger UI:

```yaml
swagger:
  image: swaggerapi/swagger-ui:latest
  environment:
    SWAGGER_JSON: /spec/openapi.yaml
  volumes:
    - ./docs/api:/spec:ro
  ports: ["8081:8080"]
```

Embed in `04-api.md` with an iframe (`http://localhost:8081/?url=/spec/openapi.yaml`).
**Do not** expose Swagger UI publicly in production unless you protect it.

---

## 10) Observability

- **/metrics**: enable Prometheus scraping (private network or basic auth).
- **Logs**: app logs to stdout; use `docker logs` or a log collector (Loki/ELK).
- **Health**:

  - `/healthz` → process up.
  - `/readyz` → DB reachable.

Optional dev stack: Prometheus + Grafana (compose services commented are fine).

---

## 11) Database: provisioning, backup & restore

**Provision**

- Postgres 14+ recommended (example uses 16).
- Create user/db; set `DATABASE_URL`.

**First run**

- The app will create tables and (in development) seed demo users if configured.

**Backup**

```bash
# On host (container name: db)
docker exec -t db pg_dump -U hpc_user -d hpc_app -F c -f /tmp/hpc_app.dump
docker cp db:/tmp/hpc_app.dump ./backups/hpc_app-$(date +%F).dump
```

**Restore**

```bash
docker cp ./backups/hpc_app-2025-09-13.dump db:/tmp/restore.dump
docker exec -it db pg_restore -U hpc_user -d hpc_app -c /tmp/restore.dump
```

Schedule backups and keep them **encrypted** off-host.

---

## 12) CI/CD (example workflow)

- **Build** on every push to `main`: `docker build -t your-registry/hpc-billing:$GIT_SHA .`
- **Scan** the image (Trivy) and dependencies (`pip-audit`).
- **Push** to registry; **tag** `:prod` on release.
- **Deploy**: pull on the server and `docker compose up -d`.

Example deploy step (on server):

```bash
docker pull your-registry/hpc-billing:prod
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

---

## 13) Zero-downtime & rollbacks

- Compose: run two app containers behind a reverse proxy, update one at a time.
- k8s: rolling update with `maxUnavailable=0`, `maxSurge=1`.
- Always keep the previous image tag handy; rollback by re-tagging and redeploying.

---

## 14) Hardening checklist (prod)

- [ ] Reverse proxy terminates TLS; HSTS/CSP/security headers set.
- [ ] `SESSION_COOKIE_SECURE/HTTPONLY/SAMESITE` configured.
- [ ] Adminer and Swagger UI **not** exposed publicly.
- [ ] `PAYMENT_WEBHOOK_SECRET` set; webhook endpoint reachable; logs show signature checks.
- [ ] Slurm via **TLS**; tokens/certs rotated; app can’t execute arbitrary shell from user input.
- [ ] Postgres on private network; backups scheduled and tested.
- [ ] Probes: `/healthz` and `/readyz`; metrics scraped privately.
- [ ] Logs shipped centrally; no secrets in logs.
- [ ] Image/deps scanned; base images updated.
- [ ] `.env.production` not in VCS; secrets via env or secret store.

---

## 15) Troubleshooting quickies

- **Swagger shows Petstore** → the UI can’t find your spec; fix `SWAGGER_JSON` path or add `?url=/spec/openapi.yaml`.
- **Ready probe failing** → check DB connectivity and `DATABASE_URL`.
- **Webhook “ignored”** → wrong signature or amount/currency mismatch; check provider config and logs.
- **CSV empty** → your date window has no jobs; confirm Slurm source and app’s normalization.
- **Admin login fails** → ensure `ADMIN_PASSWORD` was set on first boot; create an admin in the DB if needed.

---
