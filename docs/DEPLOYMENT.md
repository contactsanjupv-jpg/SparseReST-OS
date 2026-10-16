# Deployment (self-hosted, $0 licences)

> Going live with outreach? Follow **`RUNBOOK.md`** -- this file only covers installing the server.

One Linux box (any VPS) is enough: Postgres, the API, N workers, the inbound checker, and the web app.

## 1. First start (Docker Compose)
```bash
mkdir -p secrets && chmod 700 secrets
python -m scripts.secrets_cli gen-key > secrets/master_key.txt          # needs deps: pip install -r requirements.txt
openssl rand -hex 24 > secrets/postgres_password.txt
echo "postgresql+psycopg://postgres:$(cat secrets/postgres_password.txt)@db:5432/sparserest" > secrets/database_url.txt
# app_role's password is set by the migration placeholder -- change it BEFORE exposing anything:
APP_PW=$(openssl rand -hex 24)
echo "postgresql+psycopg://app_role:$APP_PW@db:5432/sparserest" > secrets/app_database_url.txt
chmod 600 secrets/*.txt
cp .env.example .env     # set ALLOWED_ORIGINS, TRUSTED_HOSTS, PUBLIC_API_URL; keep credentials as secret:// refs
docker compose up -d db && docker compose run --rm migrate
docker compose exec db psql -U postgres sparserest -c "ALTER ROLE app_role PASSWORD '$APP_PW'"
docker compose run --rm api python -m scripts.create_founder you@yourdomain.com
docker compose run --rm api python -m scripts.check_production        # must print OK
docker compose up -d --scale worker=2
```
Put TLS in front with `infra/Caddyfile.example` (free automatic certificates).

## 2. Store credentials (never in .env)
```bash
docker compose run --rm api python -m scripts.secrets_cli set smtp_password
```
then `SMTP_PASSWORD=secret://smtp_password` in `.env`. Rotate the master key: prepend a new key (comma-separated),
run `secrets_cli rotate`, drop the old key.

## 3. Processes
| Process | Command | How many | Notes |
|---|---|---|---|
| API | `uvicorn apps.api.main:app` | 1+ | rate limits are per process; login lockout is shared (Postgres) |
| Worker | `python -m scripts.run_worker` | any (`--scale worker=N`) | `SKIP LOCKED` queue; one wins an advisory lock and also runs maintenance |
| Inbound | `python -m scripts.run_inbound` | run 2 for failover | advisory lock => only one polls; standby takes over if it dies |

SIGTERM is graceful: workers finish the task in hand. Without Docker use `infra/systemd/*.service`
(`systemctl enable --now sparserest-worker@1 sparserest-worker@2 sparserest-inbound sparserest-api`).

## 4. Health
`/health/live` (process up), `/health/ready` (DB reachable + migrations current), `/ops/status` and `/ops/workers`
(authenticated: are workers alive, queue depth, which integrations are live vs stubbed). The web app shows the same strip.

## 5. Backups
`docker compose exec db pg_dump -U postgres -Fc sparserest > backup.dump` (cron it). The secrets table is encrypted, but a backup
is only restorable with `secrets/master_key.txt` -- back that up separately, never alongside the dump.
