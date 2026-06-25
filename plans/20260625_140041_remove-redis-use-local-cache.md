# Plan — Remove Redis entirely (replace Django cache with a local backend)

**Status:** completed

## Summary

The company's deployment constraints rule out Redis. The earlier Celery removal
(`change_log/20260617_112017_...`) already dropped Redis's broker role, but Redis
is **still wired as the Django cache backend** (`django_redis.cache.RedisCache`).

Audit of the codebase shows the cache is used in exactly **one place** — the
`/healthz` probe does a `cache.set`/`cache.get` round-trip
(`apps/core/views.py:44-50`). The login throttle is DB-backed (`LoginAttempt`
rows, `apps/accounts/security.py`), and sessions use the default DB backend
(no `SESSION_ENGINE` override). So nothing functional depends on Redis.

This change swaps the cache backend to Django's built-in **LocMemCache**
(in-process, zero external services), removes the `redis`/`django-redis`
dependencies, drops the `REDIS_*` env vars, and updates the `/healthz` probe to
report a generic `cache` check instead of `redis`. Docs and deploy scripts that
install/reference Redis are updated.

LocMemCache is chosen over DatabaseCache because the only consumer is a liveness
probe; per-process memory is sufficient and needs no cache table/migration. (If a
shared cross-worker cache is ever needed, switch the backend to
`django.core.cache.backends.db.DatabaseCache` + `createcachetable` — noted but not
done here.)

## Files affected

- `mmftp/settings.py` — **changed**: replace the `REDIS_URL` + `CACHES` (RedisCache)
  block with a `LocMemCache` `CACHES` block; remove the `REDIS_URL` env read.
- `apps/core/views.py` — **changed**: `healthz()` reports `cache` instead of
  `redis`; docstring updated.
- `requirements.txt` — **changed**: remove `redis==5.2.1` and
  `django-redis==5.4.0`.
- `.env.example` — **changed**: delete the Redis section (`REDIS_URL`,
  `REDIS_HOST`, `REDIS_PORT`).
- `deploy/.env.production.template` — **changed**: delete Redis vars.
- `deploy/INSTALL_NO_DOCKER.md` — **changed**: remove Redis from the
  prerequisites/service table and install steps.
- `deploy/install_no_docker.sh` — **changed**: drop the Redis package install +
  service enable.
- `deploy/update_no_docker.sh` — **changed**: drop any Redis references.
- `deploy/wsl_test_install.sh` — **changed**: drop Redis install/start.
- `README.md` — **changed**: stack line, prereqs, `/healthz` description
  (DB only), and the stale `mmftp-worker`/`mmftp-beat` line (line 51-52) corrected
  to the timer units.
- `CLAUDE.md` — **changed**: de-Redis the intro, `/healthz` description, and the
  "Runtime config / cache" mentions.
- `ROADMAP.md` — **changed**: dated note that Redis was removed.
- `deploy/nginx.host.conf`, `load_tests/README.md`, `scripts/build-deploy-package.ps1`
  — **changed only if** they install or require Redis (verify during edit; doc-only
  mentions get a light touch).
- `docker-compose.dev.yml` — **created**: dev-only stack running **PostgreSQL +
  MinIO** (and a one-shot `mc` bucket-creator) for local development on Windows.
  **No Redis, no app/worker/beat containers** — Django runs on the host via
  `runserver`. Ports `5432`, `9000` (S3), `9001` (console); named volumes
  `pg_data` / `minio_data`; credentials read from `.env` with the same defaults as
  `.env.example`. This is for development convenience only; the production Debian
  host remains no-Docker per company constraints.

## How dev services run (Docker) vs. production (no Docker)

- **Local dev (Windows):** `docker compose -f docker-compose.dev.yml up -d` brings
  up Postgres + MinIO. Django itself runs on the host (`runserver`), not in Docker.
- **Production (Debian):** unchanged — no Docker; Postgres + MinIO installed as host
  services per `deploy/INSTALL_NO_DOCKER.md`. (Redis no longer needed in either.)

## Database changes

None. (No cache table — LocMemCache is in-memory. No model/migration changes.)

## Effect on the current system

- **Runtime:** No external Redis process required anymore. `/healthz` still returns
  200 and now reports `{"database": "ok", "cache": "ok"}`. LocMemCache is
  per-process, so each Gunicorn worker has its own cache — fine, because the only
  use is a self-contained liveness round-trip.
- **Backward compatibility:** Any operator `.env` with `REDIS_URL` set is simply
  ignored (no longer read) — no breakage. A server that still has Redis installed
  is harmless; it just goes unused and can be uninstalled.
- **Services to restart:** `mmftp-web` (Gunicorn) must be restarted to pick up the
  settings change. Background job timers re-exec each run, so no action needed
  there. Redis service can be stopped/removed after deploy.
- **Config/secrets needed:** none added. `REDIS_*` vars become obsolete.
- **Dependencies:** after `requirements.txt` edit, run
  `pip install -r requirements.txt` (or just leave the now-unused packages in the
  venv until next rebuild). No new packages.
- **Rollback:** revert this commit and `pip install redis django-redis` to restore
  the RedisCache backend. No data migration to undo.

## Local Windows dev (the user's immediate goal)

After this change, local dev needs only **PostgreSQL + MinIO** (no Redis), run via
Docker; Django runs on the Windows host.

```powershell
docker compose -f docker-compose.dev.yml up -d   # Postgres + MinIO + bucket
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # defaults already point at 127.0.0.1 for both
venv\Scripts\python manage.py migrate
venv\Scripts\python manage.py collectstatic --noinput
venv\Scripts\python manage.py createsuperuser
venv\Scripts\python manage.py runserver
```

`.env` defaults already target `127.0.0.1:5432` (Postgres) and `127.0.0.1:9000`
(MinIO), which match the compose port mappings — no edits needed for a default
local run.
