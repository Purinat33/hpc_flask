# Docker 101 (for this project)

> A gentle, practical intro to Docker so teammates can understand and operate our **HPC Billing Platform** without surprises. We map every concept to our actual files and commands.

| Concept         | What it is                                                                              | In our project                                                               |
| --------------- | --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| **Image**       | Read-only filesystem + metadata (an “executable package”). Built from a **Dockerfile**. | The `hpc` service builds an image from the repo (your app code).             |
| **Container**   | A running instance of an image (plus writable layer).                                   | `hpc`, `pg`, `adminer`, `swagger` are containers created by Compose.         |
| **Tag**         | A human-readable pointer to an image version.                                           | e.g., `your-registry/hpc-billing:prod`                                       |
| **Registry**    | Remote store for images.                                                                | Docker Hub or GHCR (GitHub Container Registry).                              |
| **Volume**      | Managed persistent storage.                                                             | `pgdata` volume stores Postgres data.                                        |
| **Bind mount**  | Map a host path into a container.                                                       | `./docs/api -> /spec` (swagger); `./instance -> /app/instance:ro` (demo CSV) |
| **Network**     | Virtual network where containers can discover each other by service name.               | The app connects to `pg` (hostname resolves to Postgres).                    |
| **Compose**     | A YAML file that defines multi-container apps and how they connect.                     | `docker-compose.yml` defines services, ports, env, healthchecks.             |
| **Healthcheck** | Command that marks a container healthy/unhealthy.                                       | `hpc` and `pg` have healthchecks to coordinate start order.                  |

---

## 1) Big picture

```mermaid
flowchart LR
  Dev[Developer CLI] -->|docker| DD[Docker Daemon]
  DD -->|pull/push| Reg[(Registry)]
  DD --> Img[Images]
  Img -->|run| C1[Containers]
  C1 --> Vol[(Volumes)]
  C1 --> Net{{Networks}}
```

- **Docker client/daemon**: your `docker`/`docker compose` commands talk to the Docker daemon.
- **Image**: a packaged filesystem + metadata (e.g., “Python + our app”). Immutable; versioned by **tags**.
- **Container**: a running instance of an image (has processes, ports, logs).
- **Registry**: where images live (Docker Hub, GHCR, private).
- **Volumes / Networks**: persistent data and isolated networks.

---

## 2) Our Compose stack (what runs)

From `docker-compose.yml`, the main services are:

| Service   | What it is               | Notes                                    |
| --------- | ------------------------ | ---------------------------------------- |
| `pg`      | PostgreSQL 16            | Named volume `pgdata` persists DB files  |
| `adminer` | DB UI (dev only)         | Port `8080`                              |
| `hpc`     | Our Flask app (gunicorn) | Port `8000`, env vars, healthcheck       |
| `swagger` | Swagger UI (dev only)    | Serves `docs/api/openapi.yaml` on `8081` |

**Why Compose?** It defines multi-container apps declaratively: one command brings the whole stack up.

Common dev commands:

```bash
docker compose up -d --build       # build app image + start all services
docker compose logs -f hpc         # follow app logs
docker compose ps                  # list services
docker compose exec hpc bash       # shell into the app container
docker compose down                # stop & remove containers (keeps named volumes)
```

---

## 3) Image vs Container (in 20 seconds)

- **Image** = blueprint (`python:3.11-slim` + our code).
- **Container** = a running process created _from_ the image (with a writable layer).

You can run many containers from the same image.

---

## 4) Dockerfile (how we build the app image)

If you don’t already have one, here’s a sane **multi-stage** `Dockerfile` that matches our `gunicorn -b 0.0.0.0:8000 wsgi:app` command in Compose:

```dockerfile
# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# System deps (add libpq-dev if building psycopg2 from source)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && rm -rf /var/lib/apt/lists/*

# --- builder to cache deps ---
FROM base AS builder
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip wheel --wheel-dir /wheels -r requirements.txt

# --- runtime image ---
FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Minimal OS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 && rm -rf /var/lib/apt/lists/*

# Copy wheels & install
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*

# Copy app code
COPY . /app

# Non-root (optional but recommended)
RUN useradd -m appuser
USER appuser

EXPOSE 8000
CMD ["gunicorn","-b","0.0.0.0:8000","--access-logfile","-","--error-logfile","-","wsgi:app"]
```

Also add a minimal `.dockerignore` to keep builds fast:

```
.git
.env*
venv
env
__pycache__/
*.pyc
dist
build
docs
instance/test.csv
.reports
```

Build locally (optional, Compose can do it):

```bash
docker build -t hpc-billing:dev .
```

---

## 5) Volumes & bind mounts (our data)

```yaml
volumes:
  pgdata: {} # named volume for Postgres data
```

