# Docker quickstart & handy commands

> All commands should be run in the project root (where `docker-compose.yml` lives).

## First time: build & start everything

```bash
# Build images (only needed after code/dep changes) and start in the background
docker compose up -d --build
```

- App: [http://localhost:8000](http://localhost:8000)
- Adminer (DB UI): [http://localhost:8080](http://localhost:8080)
- Postgres is started automatically and the app waits for it.

## See what’s running

```bash
docker compose ps
```

## Follow logs (live)

```bash
# All services
docker compose logs -f

# A single service
docker compose logs -f hpc      # app
docker compose logs -f pg       # postgres
docker compose logs -f adminer  # adminer
```

## Start/stop

```bash
# Stop containers but keep them around
docker compose stop

# Start again (no rebuild)
docker compose start

# Stop and remove containers + network (keeps DB volume/data)
docker compose down
```

## Rebuild the app only

```bash
# Rebuild image for the app and restart just that service
docker compose build hpc
docker compose up -d --no-deps hpc
```

## Run only one service

```bash
# Bring up only Postgres (useful for DB-only tasks)
docker compose up -d pg

# Bring up only the app (will start pg automatically due to depends_on)
docker compose up -d hpc
```

## “Shell into” a container

```bash
# App shell
docker compose exec hpc bash

# Adminer shell (BusyBox/sh)
docker compose exec adminer sh

# psql inside the Postgres container
docker compose exec pg psql -U hpc_user -d hpc_app
```

## Run tests inside the container

```bash
# Reuse the running app container
docker compose exec hpc pytest -q

# OR run in a fresh, one-off container (doesn't reuse state)
docker compose run --rm hpc pytest -q
```

## Inspect a container deeply (advanced)

```bash
# Show low-level info (ports, env, health, mounts)
docker inspect hpc
```

## Database data (IMPORTANT)

We persist Postgres data in a named volume so it survives `down`:

- Volume name: `hpc_flask_pgdata`
- **Safe:** `docker compose down` → data stays
- **Danger:** `docker compose down -v` → **deletes the DB volume and your data**

## Back up the database

**Option A: logical dump (recommended for portability)**

```bash
# Create a SQL dump from inside the pg container
docker compose exec pg pg_dump -U hpc_user -d hpc_app -F c -f /tmp/backup.dump

# Copy it to your host
docker compose cp pg:/tmp/backup.dump ./backup_$(date +%F).dump
```

**Restore:**

```bash
# Put the dump back into the container
docker compose cp ./backup_YYYY-MM-DD.dump pg:/tmp/restore.dump

# Restore into an empty database
docker compose exec pg pg_restore -U hpc_user -d hpc_app --clean --if-exists /tmp/restore.dump
```

**Option B: raw volume archive (fast, whole cluster)**

```bash
# Create a tar.gz of the Postgres data volume (Linux/macOS)
docker run --rm -v hpc_flask_pgdata:/var/lib/postgresql/data -v "$PWD":/backup \
  alpine sh -c 'cd /var/lib/postgresql/data && tar czf /backup/pgdata.tar.gz .'
```

Restore by stopping services, removing the volume, recreating, and extracting back in (only if you know what you’re doing).

## Reset the database (DESTROYS DATA)

```bash
docker compose down
docker volume rm hpc_flask_pgdata
docker compose up -d --build
```

## Common tweaks

- **Change host ports:** Edit the `ports:` lines in `docker-compose.yml`.
  Example: Postgres `5433:5432` means _host_ `5433` → _container_ `5432`.
  The app connects to Postgres via the internal name `pg:5432` (not the host port).
- **Mount local files:** We mount `./instance:/app/instance`. Put your `test.csv` or configs in `instance/` and set env `FALLBACK_CSV=/app/instance/test.csv`.

## Health checks (optional curiosity)

```bash
# See if Postgres reports healthy
docker inspect --format='{{json .State.Health}}' pg | jq
```

## Clean up dangling stuff (safe)

```bash
# Remove unused images/containers/networks (won't touch named volumes)
docker system prune
```

---

## Quick glossary

- **Service**: a thing in `docker-compose.yml` (e.g., `hpc`, `pg`, `adminer`).
- **Container**: a running instance of a service.
- **Image**: the built filesystem + app used to start containers.
- **Volume**: persistent data (our Postgres data lives here).

If you only remember three commands:
`docker compose up -d --build`, `docker compose logs -f`, and `docker compose down`.