- **Named volume (`pgdata`)**: Docker-managed storage that survives `docker compose down`.
- **Bind mount**: maps a _host_ path into a container. We use it for Swagger UI spec:

  ```yaml
  swagger:
    volumes:
      - ./docs/api:/usr/share/nginx/html/spec:ro
  ```

  `:ro` makes it read-only inside the container.

---

## 6) Ports & networking

- `hpc` exposes **8000** inside the container; Compose maps `8000:8000` → reach it at `http://localhost:8000`.
- All services are on the same **default network**, addressable by service name (e.g., `postgresql://hpc_user@pg:5432/hpc_app`).

**Host access tips**

- Prometheus on the host scraping the app inside Docker uses `host.docker.internal:8000` on Mac/Windows.
- On Linux, prefer putting Prometheus in the same Compose network and target `hpc:8000`.

---

## 7) Environment variables & secrets

We configure the app via env vars (12-factor). In Compose:

```yaml
hpc:
  environment:
    DATABASE_URL: postgresql+psycopg2://hpc_user:muict@pg:5432/hpc_app
    FLASK_SECRET_KEY: change-me
    APP_ENV: development
    PAYMENT_WEBHOOK_SECRET: super-secret
```

**Best practice for prod**

- Use `env_file: .env.production` and keep it **out of git**.
- Don’t bake secrets into the image; pass them at runtime.

---

## 8) Registries, tags, pull/push

- **Tag** your image before pushing:

  ```bash
  docker tag hpc-billing:dev ghcr.io/your-org/hpc-billing:prod
  docker push ghcr.io/your-org/hpc-billing:prod
  ```

- In prod Compose, reference the pushed image:

  ```yaml
  hpc:
    image: ghcr.io/your-org/hpc-billing:prod
    env_file: [.env.production]
  ```

- Prefer immutable tags (e.g., commit SHA) for rollbacks:
  `:2025-09-13-abcdef0`.

---

## 9) Healthchecks & restart policy

We already use a healthcheck in Compose that hits `/readyz`. Docker uses it to mark the container **healthy**.

```yaml
restart: unless-stopped
healthcheck:
  test:
    [
      "CMD-SHELL",
      'python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(''http://127.0.0.1:8000/readyz'', timeout=2).getcode()==200 else 1)"',
    ]
  interval: 10s
  timeout: 3s
  retries: 5
  start_period: 15s
```

---

## 10) Dev vs Prod

- **Dev**: keep `adminer` and `swagger` enabled; mount files; expose all ports to `localhost`.
- **Prod**: remove dev tools; bind the app to `127.0.0.1:8000` and put **TLS** at a reverse proxy (nginx/Caddy). See Book **07 Deployment** for a `docker-compose.prod.yml` overlay.

---

## 11) Typical lifecycle

```mermaid
sequenceDiagram
  participant Dev as Developer
  participant D as Docker/Compose
  participant R as Registry
  Dev->>D: docker build -t hpc-billing:dev .
  Dev->>D: docker compose up -d
  D-->>Dev: hpc, pg, adminer, swagger running
  Dev->>R: docker push ghcr.io/your-org/hpc-billing:prod
  Dev->>D: (on server) compose pull + up -d
```

---

## 12) Troubleshooting quickies

- **Port already in use** → something else on `8000/8080/8081`. Change host port mapping (left side of `host:container`).
- **Swagger shows Petstore** → UI can’t find your spec; ensure `SWAGGER_JSON` path matches the mount (`/usr/share/nginx/html/spec/openapi.yaml`) or use `?url=/spec/openapi.yaml`.
- **DB connection fails** → confirm `DATABASE_URL` points to `pg:5432`, not `localhost`.
- **Healthcheck fails** → `docker compose logs hpc`; check stack trace, DB creds, or wait for `pg` to be healthy.
- **Prometheus can’t scrape** → on Linux use service name (`hpc:8000`) in the same network rather than `host.docker.internal`.

---

## 13) Cheat-sheet

```bash
# Build & run
docker compose up -d --build
docker compose restart hpc

# Inspect
docker compose ps
docker compose logs -f hpc
docker compose exec hpc bash

# Images
docker images | grep hpc
docker build -t hpc-billing:dev .
docker tag hpc-billing:dev ghcr.io/your-org/hpc-billing:prod
docker push ghcr.io/your-org/hpc-billing:prod

# Clean up
docker compose down
docker volume ls
docker volume rm <volume>             # be careful (data loss)
docker system prune -f                # remove unused stuff
```

---

## 14) Glossary (super short)

- **Image**: packaged filesystem with our app.
- **Container**: running instance of an image.
- **Tag**: version label (e.g., `:prod`, `:abcdef0`).
- **Registry**: image storage service.
- **Volume**: persistent storage.
- **Bind mount**: map a local folder into a container.
- **Compose**: multi-service orchestrator for dev/staging/prod-lite.

---

## 15) Docker's Network and Port (Project)

```mermaid
flowchart LR
  %% ====== HOST SIDE (your laptop/server) ======
  subgraph Host
    U[Browser / CLI on host]
    H8000[(localhost:8000)]
    H8080[(localhost:8080)]
    H8081[(localhost:8081)]
    H5433[(localhost:5433)]
  end

  %% ====== DOCKER BRIDGE NETWORK (project_default) ======
  subgraph Docker bridge network
    direction LR
    HPC[["hpc_Flask+Gunicorn
    LISTEN 0.0.0.0:8000"]]
    PG[[pg
    Postgres 16
    LISTEN 0.0.0.0:5432]]
    ADM[[adminer
    Adminer
    LISTEN 0.0.0.0:8080]]
    SWG[[swagger
    Swagger UI
    LISTEN 0.0.0.0:8080]]
  end

  %% host->container published ports (NAT)
  U --> H8000 --> HPC
  U --> H8080 --> ADM
  U --> H8081 --> SWG
  U --> H5433 --> PG

  %% container-to-container (service-name DNS inside bridge net)
  HPC -- "TCP 5432
  host=pg" --> PG
  ADM -- "TCP 5432
  host=pg" --> PG

  %% helpful labels
  classDef host fill:#eef,stroke:#445,color:#111;
  classDef cont fill:#efe,stroke:#494,color:#111;

  class U,H8000,H8080,H8081,H5433 host;
  class HPC,PG,ADM,SWG cont;
```

### How to read this

- **Published ports (host ⇄ container):**

  - `localhost:8000` → `hpc:8000` (the web app)
  - `localhost:8080` → `adminer:8080` (Adminer, dev only)
  - `localhost:8081` → `swagger:8080` (Swagger UI)
  - `localhost:5433` → `pg:5432` (Postgres)

- **Inside the Docker network**, containers reach each other by **service name**:

  - App → DB: `host=pg port=5432` (DNS provided by Compose)
  - Adminer → DB: `host=pg port=5432`

> Each container also gets a private IP (e.g., `172.18.x.y`) on the auto-created bridge. Those IPs are ephemeral; **use service names** (`pg`, `hpc`, …) instead.

#### Healthchecks & paths

- `hpc` healthcheck hits **itself** at `http://127.0.0.1:8000/readyz` (loopback **inside the container**).
- Swagger UI serves `openapi.yaml` from a **bind mount**: `./docs/api → /spec` in the `swagger` container.
- The app reads any demo CSV via bind mount: `./instance → /app/instance:ro` in `hpc`.

---

### Quick reference (who listens where)

| Service   | Container port | Host port → URL           | Who connects to it                  |
| --------- | -------------- | ------------------------- | ----------------------------------- |
| `hpc`     | 8000           | `http://localhost:8000`   | You (browser), Prometheus (if used) |
| `pg`      | 5432           | `localhost:5433` (mapped) | Adminer (host/inside), host tools   |
| `adminer` | 8080           | `http://localhost:8080`   | You (browser)                       |
| `swagger` | 8080           | `http://localhost:8081`   | You (browser)                       |

---

### Why the two `DATABASE_URL`s look different

You have **two contexts**:

1. **Inside containers (Compose env for `hpc`)**

```env
DATABASE_URL=postgresql+psycopg2://hpc_user:muict@pg:5432/hpc_app
```

- The hostname **`pg`** is the **service name**.
- Port **5432** is the Postgres port **inside** the Docker network.
- Use this when the **app runs in Docker** (as your Compose `hpc` service does).

2. **On the host (your `.env` for running tools or the app locally)**

```env
DATABASE_URL=postgresql+psycopg2://hpc_user:muict@127.0.0.1:5433/hpc_app
```

- The hostname `127.0.0.1` is your **host** loopback.
- Port **5433** is the **published** host port mapped to container **5432** (`ports: "5433:5432"`).
- Use this when you run things **on your host**, e.g., a local Flask run, `psql`, or migration tooling.

### Common mistakes:

- From **one container**, `127.0.0.1` means “**this container**”, not the DB. That’s why you **must not** use `127.0.0.1:5433` inside containers. Use `pg:5432`.
- From the **host**, `pg:5432` won’t resolve—use `127.0.0.1:5433` (or `localhost:5433`).
- On macOS/Windows, containers can reach host services via `host.docker.internal` (not needed here, but handy for tools scraping `http://host.docker.internal:8000/metrics`).

---

### File paths (host vs container)

You’ll also see path differences for things like the demo CSV:

- **Inside container (`hpc`)**: `/app/instance/test.csv` (because `./instance` is mounted to `/app/instance:ro`)
- **On the host (your `.env`)**: `C:/Users/User/Downloads/hpc_flask/instance/test.csv`

Both are correct in their **own** context.

---

### Optional (Prometheus/Grafana)

If you enable the commented services:

- Prometheus: `localhost:9090` on host → scrapes `http://host.docker.internal:8000/metrics`
- Grafana: `localhost:3000` on host → connects to Prometheus service

(They live on the same bridge network as the rest if you add them to Compose.)

---
